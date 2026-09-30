@echo off
REM HuskAssistant - radar de tokens nuevos en Windows
cd /d "%~dp0"

if not exist .env (
  copy .env.example .env >nul
  echo ^>^> Cree .env - pon tu TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID, guarda y vuelve a ejecutar radar.bat
  notepad .env
  exit /b
)

if not exist .venv (
  echo ^>^> Creando entorno...
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)

echo ^>^> Radar arrancado: revisa tokens nuevos cada 8 minutos. Cierra esta ventana para detenerlo.
.venv\Scripts\python huskagent.py %*
pause
