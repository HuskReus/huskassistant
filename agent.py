#!/usr/bin/env python3
"""
HuskAssistant — agent.py  (cerebro consolidado)
================================================================================
Bucle: Percibir -> Razonar -> Actuar -> Aprender.
Todo integrado: personalización, memoria semántica, búsqueda web, multi-usuario,
briefing proactivo, voz por Telegram y AUTO-ACTUALIZACIÓN segura.

Cómo se actualiza:
  1) Recibes un archivo nuevo (agent.py / api.py / assistant.html).
  2) Se lo mandas como DOCUMENTO por Telegram al bot.
  3) Solo si eres el DUEÑO: valida sintaxis -> pide confirmación -> backup ->
     aplica -> se reinicia solo.  "rollback agent.py" deshace la última.

Config: archivo .env junto a este script (ver .env.example). Sin dependencias
obligatorias más allá de requests (+flask para la PWA). Whisper, embeddings y
búsqueda web son opcionales y degradan con gracia si no están instalados.

REGLA DURA: este agente NUNCA ejecuta capital ni compras. Acciones con
consecuencias piden confirmación explícita. Punto.
================================================================================
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import logging
import tempfile
import py_compile
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional, Callable

import requests

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
STARTED = dt.datetime.utcnow()

# --------------------------------------------------------------------------- #
# .env — carga simple, sin dependencias
# --------------------------------------------------------------------------- #
def _load_env():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

CONFIG = {
    "db_path": os.path.join(HERE, "assistant.db"),
    "radar_db_path": os.path.join(HERE, "huskagent.db"),
    "backup_dir": os.path.join(HERE, "backups"),
    "poll_seconds": 3,
    "llm_model": "claude-haiku-4-5-20251001",
    "whisper_model": "base",
    "briefing_hour_utc": int(os.environ.get("BRIEFING_HOUR_UTC", "13")),
}

OWNER_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# Vacío = solo el dueño. Añade chat_ids de invitados separados por coma en .env:
# GUEST_CHAT_IDS=111111,222222
ALLOWED: set[str] = {
    c.strip() for c in os.environ.get("GUEST_CHAT_IDS", "").split(",") if c.strip()
}

UPDATABLE_FILES = {"agent.py", "api.py", "assistant.html"}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("huskassistant")


# --------------------------------------------------------------------------- #
# MODELOS
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    kind: str               # "message" | "reminder_due" | "document"
    text: str
    chat_id: str
    raw: dict = field(default_factory=dict)


@dataclass
class Skill:
    name: str
    description: str
    handler: Callable[[dict, "MemoryStore"], str]
    requires_confirmation: bool = False
    owner_only: bool = False


# --------------------------------------------------------------------------- #
# MEMORIA (SQLite): conversación, recordatorios, confirmaciones, perfil,
# hábitos, memoria de largo plazo y kv.
# --------------------------------------------------------------------------- #
class MemoryStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages  (ts TEXT, chat_id TEXT, role TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT, text TEXT, due_ts TEXT, fired INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS pending   (chat_id TEXT PRIMARY KEY,
                skill TEXT, args_json TEXT, ts TEXT);
            CREATE TABLE IF NOT EXISTS profile   (chat_id TEXT, k TEXT, v TEXT,
                PRIMARY KEY(chat_id, k));
            CREATE TABLE IF NOT EXISTS usage     (chat_id TEXT, skill TEXT, ts TEXT, hour INTEGER);
            CREATE TABLE IF NOT EXISTS ltm       (chat_id TEXT, ts TEXT, role TEXT,
                text TEXT, emb TEXT);
            CREATE TABLE IF NOT EXISTS kv        (k TEXT PRIMARY KEY, v TEXT);
        """)
        self.conn.commit()

    # conversación corta
    def log_msg(self, chat_id, role, text):
        self.conn.execute("INSERT INTO messages VALUES (?,?,?,?)",
                          (dt.datetime.utcnow().isoformat(), chat_id, role, text))
        self.conn.commit()

    def recent_history(self, chat_id, n=8):
        rows = self.conn.execute(
            "SELECT role, text FROM messages WHERE chat_id=? ORDER BY rowid DESC LIMIT ?",
            (chat_id, n)).fetchall()
        return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]

    # recordatorios
    def add_reminder(self, chat_id, text, due_iso):
        self.conn.execute("INSERT INTO reminders (chat_id,text,due_ts) VALUES (?,?,?)",
                          (chat_id, text, due_iso))
        self.conn.commit()

    def due_reminders(self):
        now = dt.datetime.utcnow().isoformat()
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE fired=0 AND due_ts<=?", (now,)).fetchall()
        for r in rows:
            self.conn.execute("UPDATE reminders SET fired=1 WHERE id=?", (r["id"],))
        self.conn.commit()
        return rows

    def upcoming_reminders(self, chat_id):
        return self.conn.execute(
            "SELECT * FROM reminders WHERE chat_id=? AND fired=0 ORDER BY due_ts",
            (chat_id,)).fetchall()

    # confirmaciones pendientes
    def set_pending(self, chat_id, skill, args):
        self.conn.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?)",
                          (chat_id, skill, json.dumps(args),
                           dt.datetime.utcnow().isoformat()))
        self.conn.commit()

    def pop_pending(self, chat_id):
        row = self.conn.execute("SELECT * FROM pending WHERE chat_id=?",
                                (chat_id,)).fetchone()
        if not row:
            return None
        self.conn.execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))
        self.conn.commit()
        # confirmaciones caducan a los 10 min (evita "sí" accidentales tardíos)
        age = dt.datetime.utcnow() - dt.datetime.fromisoformat(row["ts"])
        if age.total_seconds() > 600:
            return None
        return row["skill"], json.loads(row["args_json"])

    # kv
    def kv_get(self, k, default=None):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, v))
        self.conn.commit()


# --------------------------------------------------------------------------- #
# PERSONALIZACIÓN
# --------------------------------------------------------------------------- #
DEFAULT_PROFILE = {
    "name": "amigo/a", "lang": "español", "tz": "UTC",
    "style": "directo y breve, sin relleno",
    "projects": "",
    "rules": "NUNCA ejecutar capital ni compras sin confirmación explícita.",
}

def get_profile(mem, chat_id):
    prof = dict(DEFAULT_PROFILE)
    if chat_id == OWNER_CHAT_ID:
        prof.update({"name": "HuskReus",
                     "projects": "HuskAgent (radar de tokens) y bot Polymarket (clima) en VPS"})
    for r in mem.conn.execute("SELECT k,v FROM profile WHERE chat_id=?", (chat_id,)):
        prof[r["k"]] = r["v"]
    return prof

def profile_context(mem, chat_id):
    p = get_profile(mem, chat_id)
    habits = usage_summary(mem, chat_id)
    out = ["PERFIL DEL USUARIO (personaliza tono y contenido):",
           f"- Nombre: {p['name']}. Idioma: {p['lang']}. TZ: {p['tz']}.",
           f"- Estilo: {p['style']}."]
    if p["projects"]:
        out.append(f"- Proyectos: {p['projects']}.")
    out.append(f"- REGLAS DURAS: {p['rules']}")
    if habits:
        out.append(f"HÁBITOS OBSERVADOS: {habits}")
    return "\n".join(out)

def record_usage(mem, chat_id, skill):
    now = dt.datetime.utcnow()
    mem.conn.execute("INSERT INTO usage VALUES (?,?,?,?)",
                     (chat_id, skill, now.isoformat(), now.hour))
    mem.conn.commit()

def usage_summary(mem, chat_id):
    rows = mem.conn.execute(
        "SELECT skill, hour FROM usage WHERE chat_id=? AND skill!='chat'",
        (chat_id,)).fetchall()
    if len(rows) < 4:
        return ""
    freq = {}
    for r in rows:
        freq[r["skill"]] = freq.get(r["skill"], 0) + 1
    top = ", ".join(f"{s}(x{n})" for s, n in
                    sorted(freq.items(), key=lambda x: -x[1])[:3])
    return f"skills más usados: {top}"


# --------------------------------------------------------------------------- #
# MEMORIA SEMÁNTICA (opcional: pip install sentence-transformers numpy)
# --------------------------------------------------------------------------- #
_EMB_MODEL = None

def _emb_model():
    global _EMB_MODEL
    if _EMB_MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
            _EMB_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception:
            _EMB_MODEL = False
    return _EMB_MODEL

def remember(mem, chat_id, role, text):
    if not text or len(text) < 8:
        return
    m = _emb_model()
    emb = m.encode(text, normalize_embeddings=True).tolist() if m else None
    mem.conn.execute("INSERT INTO ltm VALUES (?,?,?,?,?)",
                     (chat_id, dt.datetime.utcnow().isoformat(), role, text,
                      json.dumps(emb) if emb else None))
    mem.conn.commit()

def recall_context(mem, chat_id, query, k=4):
    rows = mem.conn.execute(
        "SELECT ts, role, text, emb FROM ltm WHERE chat_id=?", (chat_id,)).fetchall()
    if not rows:
        return ""
    m = _emb_model()
    if m:
        import numpy as np
        qv = np.array(m.encode(query, normalize_embeddings=True))
        scored = [(float(qv @ np.array(json.loads(r["emb"]))), r)
                  for r in rows if r["emb"]]
    else:  # fallback por palabras
        qw = set(query.lower().split())
        scored = [(len(qw & set(r["text"].lower().split())), r) for r in rows]
    scored.sort(key=lambda x: -x[0])
    hits = [r for s, r in scored[:k] if s > 0]
    if not hits:
        return ""
    return "MEMORIAS RELEVANTES DE ANTES (usa solo si vienen al caso):\n" + \
        "\n".join(f"- [{h['ts'][:10]}] {h['role']}: {h['text'][:200]}" for h in hits)


# --------------------------------------------------------------------------- #
# SKILLS
# --------------------------------------------------------------------------- #
def skill_radar_status(args, mem):
    path = CONFIG["radar_db_path"]
    if not os.path.exists(path):
        return "No encuentro la base del radar (huskagent.db) junto al agente."
    try:
        c = sqlite3.connect(path); c.row_factory = sqlite3.Row
        pend = c.execute("SELECT COUNT(*) n FROM decisions WHERE outcome='pending' "
                         "AND action='alert'").fetchone()["n"]
        recent = c.execute("SELECT symbol, confidence FROM decisions WHERE "
                           "action='alert' ORDER BY rowid DESC LIMIT 5").fetchall()
        res = c.execute("SELECT outcome, COUNT(*) n FROM decisions WHERE outcome "
                        "IN ('hit','miss') GROUP BY outcome").fetchall()
        c.close()
        hits = next((r["n"] for r in res if r["outcome"] == "hit"), 0)
        tot = hits + next((r["n"] for r in res if r["outcome"] == "miss"), 0)
        rate = f"{hits/tot:.0%}" if tot else "sin datos aún"
        top = ", ".join(f"{r['symbol']}({r['confidence']:.0%})" for r in recent) or "ninguna"
        return (f"📡 Radar: {pend} alertas activas. Acierto histórico: {rate} "
                f"({hits}/{tot}). Últimas: {top}.")
    except Exception as e:
        return f"No pude leer el radar: {e}"


def skill_polymarket_status(args, mem):
    return ("🌦️ Polymarket: (por cablear) — aquí se lee el estado del bot de clima "
            "en el VPS: posiciones, PnL, última corrida.")


def skill_polymarket_control(args, mem):
    action = args.get("action", "desconocida")
    return f"✅ (simulado) Ejecuté '{action}' sobre el bot de Polymarket."


def skill_set_reminder(args, mem):
    text, due = args.get("text", "recordatorio"), args.get("due_iso")
    if not due:
        return "¿Para cuándo? Dime fecha y hora."
    try:
        when = dt.datetime.fromisoformat(due).strftime("%d/%m %H:%M")
    except ValueError:
        return "No entendí la fecha. Dímela de nuevo (ej: mañana a las 9pm)."
    mem.add_reminder(args["_chat_id"], text, due)
    return f"⏰ Anotado: \"{text}\" para el {when} UTC."


def skill_list_reminders(args, mem):
    rows = mem.upcoming_reminders(args["_chat_id"])
    if not rows:
        return "No tienes recordatorios pendientes."
    return "Tus recordatorios:\n" + "\n".join(
        f"• {r['text']} — {dt.datetime.fromisoformat(r['due_ts']).strftime('%d/%m %H:%M')}"
        for r in rows)


def skill_web_search(args, mem):
    query = (args.get("query") or "").strip()
    if not query:
        return "¿Qué busco?"
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as d:
            results = list(d.text(query, max_results=5))
    except ImportError:
        return "Búsqueda web no instalada (pip install ddgs)."
    except Exception as e:
        return f"La búsqueda falló: {e}"
    if not results:
        return f"Sin resultados para: {query}"
    snippets = "\n".join(f"- {r.get('title','')}: {r.get('body','')[:200]} "
                         f"({r.get('href','')})" for r in results)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return f"🔎 \"{query}\":\n{snippets}"
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CONFIG["llm_model"], "max_tokens": 400,
                  "system": ("Responde en español, breve. Usa SOLO estos resultados; "
                             "no inventes. Cita la fuente al final."),
                  "messages": [{"role": "user",
                                "content": f"Pregunta: {query}\n\nResultados:\n{snippets}"}]},
            timeout=30)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])
    except Exception:
        return f"🔎 \"{query}\":\n{snippets}"


def skill_set_preference(args, mem):
    key = (args.get("key") or "").strip().lower()
    value = (args.get("value") or "").strip()
    valid = {"name", "lang", "tz", "style", "projects", "rules"}
    if key not in valid or not value:
        return f"Campos válidos: {', '.join(sorted(valid))}."
    # los invitados no pueden relajar las reglas de seguridad
    if key == "rules" and args["_chat_id"] != OWNER_CHAT_ID:
        return "Solo el dueño puede cambiar las reglas."
    mem.conn.execute("INSERT OR REPLACE INTO profile VALUES (?,?,?)",
                     (args["_chat_id"], key, value))
    mem.conn.commit()
    return f"Listo: {key} = \"{value}\"."


def skill_show_profile(args, mem):
    p = get_profile(mem, args["_chat_id"])
    habits = usage_summary(mem, args["_chat_id"]) or "sin patrón todavía"
    return ("👤 Configuración:\n"
            f"• Nombre: {p['name']}\n• Idioma: {p['lang']} · TZ: {p['tz']}\n"
            f"• Estilo: {p['style']}\n• Proyectos: {p['projects'] or '—'}\n"
            f"• Reglas: {p['rules']}\n• Hábitos: {habits}")


def skill_briefing(args, mem):
    p = get_profile(mem, args["_chat_id"])
    hour = dt.datetime.utcnow().hour
    saludo = ("Buenos días" if 5 <= hour < 12 else
              "Buenas tardes" if 12 <= hour < 19 else "Buenas noches")
    parts = [f"{saludo}, {p['name']}."]
    if args["_chat_id"] == OWNER_CHAT_ID:
        parts.append(skill_radar_status(args, mem))
        parts.append(skill_polymarket_status(args, mem))
    ups = mem.upcoming_reminders(args["_chat_id"])
    if ups:
        parts.append(f"⏰ {len(ups)} recordatorio(s) pendiente(s).")
    return "\n".join(parts)


def skill_status(args, mem):
    up = dt.datetime.utcnow() - STARTED
    h, rem = divmod(int(up.total_seconds()), 3600)
    m = rem // 60
    extras = []
    extras.append("memoria semántica ✅" if _emb_model() else "memoria semántica ➖ (fallback palabras)")
    try:
        import ddgs  # noqa: F401
        extras.append("búsqueda web ✅")
    except ImportError:
        try:
            import duckduckgo_search  # noqa: F401
            extras.append("búsqueda web ✅")
        except ImportError:
            extras.append("búsqueda web ➖")
    extras.append("LLM ✅" if os.environ.get("ANTHROPIC_API_KEY") else "LLM ➖ (modo heurístico)")
    return (f"🛰️ HuskAssistant v{VERSION} · activo {h}h {m}m\n"
            + " · ".join(extras)
            + "\nPara actualizarme: mándame el archivo nuevo como documento.")


def skill_chat(args, mem):
    return args.get("_llm_reply") or "Estoy aquí. ¿En qué te ayudo?"


SKILLS: dict[str, Skill] = {s.name: s for s in [
    Skill("radar_status", "Estado del radar de tokens (HuskAgent): alertas, aciertos.",
          skill_radar_status, owner_only=True),
    Skill("polymarket_status", "Estado del bot de Polymarket/clima en el VPS.",
          skill_polymarket_status, owner_only=True),
    Skill("polymarket_control", "Controlar el bot de Polymarket (pausar/reanudar).",
          skill_polymarket_control, requires_confirmation=True, owner_only=True),
    Skill("set_reminder", "Crear recordatorio. args: text, due_iso (ISO 8601 UTC).",
          skill_set_reminder),
    Skill("list_reminders", "Listar recordatorios pendientes.", skill_list_reminders),
    Skill("web_search", "Buscar en internet: datos actuales, películas, noticias, "
          "precios, cualquier cosa que no sepas. args: query.", skill_web_search),
    Skill("set_preference", "Guardar preferencia del usuario. args: key, value.",
          skill_set_preference),
    Skill("show_profile", "Mostrar cómo está configurado el usuario.", skill_show_profile),
    Skill("briefing", "Resumen del día: proyectos y recordatorios.", skill_briefing),
    Skill("status", "Versión y estado del propio asistente.", skill_status),
    Skill("chat", "Conversación general, recomendaciones de memoria, preguntas "
          "abiertas. Usar si nada más encaja.", skill_chat),
]}


# --------------------------------------------------------------------------- #
# CONTROL DE ACCESO
# --------------------------------------------------------------------------- #
def is_allowed(chat_id: str) -> bool:
    cid = str(chat_id)
    if cid == OWNER_CHAT_ID:
        return True
    return cid in ALLOWED

def can_use(chat_id: str, skill_name: str) -> bool:
    if str(chat_id) == OWNER_CHAT_ID:
        return True
    return not SKILLS[skill_name].owner_only


# --------------------------------------------------------------------------- #
# AUTO-ACTUALIZACIÓN (solo dueño, con validación, backup y rollback)
# --------------------------------------------------------------------------- #
def _backup(filename: str) -> Optional[str]:
    src = os.path.join(HERE, filename)
    if not os.path.exists(src):
        return None
    os.makedirs(CONFIG["backup_dir"], exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(CONFIG["backup_dir"], f"{filename}.{stamp}")
    shutil.copy2(src, dst)
    return dst

def stage_update(mem, chat_id: str, filename: str, content: bytes) -> str:
    """Valida y deja la actualización lista; pide confirmación para aplicar."""
    if chat_id != OWNER_CHAT_ID:
        return "⛔ Solo el dueño puede enviar actualizaciones."
    if filename not in UPDATABLE_FILES:
        return f"⛔ Solo acepto: {', '.join(sorted(UPDATABLE_FILES))}."
    staging = os.path.join(HERE, f".staged_{filename}")
    with open(staging, "wb") as f:
        f.write(content)
    if filename.endswith(".py"):
        try:
            py_compile.compile(staging, doraise=True)
        except py_compile.PyCompileError as e:
            os.unlink(staging)
            return f"❌ El archivo tiene errores de sintaxis, no lo aplico:\n{e.msg[:300]}"
    mem.set_pending(chat_id, "__apply_update__", {"filename": filename})
    return (f"📦 Actualización de *{filename}* validada ({len(content)} bytes).\n"
            f"Haré backup del actual y me reiniciaré. Responde 'sí' para aplicar.")

def apply_update(mem, chat_id: str, filename: str) -> str:
    staging = os.path.join(HERE, f".staged_{filename}")
    if not os.path.exists(staging):
        return "No hay actualización preparada."
    bak = _backup(filename)
    shutil.move(staging, os.path.join(HERE, filename))
    msg = f"✅ {filename} actualizado. Backup: {os.path.basename(bak) if bak else '—'}."
    if filename == "agent.py":
        telegram_send(chat_id, msg + " Reiniciando…")
        log.info("Reiniciando por actualización de agent.py")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    return msg

def rollback(chat_id: str, filename: str) -> str:
    if chat_id != OWNER_CHAT_ID:
        return "⛔ Solo el dueño puede hacer rollback."
    if filename not in UPDATABLE_FILES:
        return f"Archivos válidos: {', '.join(sorted(UPDATABLE_FILES))}."
    bdir = CONFIG["backup_dir"]
    if not os.path.isdir(bdir):
        return "No hay backups."
    baks = sorted(f for f in os.listdir(bdir) if f.startswith(filename + "."))
    if not baks:
        return f"No hay backups de {filename}."
    shutil.copy2(os.path.join(bdir, baks[-1]), os.path.join(HERE, filename))
    msg = f"↩️ Restaurado {filename} desde {baks[-1]}."
    if filename == "agent.py":
        telegram_send(chat_id, msg + " Reiniciando…")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    return msg


# --------------------------------------------------------------------------- #
# RAZONAR
# --------------------------------------------------------------------------- #
def _is_confirmation(text: str) -> bool:
    return text.strip().lower() in {"sí", "si", "confirmar", "dale", "hazlo", "ok", "yes"}

def reason(ev: Event, mem: MemoryStore):
    text = ev.text.strip()
    tl = text.lower()

    # comandos directos (no requieren LLM)
    if tl.startswith("rollback"):
        parts = text.split()
        fname = parts[1] if len(parts) > 1 else "agent.py"
        return "chat", {"_chat_id": ev.chat_id,
                        "_llm_reply": rollback(ev.chat_id, fname)}
    if tl in {"version", "versión", "estado", "status"}:
        return "status", {"_chat_id": ev.chat_id}

    # confirmación pendiente
    if _is_confirmation(text):
        pend = mem.pop_pending(ev.chat_id)
        if pend:
            skill, args = pend
            args["_chat_id"] = ev.chat_id
            if skill == "__apply_update__":
                return "chat", {"_chat_id": ev.chat_id,
                                "_llm_reply": apply_update(mem, ev.chat_id,
                                                           args["filename"])}
            return skill, {**args, "_confirmed": True}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return _route_with_llm(ev, mem, api_key)
    return _route_heuristic(ev)


def _route_heuristic(ev: Event):
    t = ev.text.lower()
    cid = ev.chat_id
    if any(w in t for w in ["radar", "token", "alertas", "memeradar"]):
        return "radar_status", {"_chat_id": cid}
    if "polymarket" in t or "clima" in t:
        return "polymarket_status", {"_chat_id": cid}
    if any(w in t for w in ["busca", "búscame", "buscame", "googlea"]):
        q = ev.text
        for w in ["busca ", "búscame ", "buscame ", "googlea "]:
            if w in t:
                q = ev.text[t.index(w) + len(w):]
                break
        return "web_search", {"_chat_id": cid, "query": q}
    if any(w in t for w in ["recuérdame", "recuerdame", "recordatorio"]):
        return "set_reminder", {"_chat_id": cid, "text": ev.text, "due_iso": None}
    if "resumen" in t or "briefing" in t:
        return "briefing", {"_chat_id": cid}
    return "chat", {"_chat_id": cid, "_llm_reply": None}


def _route_with_llm(ev: Event, mem: MemoryStore, api_key: str):
    catalog = "\n".join(f"- {s.name}: {s.description}" for s in SKILLS.values()
                        if can_use(ev.chat_id, s.name))
    now = dt.datetime.utcnow().isoformat()
    system_parts = [profile_context(mem, ev.chat_id)]
    mems = recall_context(mem, ev.chat_id, ev.text)
    if mems:
        system_parts.append(mems)
    system_parts.append(
        "Eres el orquestador de un asistente personal. Elige UN skill del catálogo "
        "y extrae sus argumentos. Si es charla, pregunta general o una recomendación "
        "que puedas dar de memoria (ej. una película), usa 'chat' con tu respuesta en "
        "'reply'. Si piden datos actuales o verificables de internet, usa 'web_search'. "
        f"Fechas en ISO 8601 UTC (ahora: {now}). Responde SOLO JSON: "
        '{"skill":"...","args":{...},"reply":"(solo si skill=chat)"}\n\n'
        f"CATÁLOGO:\n{catalog}")
    messages = mem.recent_history(ev.chat_id) + [{"role": "user", "content": ev.text}]
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CONFIG["llm_model"], "max_tokens": 600,
                  "system": "\n\n".join(system_parts), "messages": messages},
            timeout=30)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        parsed = json.loads(text.replace("```json", "").replace("```", "").strip())
        skill = parsed.get("skill", "chat")
        args = parsed.get("args", {}) or {}
        args["_chat_id"] = ev.chat_id
        if skill == "chat":
            args["_llm_reply"] = parsed.get("reply", "")
        return (skill if skill in SKILLS else "chat"), args
    except Exception as e:
        log.warning("Ruteo LLM falló (%s), uso heurística", e)
        return _route_heuristic(ev)


# --------------------------------------------------------------------------- #
# ACTUAR + APRENDER  (núcleo compartido por Telegram y la PWA)
# --------------------------------------------------------------------------- #
def handle_text(mem: MemoryStore, chat_id: str, text: str) -> str:
    """Punto de entrada único: lo usan Telegram y la API/PWA por igual."""
    if not is_allowed(chat_id):
        return "⛔ Este asistente es privado. Pide acceso al dueño."
    ev = Event("message", text, chat_id)
    mem.log_msg(chat_id, "user", text)
    remember(mem, chat_id, "user", text)

    skill_name, args = reason(ev, mem)

    if not can_use(chat_id, skill_name):
        reply = "⛔ Ese skill es solo del dueño."
    else:
        skill = SKILLS[skill_name]
        if skill.requires_confirmation and not args.get("_confirmed"):
            mem.set_pending(chat_id, skill_name, args)
            reply = (f"⚠️ Esto tiene consecuencias: {skill.description}\n"
                     f"Responde 'sí' para confirmar.")
        else:
            try:
                reply = skill.handler(args, mem)
            except Exception as e:
                log.exception("Skill %s falló", skill_name)
                reply = f"El skill {skill_name} falló: {e}"
            record_usage(mem, chat_id, skill_name)

    mem.log_msg(chat_id, "assistant", reply)
    remember(mem, chat_id, "assistant", reply)
    return reply


# --------------------------------------------------------------------------- #
# TELEGRAM I/O  (texto, voz y documentos de actualización)
# --------------------------------------------------------------------------- #
def _tg_base():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    return f"https://api.telegram.org/bot{token}" if token else None

def telegram_send(chat_id, text):
    base = _tg_base()
    if not base:
        log.info("[telegram off] -> %s", text.replace("\n", " | "))
        return
    try:
        r = requests.post(f"{base}/sendMessage",
                          json={"chat_id": chat_id, "text": text,
                                "parse_mode": "Markdown"}, timeout=15)
        if not r.ok:  # Markdown inválido -> reintenta plano
            requests.post(f"{base}/sendMessage",
                          json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception as e:
        log.warning("send falló: %s", e)

def _tg_download(base, file_id) -> Optional[bytes]:
    try:
        fr = requests.get(f"{base}/getFile", params={"file_id": file_id},
                          timeout=15).json()
        path = fr["result"]["file_path"]
        token = base.split("/bot")[1]
        return requests.get(f"https://api.telegram.org/file/bot{token}/{path}",
                            timeout=60).content
    except Exception as e:
        log.warning("descarga falló: %s", e)
        return None

def _transcribe(audio: bytes) -> Optional[str]:
    try:
        import whisper
    except ImportError:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as f:
            f.write(audio)
            tmp = f.name
        model = whisper.load_model(CONFIG["whisper_model"])
        result = model.transcribe(tmp, language="es")
        os.unlink(tmp)
        return result.get("text", "").strip()
    except Exception as e:
        log.warning("transcripción falló: %s", e)
        return None

def telegram_poll(mem: MemoryStore):
    base = _tg_base()
    if not base:
        return
    offset = int(mem.kv_get("tg_offset", "0"))
    try:
        r = requests.get(f"{base}/getUpdates",
                         params={"offset": offset + 1, "timeout": 0}, timeout=20)
        r.raise_for_status()
        updates = r.json().get("result", [])
    except Exception as e:
        log.warning("getUpdates falló: %s", e)
        return

    for u in updates:
        mem.kv_set("tg_offset", str(u["update_id"]))
        msg = u.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not chat_id:
            continue
        if not is_allowed(chat_id):
            telegram_send(chat_id, "⛔ Este asistente es privado.")
            continue

        if "document" in msg:  # posible actualización
            doc = msg["document"]
            fname = doc.get("file_name", "")
            if fname in UPDATABLE_FILES:
                content = _tg_download(base, doc["file_id"])
                reply = (stage_update(mem, chat_id, fname, content)
                         if content else "No pude descargar el archivo.")
            else:
                reply = (f"Recibí '{fname}', pero solo acepto actualizaciones de: "
                         f"{', '.join(sorted(UPDATABLE_FILES))}.")
            telegram_send(chat_id, reply)
            continue

        text = None
        if "text" in msg:
            text = msg["text"]
        elif "voice" in msg:
            audio = _tg_download(base, msg["voice"]["file_id"])
            text = _transcribe(audio) if audio else None
            if text:
                telegram_send(chat_id, f"🎙️ Entendí: _{text}_")
            else:
                telegram_send(chat_id, "No pude transcribir el audio "
                              "(¿whisper instalado?).")
                continue
        if text:
            telegram_send(chat_id, handle_text(mem, chat_id, text))


# --------------------------------------------------------------------------- #
# PROACTIVIDAD: recordatorios que vencen + briefing diario
# --------------------------------------------------------------------------- #
def proactive_tick(mem: MemoryStore):
    for r in mem.due_reminders():
        telegram_send(r["chat_id"], f"⏰ Recordatorio: {r['text']}")
    now = dt.datetime.utcnow()
    if OWNER_CHAT_ID and now.hour == CONFIG["briefing_hour_utc"]:
        today = now.date().isoformat()
        if mem.kv_get(f"briefed:{OWNER_CHAT_ID}") != today:
            mem.kv_set(f"briefed:{OWNER_CHAT_ID}", today)
            telegram_send(OWNER_CHAT_ID,
                          skill_briefing({"_chat_id": OWNER_CHAT_ID}, mem))


# --------------------------------------------------------------------------- #
# BUCLE PRINCIPAL
# --------------------------------------------------------------------------- #
def main():
    log.info("HuskAssistant v%s arrancando. Dueño: %s. Skills: %s",
             VERSION, OWNER_CHAT_ID or "(sin configurar)", ", ".join(SKILLS))
    if not OWNER_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID vacío: configúralo en .env para ser el dueño.")
    mem = MemoryStore(CONFIG["db_path"])
    while True:
        try:
            proactive_tick(mem)
            telegram_poll(mem)
        except Exception as e:
            log.exception("Ciclo falló: %s", e)
        time.sleep(CONFIG["poll_seconds"])


if __name__ == "__main__":
    main()
