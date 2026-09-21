<div align="center">

<h1><code>llm_tg_bot</code></h1>

<h3>Local Telegram AI-agent on your own model. No cloud.</h3>

</div>

Telegram: [@cmonjustdoitbot](https://t.me/cmonjustdoitbot)

Source: https://github.com/Fakeonomics/llm_tg_bot

## Features

- Agent with tools — web search, page fetch, files, sandbox
- Fast-path: simple questions answered in 1 generation
- Real rich formatting (Bot API 10.1+) — tables, headings, lists, code, quotes, spoilers
- Private chats, groups (by mention), guest mode, inline
- Model vision (mmproj) + OCR fallback
- Any OpenAI-compatible API (local / OpenRouter / Together / NVIDIA…), models auto-fetched
- Second API as fallback (server-side env only)
- Fair per-user queue + anti-spam cooldowns, live progress, cancel by stop-word
- Access for everyone or admin-only, switchable at runtime

## Commands

| Command | Description |
|---|---|
| `/reset` | Clear chat context immediately |
| `/ctx` | Context usage |
| `/start` | Help with all commands |
| `/admin open\|close\|status` | Access for all / admin-only / status (admin) |
| `/models` | List models from the active API (admin) |
| `/setapi <url> [key]` | Switch backend (admin) |
| `/setmodel <id>` | Set model manually (admin) |
| `/setcd [private group]` | Anti-spam cooldowns (admin) |

Terminal:

| Command | Description |
|---|---|
| `tgbot [start\|stop\|restart\|status\|logs]` | Manage the bot |

## Install

```sh
git clone https://github.com/Fakeonomics/llm_tg_bot && cd llm_tg_bot
./scripts/install.sh
```

`install.sh` creates venvs, installs pinned deps, asks for `BOT_TOKEN` and admin ID, auto-detects the model port and remembers it. Then:

```sh
tgbot           # start (background)
tgbot status    # check
tgbot logs      # live log
```

Requirements: `python3`, a model on an OpenAI-compatible API (LM Studio / llama-server / hosted). Optional OCR: `tesseract`.

## Configuration

`.env` files (never committed, see `configs/*.example`):

| Key | Where | Meaning |
|---|---|---|
| `BOT_TOKEN` | `yymbot/.env`, `base/.env` | Token from [@BotFather](https://t.me/BotFather) |
| `ADMIN_LIST` | `yymbot/.env` | Admin Telegram ID |
| `whitelist` | `yymbot/.env` | Empty = open to all |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | env | Active backend (set via `/setapi`) |
| `EXTRA_BASE_URL` / `EXTRA_API_KEY` / `EXTRA_MODEL` | env, server only | Second API fallback |

## Layout

```text
bin/tgbot                    # launcher: start/stop/restart/status/logs
scripts/install.sh           # one-command setup
configs/*.example            # env templates
deployment/tgbot.service     # systemd unit example
yymbot/bot.py                # engine: private/groups/guests/inline
yymbot/smo_bridge.py         # bridge: queue, cooldowns, files, vision, delivery
base/src/services/smo_agent.py     # agent (CodeAgent + native LFM loop)
base/src/services/agent_loop.py    # delivery + privacy scrub
base/src/services/llm_config.py    # backend registry (any OpenAI-compatible API)
base/src/services/richdraft.py     # rich: markdown to JSON blocks
base/src/services/status.py        # live status, quiet net-error logging
```

## Notes

- Slow answer? Check `send_final via=` and `private lag/done` in logs: network vs model.
- Old Telegram clients may render rich tables as text — that's the client, not the bot.
