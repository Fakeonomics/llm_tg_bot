# AGENTS.md — guide for AI contributors

## Layout

- `yymbot/bot.py` — Telegram engine (PTB): handlers, commands, guest/inline.
- `yymbot/smo_bridge.py` — bridge: per-user queue/cooldowns, files, vision, delivery.
- `base/src/services/` — agent (`smo_agent.py`), backends (`llm_config.py`),
  rich blocks (`richdraft.py`), live status (`status.py`).
- `bin/`, `scripts/`, `configs/`, `deployment/` — ops. No code logic there.

## Rules

- Never commit secrets (`*.env`, tokens, keys). Only `.example` templates.
- User-facing strings in English; answers match the user's language.
- No emoji in code or messages.
- Delivery chain: rich blocks → rich markdown → plain. Never break fallbacks.
- Every network call gets an explicit timeout; storm-quiet logging only.
- Verify with `py_compile` + focused offline tests before pushing.
