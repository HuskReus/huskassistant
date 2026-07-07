#!/usr/bin/env python3
"""
HuskAssistant — api.py  (cara PWA/HTTP del mismo cerebro)
Sirve la app (assistant.html) y expone /api/message, que llama al MISMO
handle_text() que usa Telegram. Cerebro único, caras intercambiables.

Ejecutar:  python api.py   ->  http://TU_VPS:8787
SEGURIDAD: no lo expongas desnudo a internet. Usa Tailscale o define
AGENT_API_TOKEN en .env y ponlo detrás de HTTPS.
"""

import os
from flask import Flask, request, jsonify, Response

import agent  # el cerebro consolidado

HERE = os.path.dirname(os.path.abspath(__file__))
API_TOKEN = os.environ.get("AGENT_API_TOKEN")

app = Flask(__name__)
mem = agent.MemoryStore(agent.CONFIG["db_path"])


@app.before_request
def _guard():
    if API_TOKEN and request.path.startswith("/api/"):
        if request.headers.get("Authorization", "") != f"Bearer {API_TOKEN}":
            return jsonify({"error": "unauthorized"}), 401


@app.route("/")
def index():
    with open(os.path.join(HERE, "assistant.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "HuskAssistant", "short_name": "HuskAssistant",
        "display": "standalone", "background_color": "#0d1421",
        "theme_color": "#0d1421", "start_url": "/", "icons": [],
    })


@app.route("/api/message", methods=["POST"])
def message():
    data = request.get_json(force=True, silent=True) or {}
    chat_id = str(data.get("chat_id", "pwa"))
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"reply": ""})
    reply = agent.handle_text(mem, chat_id, text)
    return jsonify({"reply": reply, "version": agent.VERSION})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8787)
