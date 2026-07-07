#!/usr/bin/env python3
"""
HuskAgent v0.1 — Agente autónomo de screening con aprendizaje por retroalimentación.

Se sienta ENCIMA de tus fuentes de datos actuales (GeckoTerminal, GoPlus, RugCheck).
Bucle: Percibir -> Razonar -> Actuar -> Aprender.

Filosofía de diseño:
  - El "aprendizaje" es un bucle de retroalimentación real: el agente registra cada
    decisión, mide el resultado real más tarde, y usa ese historial para razonar mejor
    la próxima vez. No reentrena modelos ni se auto-modifica: acumula track record.
  - NUNCA ejecuta compras ni toca private keys. Solo alerta y, como mucho, pide
    confirmación. Las decisiones de capital siguen siendo 100% tuyas.
  - Degradado elegante: si no hay API key, razona con heurística pura y sigue vivo.

Secretos vía variables de entorno (no hardcodear):
  ANTHROPIC_API_KEY   -> opcional, activa el razonamiento con LLM
  TELEGRAM_BOT_TOKEN  -> tu bot HuskRadar
  TELEGRAM_CHAT_ID    -> tu chat personal
"""

import os
import json
import time
import sqlite3
import logging
import datetime as dt
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
CONFIG = {
    "db_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "huskagent.db"),
    "cycle_seconds": 8 * 60,          # mismo ritmo que tu memeradar
    "score_threshold": 62,            # umbral base (el agente lo ajusta con el tiempo)
    "min_liquidity_usd": 8_000,
    "max_pool_age_hours": 96,
    "outcome_window_days": 30,        # ventana para medir si un pick "acertó"
    "outcome_hit_multiple": 2.0,      # +100% = acierto (ajústalo a tu gusto)
    # LLM (opcional). Haiku por costo; súbelo a sonnet/opus si quieres más razonamiento.
    "llm_model": "claude-haiku-4-5-20251001",
    "llm_max_candidates_per_cycle": 15,  # tope de tokens que pasan por el modelo (costo)
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("huskagent")


# --------------------------------------------------------------------------- #
# MODELOS DE DATOS
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    """Un token candidato tras la fase de percepción."""
    token_id: str          # identificador único estable (ej. chain:pool_address)
    symbol: str
    chain: str
    liquidity_usd: float
    pool_age_hours: float
    price_usd: float
    features: dict = field(default_factory=dict)  # métricas extra crudas


@dataclass
class Decision:
    """La resolución del agente sobre un candidato."""
    token_id: str
    symbol: str
    action: str            # "alert" | "skip" | "watch"
    confidence: float      # 0..1
    reasoning: str
    features: dict
    price_at_decision: float
    ts: str = field(default_factory=lambda: dt.datetime.utcnow().isoformat())


# --------------------------------------------------------------------------- #
# ALMACÉN DE RETROALIMENTACIÓN (SQLite)
# --------------------------------------------------------------------------- #
class FeedbackStore:
    """Guarda decisiones y sus resultados reales. Este es el corazón del aprendizaje."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                token_id      TEXT,
                symbol        TEXT,
                action        TEXT,
                confidence    REAL,
                reasoning     TEXT,
                features_json TEXT,
                price_at_decision REAL,
                ts            TEXT,
                outcome       TEXT DEFAULT 'pending',   -- pending|hit|miss|expired
                peak_multiple REAL DEFAULT 1.0,
                resolved_ts   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_outcome ON decisions(outcome);
            CREATE INDEX IF NOT EXISTS idx_token   ON decisions(token_id);
            """
        )
        self.conn.commit()

    def record(self, d: Decision):
        self.conn.execute(
            """INSERT INTO decisions
               (token_id, symbol, action, confidence, reasoning, features_json,
                price_at_decision, ts)
               VALUES (?,?,?,?,?,?,?,?)""",
            (d.token_id, d.symbol, d.action, d.confidence, d.reasoning,
             json.dumps(d.features), d.price_at_decision, d.ts),
        )
        self.conn.commit()

    def pending_alerts(self, older_than_hours: float = 0):
        """Decisiones 'alert' aún sin resolver, para chequear su resultado."""
        cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=older_than_hours)).isoformat()
        rows = self.conn.execute(
            """SELECT rowid, * FROM decisions
               WHERE outcome='pending' AND action='alert' AND ts <= ?""",
            (cutoff,),
        ).fetchall()
        return rows

    def resolve(self, rowid: int, outcome: str, peak_multiple: float):
        self.conn.execute(
            "UPDATE decisions SET outcome=?, peak_multiple=?, resolved_ts=? WHERE rowid=?",
            (outcome, peak_multiple, dt.datetime.utcnow().isoformat(), rowid),
        )
        self.conn.commit()

    def already_seen(self, token_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM decisions WHERE token_id=? LIMIT 1", (token_id,)
        ).fetchone()
        return row is not None

    def track_record_summary(self) -> str:
        """
        Resumen compacto de qué le ha funcionado al agente. Esto se INYECTA en el
        prompt de razonamiento: así el modelo decide 'a la luz de su propia experiencia'.
        """
        rows = self.conn.execute(
            "SELECT outcome, confidence, features_json FROM decisions "
            "WHERE outcome IN ('hit','miss')"
        ).fetchall()
        if not rows:
            return "Sin historial resuelto todavía. Usa criterio base."

        hits = [r for r in rows if r["outcome"] == "hit"]
        total = len(rows)
        hit_rate = len(hits) / total

        # Tasa de acierto por franja de confianza (aprendizaje simple y honesto)
        buckets = {"baja(<0.4)": [0, 0], "media(0.4-0.7)": [0, 0], "alta(>0.7)": [0, 0]}
        for r in rows:
            c = r["confidence"]
            key = "baja(<0.4)" if c < 0.4 else "media(0.4-0.7)" if c < 0.7 else "alta(>0.7)"
            buckets[key][1] += 1
            if r["outcome"] == "hit":
                buckets[key][0] += 1

        lines = [f"Historial: {total} decisiones resueltas, tasa de acierto global {hit_rate:.0%}."]
        for k, (h, n) in buckets.items():
            if n:
                lines.append(f"  - Confianza {k}: {h}/{n} aciertos ({h/n:.0%}).")

        # Señal de calibración: ¿el agente es demasiado optimista?
        if buckets["alta(>0.7)"][1] and buckets["alta(>0.7)"][0] / buckets["alta(>0.7)"][1] < 0.4:
            lines.append("  ! AVISO: tus picks de confianza alta rinden mal. Sé más exigente.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 1) PERCIBIR
# --------------------------------------------------------------------------- #
def perceive() -> list[Candidate]:
    """
    STUB: aquí conectas tu lógica del memeradar (GeckoTerminal new_pools/trending,
    GoPlus, RugCheck). Debe devolver una lista de Candidate ya con los filtros duros
    de liquidez/edad aplicados.

    Reemplaza este cuerpo por tus fetchers reales. Devuelvo lista vacía por defecto
    para que el esqueleto no invente datos.
    """
    log.info("perceive(): conecta aquí tus fuentes del memeradar")
    return []


# --------------------------------------------------------------------------- #
# 2) RAZONAR
# --------------------------------------------------------------------------- #
def reason(candidates: list[Candidate], track_record: str) -> list[Decision]:
    decisions = []
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    # Tope de candidatos que pasan por el modelo (control de costo)
    llm_batch = candidates[: CONFIG["llm_max_candidates_per_cycle"]]

    for c in candidates:
        if api_key and c in llm_batch:
            d = _reason_with_llm(c, track_record, api_key)
        else:
            d = _reason_heuristic(c)
        decisions.append(d)
    return decisions


def _reason_heuristic(c: Candidate) -> Decision:
    """Fallback determinista. Score simple; ajusta a tu fórmula real del memeradar."""
    score = 0
    score += 25 if c.liquidity_usd >= CONFIG["min_liquidity_usd"] * 2 else 10
    score += 20 if c.pool_age_hours <= 24 else 5
    score += min(30, c.features.get("volume_24h", 0) / 1000)
    score += min(25, c.features.get("holders", 0) / 20)
    action = "alert" if score >= CONFIG["score_threshold"] else "skip"
    return Decision(
        token_id=c.token_id, symbol=c.symbol, action=action,
        confidence=min(1.0, score / 100),
        reasoning=f"Heurística: score {score:.0f} vs umbral {CONFIG['score_threshold']}.",
        features=c.features, price_at_decision=c.price_usd,
    )


def _reason_with_llm(c: Candidate, track_record: str, api_key: str) -> Decision:
    """Razona con el modelo, inyectando el historial de aciertos como contexto."""
    system = (
        "Eres un analista de tokens meme en etapa temprana. Evalúas riesgo/retorno "
        "y decides si vale una alerta. NUNCA recomiendas comprar ni manejar fondos; "
        "solo clasificas la oportunidad. Responde SOLO con JSON: "
        '{"action":"alert|skip|watch","confidence":0..1,"reasoning":"..."}'
    )
    user = (
        f"TU HISTORIAL DE DESEMPEÑO (aprende de él):\n{track_record}\n\n"
        f"TOKEN:\n{json.dumps({**asdict(c)}, ensure_ascii=False, indent=2)}\n\n"
        "Decide action, confidence y una justificación breve."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CONFIG["llm_model"],
                "max_tokens": 400,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=30,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        clean = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        return Decision(
            token_id=c.token_id, symbol=c.symbol,
            action=parsed.get("action", "skip"),
            confidence=float(parsed.get("confidence", 0.0)),
            reasoning=parsed.get("reasoning", "")[:500],
            features=c.features, price_at_decision=c.price_usd,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("LLM falló (%s), uso heurística para %s", e, c.symbol)
        return _reason_heuristic(c)


# --------------------------------------------------------------------------- #
# 3) ACTUAR  (solo alerta; jamás ejecuta capital)
# --------------------------------------------------------------------------- #
def act(decisions: list[Decision], store: FeedbackStore):
    for d in decisions:
        if store.already_seen(d.token_id):
            continue
        store.record(d)
        if d.action == "alert":
            _send_telegram(_format_alert(d))
            log.info("ALERTA -> %s (conf %.2f)", d.symbol, d.confidence)


def _format_alert(d: Decision) -> str:
    return (
        f"🛰️ *HuskAgent* — {d.symbol}\n"
        f"Confianza: {d.confidence:.0%}\n"
        f"{d.reasoning}\n\n"
        f"⚠️ Revisa en DexScreener y decide tú. El agente NO compra."
    )


def _send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        log.info("[telegram off] %s", text.replace("\n", " | "))
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram falló: %s", e)


# --------------------------------------------------------------------------- #
# 4) APRENDER  (chequea resultados reales y cierra el bucle)
# --------------------------------------------------------------------------- #
def learn(store: FeedbackStore):
    """
    Revisa alertas pendientes: consulta el precio actual, calcula el múltiplo pico
    y marca hit/miss/expired. Esto alimenta el track_record del próximo ciclo.
    """
    pending = store.pending_alerts()
    for row in pending:
        age_days = (dt.datetime.utcnow() - dt.datetime.fromisoformat(row["ts"])).days
        current_price = _fetch_current_price(row["token_id"])  # STUB -> conéctalo
        if current_price is None:
            continue
        multiple = current_price / row["price_at_decision"] if row["price_at_decision"] else 1.0

        if multiple >= CONFIG["outcome_hit_multiple"]:
            store.resolve(row["rowid"], "hit", multiple)
        elif age_days >= CONFIG["outcome_window_days"]:
            outcome = "hit" if multiple >= CONFIG["outcome_hit_multiple"] else "miss"
            store.resolve(row["rowid"], outcome, multiple)


def _fetch_current_price(token_id: str) -> Optional[float]:
    """
    STUB: devuelve el precio actual del token (reusa tu consulta a GeckoTerminal/
    DexScreener). Retorna None si no se pudo obtener.
    """
    return None


# --------------------------------------------------------------------------- #
# BUCLE PRINCIPAL
# --------------------------------------------------------------------------- #
def run_once(store: FeedbackStore):
    learn(store)                                   # cierra bucles viejos primero
    track = store.track_record_summary()           # qué ha funcionado
    candidates = perceive()                        # percibe
    log.info("perceive: %d candidatos", len(candidates))
    decisions = reason(candidates, track)          # razona con historial
    act(decisions, store)                          # actúa (solo alertas)


def main():
    log.info("HuskAgent arrancando. DB: %s", CONFIG["db_path"])
    store = FeedbackStore(CONFIG["db_path"])
    while True:
        try:
            run_once(store)
        except Exception as e:  # noqa: BLE001
            log.exception("Ciclo falló: %s", e)
        time.sleep(CONFIG["cycle_seconds"])


if __name__ == "__main__":
    main()
