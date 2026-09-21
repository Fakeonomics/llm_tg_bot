#!/bin/sh
# One-command setup: ./install.sh
# Creates venvs, installs pinned deps, prepares .env, links commands.
# You only type BOT_TOKEN + admin ID once when asked.
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "== llm_tg_bot setup =="
command -v python3 >/dev/null || { echo "need python3"; exit 1; }
python3 --version

echo "[1/6] venvs..."
[ -d "$HERE/yymbot/yymenv" ] || python3 -m venv "$HERE/yymbot/yymenv"
[ -d "$HERE/botenv" ] || python3 -m venv "$HERE/botenv"

echo "[2/6] deps (pinned, takes a while)..."
"$HERE/yymbot/yymenv/bin/pip" install -q -r "$HERE/requirements-yym.txt"
"$HERE/botenv/bin/pip" install -q -r "$HERE/requirements-base.txt"

echo "[3/6] env files..."
[ -f "$HERE/yymbot/.env" ] || cp "$HERE/configs/yymbot.env.example" "$HERE/yymbot/.env"
[ -f "$HERE/base/.env" ] || cp "$HERE/configs/base.env.example" "$HERE/base/.env"

echo "[4/6] token + admin..."
printf "BOT_TOKEN (from @BotFather): "
read -r TOK
printf "Admin Telegram ID: "
read -r ADM
if [ -n "$TOK" ]; then
    sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$TOK|" "$HERE/yymbot/.env" "$HERE/base/.env"
fi
if [ -n "$ADM" ]; then
    sed -i "s|^ADMIN_LIST=.*|ADMIN_LIST=$ADM|" "$HERE/yymbot/.env"
fi

echo "[5/6] model port (auto-detect + remember)..."
PORT=""
for p in 8080 1234 11434 8000; do
    code=$(curl -s --max-time 3 -o /dev/null -w "%{http_code}" \
        "http://localhost:$p/v1/models" 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then PORT="$p"; break; fi
done
if [ -n "$PORT" ]; then
    echo "$PORT" > "$HERE/.llama-port"
    echo "model on :$PORT (remembered)"
else
    echo "no model detected now - tgbot will scan on start"
fi

echo "[6/6] command links..."
mkdir -p "$HOME/.local/bin"
ln -sf "$HERE/bin/tgbot" "$HOME/.local/bin/tgbot"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "NOTE: add to PATH: export PATH=\"\$HOME/.local/bin:\$PATH\"";;
esac

echo ""
echo "Done. Next:"
echo "  1. Serve a model on :8080 (LM Studio / llama-server)"
echo "  2. tgbot            # start the bot"
echo "  3. tgbot status     # check, tgbot logs = live log"
echo "Optional OCR: sudo dnf install tesseract tesseract-langpack-rus"
