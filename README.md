# claude-io

Telegram ↔ Claude Code IO Server

텔레그램 메시지를 Claude Code tmux 세션으로 전달하고, 응답을 실시간으로 다시 텔레그램으로 전송하는 브릿지 서버.

## 구조

```
텔레그램 메시지
    ↓
io_server.py (polling)
    ↓ tmux paste-buffer
tmux session "claude-agent" (Claude Code)
    ↓
일반 대화: capture-pane streaming → editMessageText
크론 태스크: reports/ 파일 저장 → sendMessage
```

## 설치

```bash
git clone ...
cd claude-io
pip install -r requirements.txt
cp config.sample.json config.json  # 편집 필요
```

## 설정

`config.json`:
```json
{
  "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "chat_id": 123456789,
  "tmux_session": "claude-agent",
  "worker_timeout": 300,
  "stable_secs": 3
}
```

## 실행

```bash
# 1. tmux 세션에서 Claude Code 실행
tmux new-session -d -s claude-agent
tmux send-keys -t claude-agent 'cd ~/workspace && claude --dangerously-skip-permissions' Enter

# 2. IO 서버 실행
python3 io_server.py
```

## 크론 태스크

`crons/` 폴더에 JSON 파일 추가:

```json
{
  "name": "llm-news",
  "schedule": "07:00",
  "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
  "enabled": true,
  "prompt": "최신 LLM 뉴스를 요약해줘..."
}
```

파일 추가/수정/삭제 시 서버 재시작 없이 자동 반영.

## 디렉토리

```
claude-io/
├── io_server.py        # 메인 서버
├── config.json         # 설정 (gitignore)
├── config.sample.json  # 설정 샘플
├── crons/              # 크론 태스크 정의
├── reports/            # 크론 결과 저장 (gitignore)
└── logs/               # 세션 로그 (gitignore)
```
