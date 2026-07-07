@echo off
REM HuskAssistant - arranque en Windows
cd /d "%~dp0"

if not exist .env (
  copy .env.example .env >nul
  echo ^>^> Cree .env - editalo con tus tokens y vuelve a ejecutar start.bat
  notepad .env
  exit /b
)

if not exist .venv (
  echo ^>^> Creando entorno...
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)

echo ^>^> Arrancando cerebro + API (PWA en puerto 8787)...
start "HuskAssistant-API" .venv\Scripts\python api.py
.venv\Scripts\python agent.py
