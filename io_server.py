#!/usr/bin/env python3
import time, subprocess, requests, logging, json, threading, re
from pathlib import Path
from queue import Queue
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import schedule

BASE_DIR = Path.home() / "projects/claude-io"
REPORTS_DIR = BASE_DIR / "reports"
CRONS_DIR = BASE_DIR / "crons"

_cfg = json.loads((BASE_DIR / "config.json").read_text())
BOT_TOKEN = _cfg["bot_token"]
CHAT_ID = _cfg["chat_id"]
TMUX_SESSION = _cfg.get("tmux_session", "claude-agent")
WORKER_TIMEOUT = _cfg.get("worker_timeout", 300)
STABLE_SECS = _cfg.get("stable_secs", 3)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(BASE_DIR / "io_server.log"), logging.StreamHandler()])
log = logging.getLogger(__name__)

task_queue = Queue()
report_done = threading.Event()
capture_done = threading.Event()


# ── 텔레그램 ──────────────────────────────────────────────

def send_telegram(text):
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": chunk}, timeout=10)
            if not r.json().get("ok"):
                log.error(f"send failed: {r.text[:100]}")
        except Exception as e:
            log.error(f"telegram error: {e}")

def send_telegram_new(text) -> int | None:
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text or "⏳"}, timeout=10)
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
        log.error(f"send_new failed: {data}")
    except Exception as e:
        log.error(f"send_new error: {e}")
    return None

def edit_telegram(msg_id, text):
    if not msg_id or not text: return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
            json={"chat_id": CHAT_ID, "message_id": msg_id, "text": text[:4000]}, timeout=10)
    except Exception as e:
        log.error(f"edit error: {e}")


# ── Claude Code ───────────────────────────────────────────

def capture_pane(scrollback=False):
    flag = "-S -2000" if scrollback else ""
    r = subprocess.run(f"tmux capture-pane -t {TMUX_SESSION} -p {flag}",
        shell=True, capture_output=True, text=True)
    return r.stdout

def send_to_claude(message):
    tmp = Path("/tmp/claude_input.txt")
    tmp.write_text(message)
    subprocess.run(f"tmux load-buffer {tmp}", shell=True, capture_output=True)
    subprocess.run(f"tmux paste-buffer -t {TMUX_SESSION}", shell=True, capture_output=True)
    time.sleep(0.5)
    r = subprocess.run(f"tmux send-keys -t {TMUX_SESSION} Enter",
        shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        log.error(f"tmux error: {r.stderr}")
        send_telegram("⚠️ Claude Code 세션 없음")
        return False
    log.info(f"→ claude: {message[:60]}...")
    return True

def is_working(text):
    return "esc to interrupt" in text

def extract_last_response(text: str) -> str:
    """마지막 사용자 ❯ ~ 현재 프롬프트 ❯ 사이 Claude 응답 추출"""
    parts = text.split("❯")
    if len(parts) < 3:
        return ""
    block = parts[-2]  # 마지막에서 두 번째 블록
    # 첫 줄은 사용자 질문이므로 제거
    block = "\n".join(block.splitlines()[1:])
    skip = ("⏵⏵", "────", "bypass permissions", "for agents",
            "esc to interrupt", "shift+tab", "Enter to queue", "Tip:")
    lines = []
    for line in block.splitlines():
        s = line.strip()
        if not s: continue
        if any(p in s for p in skip): continue
        if s.startswith(("⎿", "✻", "✢", "⏳", "│", "╰", "╭")): continue
        lines.append(s)
    return "\n".join(lines).strip()


# ── capture-pane polling (일반 대화) ─────────────────────

def poll_for_response(snapshot_before: str):
    log.info("poll started")
    deadline = time.time() + WORKER_TIMEOUT
    msg_id = send_telegram_new("⏳ 처리 중...")
    log.info(f"msg_id={msg_id}")

    last_text = ""
    last_change = time.time()
    working_seen = False

    while time.time() < deadline:
        time.sleep(1)
        current = capture_pane(scrollback=True)

        if is_working(current):
            working_seen = True

        response = extract_last_response(current)

        if response != last_text:
            if response:
                edit_telegram(msg_id, response)
                log.info(f"update: {response[:50]}...")
            last_text = response
            last_change = time.time()

        stable = not is_working(current) and (time.time() - last_change) >= STABLE_SECS
        quick = not working_seen and (time.time() - last_change) >= 5

        if (stable or quick) and last_text:
            log.info(f"poll done ({'stable' if stable else 'quick'})")
            capture_done.set()
            return

    log.warning("poll timeout")
    if last_text:
        edit_telegram(msg_id, last_text)
    capture_done.set()


# ── Reports 감시 (크론용) ─────────────────────────────────

class ReportHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        path = Path(event.src_path)
        if path.suffix not in (".md", ".txt"): return
        log.info(f"report: {path.name}")
        time.sleep(1)
        try:
            send_telegram(f"📄 {path.stem}\n\n{path.read_text()}")
        except Exception as e:
            log.error(f"report error: {e}")
        report_done.set()


# ── Queue Worker ──────────────────────────────────────────

def worker():
    log.info("worker started")
    while True:
        item = task_queue.get()
        label = item.get("label", "task")
        message = item.get("message", "")
        cron_name = item.get("cron_name")

        if cron_name:
            ts = int(time.time())
            report_path = REPORTS_DIR / f"{ts}_{cron_name}.md"
            full_msg = message + f" 결과를 {report_path} 에 저장해줘"
            report_done.clear()
            send_to_claude(full_msg)
            if not report_done.wait(timeout=WORKER_TIMEOUT):
                log.warning(f"report timeout: {label}")
                send_telegram(f"⚠️ {label} 타임아웃")
        else:
            snapshot = capture_pane(scrollback=True)
            capture_done.clear()
            send_to_claude(message)
            t = threading.Thread(target=poll_for_response, args=(snapshot,), daemon=True)
            t.start()
            if not capture_done.wait(timeout=WORKER_TIMEOUT):
                log.warning(f"capture timeout: {label}")
                send_telegram(f"⚠️ {label} 타임아웃")

        task_queue.task_done()
        time.sleep(1)


def enqueue(message, label=None, cron_name=None):
    task_queue.put({"message": message, "label": label or message[:30], "cron_name": cron_name})
    log.info(f"queued [{label}] (q={task_queue.qsize()})")


# ── Cron 관리 ─────────────────────────────────────────────

DAYS_MAP = {
    "mon": schedule.every().monday, "tue": schedule.every().tuesday,
    "wed": schedule.every().wednesday, "thu": schedule.every().thursday,
    "fri": schedule.every().friday, "sat": schedule.every().saturday,
    "sun": schedule.every().sunday,
}
ALL_DAYS = list(DAYS_MAP.keys())
_loaded_crons = {}

def load_cron(path: Path):
    try:
        cfg = json.loads(path.read_text())
        name = cfg.get("name", path.stem)
        prompt = cfg.get("prompt", "")
        time_str = cfg.get("schedule", "07:00")
        days = cfg.get("days", ALL_DAYS)
        if not cfg.get("enabled", True): return
        jobs = []
        for day in days:
            d = day.lower()
            if d not in DAYS_MAP: continue
            job = DAYS_MAP[d].at(time_str).do(
                lambda p=prompt, n=name: enqueue(f"[크론: {n}] {p}", label=n, cron_name=n))
            jobs.append(job)
        _loaded_crons[name] = jobs
        log.info(f"cron: {name} @ {time_str} {days}")
    except Exception as e:
        log.error(f"cron error {path.name}: {e}")

def unload_cron(name):
    for job in _loaded_crons.pop(name, []):
        schedule.cancel_job(job)

def load_all_crons():
    CRONS_DIR.mkdir(exist_ok=True)
    for f in sorted(CRONS_DIR.glob("*.json")):
        load_cron(f)

class CronHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".json"):
            load_cron(Path(event.src_path))
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".json"):
            name = Path(event.src_path).stem
            unload_cron(name); load_cron(Path(event.src_path))
    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".json"):
            name = Path(event.src_path).stem
            unload_cron(name); send_telegram(f"크론 제거: {name}")

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── 텔레그램 Polling ──────────────────────────────────────

def telegram_polling():
    offset = None
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"timeout": 0, "limit": 100}, timeout=5)
        updates = r.json().get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
            log.info(f"skipping {len(updates)} old messages")
    except Exception as e:
        log.warning(f"offset init: {e}")
    log.info("polling started")

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset: params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params=params, timeout=35)
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if msg.get("chat", {}).get("id") != CHAT_ID: continue
                text = msg.get("text", "").strip()
                if not text: continue
                log.info(f"telegram: {text[:80]}")
                enqueue(text, label=text[:20])
        except requests.exceptions.ReadTimeout:
            pass
        except Exception as e:
            log.error(f"poll error: {e}")
            time.sleep(5)


# ── Main ──────────────────────────────────────────────────

def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    CRONS_DIR.mkdir(parents=True, exist_ok=True)

    observer = Observer()
    observer.schedule(ReportHandler(), str(REPORTS_DIR), recursive=False)
    observer.schedule(CronHandler(), str(CRONS_DIR), recursive=False)
    observer.start()

    load_all_crons()
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()

    cron_lines = []
    for name, jobs in _loaded_crons.items():
        if not jobs: continue
        cfg = json.loads((CRONS_DIR / f"{name}.json").read_text())
        cron_lines.append(f"  • {name} ({cfg.get('schedule','?')})")
    summary = "\n" + "\n".join(cron_lines) if cron_lines else " (없음)"
    send_telegram(f"🟢 IO 서버 시작됨\n📅 크론:{summary}")
    log.info("IO server started")

    try:
        telegram_polling()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop(); observer.join()

if __name__ == "__main__":
    main()
