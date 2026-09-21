# telegram-ai-bot

A Telegram bot that sits in a group chat, records conversation context, and answers questions about it using an LLM. Supports Google Gemini and Ollama (local models).

## What it does

- Listens passively in a group chat
- Records messages into a buffer when `/begin` is active
- When tagged (`@botname`), sends the buffered context + your question to an LLM
- Can run Python code and search the web to answer accurately
- Also works in private (DM) without needing any context setup

## Requirements

- Python 3.10+
- A Telegram bot token ([BotFather](https://t.me/BotFather))
- A Gemini API key (free at [aistudio.google.com](https://aistudio.google.com)) **or** a running [Ollama](https://ollama.com) instance

## Setup

```bash
git clone git@github.com:hkm67/telegram-ai-bot.git
cd telegram-ai-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your credentials:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
BOT_USERNAME=your_bot_username

LLM_BACKEND=ollama
OLLAMA_MODEL=gemma4:26b

GEMINI_API_KEY=your_key     # fallback if local Ollama fails
GEMINI_FALLBACK_MODEL=gemini-2.0-flash
LLM_MODEL=gemini-2.0-flash  # legacy fallback model setting
OLLAMA_HOST=http://your-ollama-host:11434  # set this only in your untracked .env
OLLAMA_NUM_CTX=65536
OLLAMA_THINK=false

ADMIN_USER_IDS=123456789    # comma-separated Telegram user IDs
```

Run:

```bash
python bot.py
```

## Docker

```bash
docker compose up -d
```

## Running as a systemd service (Linux / Raspberry Pi)

```bash
sudo cp tj-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tj-bot
sudo systemctl start tj-bot
```

Edit `tj-bot.service` to match your install path if needed.

## Commands

| Command | Who | Description |
|---|---|---|
| `/begin` | anyone | Start recording messages into the buffer |
| `/end` | anyone | Pause recording (buffer is kept) |
| `/clear` | anyone | Wipe the buffer and stop recording |
| `/status` | anyone | Show capture state and active model |
| `/model <spec>` | admin | Switch LLM model at runtime |

By default the bot uses the local Ollama server first. If an Ollama call fails and `GEMINI_API_KEY` is set, it retries with `GEMINI_FALLBACK_MODEL` and tells the chat which model answered.

### `/model` examples

```
/model gemini-2.0-flash
/model gemini-1.5-pro
/model ollama:gemma4:26b
/model ollama:qwen3-coder-next:latest
```

## Typical group usage

1. `/begin` — start recording
2. Chat normally (the bot listens but stays silent)
3. `/end` — pause recording
4. `@botname summarise the discussion` — bot replies using the buffered context
5. `/clear` — reset for a new session

## Project structure

```
bot.py          — Telegram handlers and entry point
agent.py        — Agentic loop (tool use, LLM calls)
config.py       — Config and backend switching
history.py      — Message buffer management
tools.py        — Tool definitions (Python runner, web search)
backends/
  gemini.py     — Google Gemini backend
  ollama.py     — Ollama backend
  base.py       — Shared types
```
