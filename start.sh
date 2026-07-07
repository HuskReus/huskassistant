#!/usr/bin/env bash
# HuskAssistant - arranque en Linux/VPS
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo ">> Cree .env - edítalo con tus tokens (nano .env) y vuelve a correr ./start.sh"
  exit 1
fi

if [ ! -d .venv ]; then
  echo ">> Creando entorno..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
  echo ">> Extras opcionales: ./.venv/bin/pip install -r requirements-extras.txt"
fi

echo ">> Arrancando cerebro (Telegram) + API (PWA en puerto 8787)..."
./.venv/bin/python api.py &
API_PID=$!
trap "kill $API_PID 2>/dev/null" EXIT
./.venv/bin/python agent.py
