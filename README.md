# HuskAssistant v1.0.0

Asistente personal autónomo. Cerebro en tu VPS (o PC), caras en Telegram y PWA.
Bucle: Percibir → Razonar → Actuar → Aprender.

## Arranque rápido (3 pasos)

1. **Descomprime** la carpeta donde quieras (VPS recomendado; Windows también sirve).
2. **Corre el arranque** — crea el entorno e instala todo solo:
   - Linux/VPS: `./start.sh`
   - Windows: doble clic a `start.bat`
3. La primera vez te crea el archivo `.env`: pon tu `TELEGRAM_BOT_TOKEN` y tu
   `TELEGRAM_CHAT_ID` (eso te hace el **dueño**), guarda, y vuelve a correr.

Listo: escríbele a tu bot por Telegram, o abre `http://TU_IP:8787` en el celular
y "Añadir a pantalla de inicio" para tenerla como app con micrófono.

`ANTHROPIC_API_KEY` en `.env` es opcional pero recomendado: sin ella funciona en
modo básico (palabras clave); con ella entiende lenguaje natural.

## Extras opcionales

```
./.venv/bin/pip install -r requirements-extras.txt
```
Activa: búsqueda web (ddgs), memoria semántica de largo plazo
(sentence-transformers, baja ~90MB la primera vez) y transcripción de notas de
voz de Telegram (whisper; necesita `ffmpeg` instalado en el sistema).

## Qué sabe hacer

- Conversar, recomendar (películas, etc.) y **buscar en internet** datos actuales
- Recordatorios ("recuérdame X mañana a las 9") y aviso proactivo cuando vencen
- **Briefing diario automático** a la hora que fijes (`BRIEFING_HOUR_UTC`)
- Estado de tus proyectos: radar de tokens (lee `huskagent.db`) y Polymarket (stub por cablear)
- Personalización: "llámame X", estilos, preferencias — y aprende tus hábitos de uso
- Notas de voz por Telegram y micrófono en la PWA
- `estado` / `version` → te dice su versión y qué módulos tiene activos

## Actualizaciones (así de simple)

1. Recibes un archivo mejorado (`agent.py`, `api.py` o `assistant.html`).
2. Se lo mandas **como documento por Telegram** al bot.
3. El bot (solo si eres el dueño) valida la sintaxis, te pide confirmar con "sí",
   hace **backup** en `backups/`, aplica y se reinicia solo.
4. ¿Salió mal? Escríbele `rollback agent.py` y vuelve a la versión anterior.

## Compartir con otra persona

- **Su propio asistente (recomendado):** pásale esta carpeta (SIN tu `.env` ni
  los `.db`). Corre `start.sh`/`start.bat` con SUS tokens. Independiente total.
- **Tu instancia como invitado:** agrega su chat_id en `GUEST_CHAT_IDS` del `.env`.
  Podrá chatear, buscar y usar recordatorios, pero NO tocar tus bots ni enviarte
  actualizaciones — eso es solo del dueño.

## Correr 24/7 en el VPS

Rápido: `tmux new -s husk` → `./start.sh` → `Ctrl+B, D` para soltar.
Serio: servicio systemd (pídelo y te doy el archivo).

## Seguridad

- El agente **nunca ejecuta capital**; acciones con consecuencias piden "sí".
- Actualizaciones y skills sensibles: **solo el dueño** (tu chat_id).
- La PWA: no expongas el puerto 8787 abierto a internet. Usa Tailscale
  (recomendado) o define `AGENT_API_TOKEN` y ponla detrás de HTTPS.
- No compartas tu `.env` ni los archivos `.db` (ahí viven tus llaves y tu memoria).

## Archivos

| Archivo          | Qué es                                          |
|------------------|--------------------------------------------------|
| `agent.py`       | El cerebro (Telegram, skills, memoria, updates)  |
| `api.py`         | La cara HTTP que sirve la PWA                    |
| `assistant.html` | La app instalable del celular                    |
| `huskagent.py`   | Tu radar de tokens (independiente, misma carpeta)|
| `estrategia.py`  | Estrategia de tendencia BTC/ETH: backtest, señal diaria y seguimiento en papel (ver `ESTRATEGIA.md`) |
| `start.sh/.bat`  | Arranque en un comando                           |
| `.env`           | Tus llaves (créalo desde `.env.example`)         |
