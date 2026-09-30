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
import sys
import json
import time
import sqlite3
import logging
import datetime as dt
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests

# --------------------------------------------------------------------------- #
# .env — misma carga simple que agent.py (así funciona igual en Windows)
# --------------------------------------------------------------------------- #
def _load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
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

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
CONFIG = {
    "db_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "huskagent.db"),
    "cycle_seconds": 8 * 60,          # mismo ritmo que tu memeradar
    "score_threshold": 62,            # umbral base (el agente lo ajusta con el tiempo)
    # --- Percepción (ver ESTRATEGIA_TOKENS_NUEVOS.md) ---
    "networks": ["solana", "base"],   # ids de GeckoTerminal: solana, base, bsc, eth...
    "new_pools_pages": 2,             # 20 pools por página
    "min_liquidity_usd": 20_000,      # menos que esto = slippage alto y rug barato
    "min_pool_age_minutes": 30,       # los primeros minutos son de snipers y bundles
    "max_pool_age_hours": 96,
    "max_fdv_usd": 5_000_000,         # arriba de esto ya no es "temprano"
    "min_txns_h24": 150,
    "min_buyers_h1": 25,              # compradores únicos en la última hora
    "max_price_change_h1": 150,       # % — no perseguir velas verticales
    # --- Seguridad (filtros duros; si la API falla, se descarta: fail-closed) ---
    "max_tax": 0.10,                  # impuesto compra/venta máximo (EVM)
    "max_top10_pct": 0.35,            # % en top 10 holders (sin contar LP/lockers)
    "min_lp_locked_pct": 0.80,        # LP quemado o bloqueado
    # --- Resultado / reglas de salida simuladas ---
    "outcome_window_days": 30,        # ventana máxima para medir si un pick "acertó"
    "outcome_hit_multiple": 2.0,      # +100% = acierto (TP1: vender la mitad)
    "outcome_stop_multiple": 0.60,    # -40% = stop (fallo)
    "time_stop_hours": 72,            # si en 72h no pasó de...
    "time_stop_min_multiple": 1.30,   # ...+30%, se libera el capital (fallo)
    # LLM (opcional). Haiku por costo; súbelo a sonnet/opus si quieres más razonamiento.
    "llm_model": "claude-haiku-4-5-20251001",
    "llm_max_candidates_per_cycle": 15,  # tope de tokens que pasan por el modelo (costo)
}

GECKO = "https://api.geckoterminal.com/api/v2"
GOPLUS_CHAIN_IDS = {"eth": "1", "bsc": "56", "base": "8453", "arbitrum": "42161", "polygon_pos": "137"}
# Tokens "quote" (nativos envueltos / stables): si aparecen como base, el pool está invertido.
QUOTE_TOKENS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "0x4200000000000000000000000000000000000006",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "0x55d398326f99059ff775485246999027b3197955",
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

    def update_peak(self, rowid: int, peak_multiple: float):
        self.conn.execute("UPDATE decisions SET peak_multiple=? WHERE rowid=?", (peak_multiple, rowid))
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
def perceive(store: Optional[FeedbackStore] = None) -> list[Candidate]:
    """
    Pools nuevos de GeckoTerminal -> filtros de mercado (baratos) -> filtros de
    seguridad (GoPlus en EVM, RugCheck en Solana). Solo pasa lo que sobrevive a todo.
    """
    out = []
    for net in CONFIG["networks"]:
        for page in range(1, CONFIG["new_pools_pages"] + 1):
            data = _gecko_get(f"/networks/{net}/new_pools", {"page": page})
            if not data:
                break
            for pool in data.get("data", []):
                c = _pool_to_candidate(net, pool)
                if not c or (store and store.already_seen(c.token_id)):
                    continue
                motivo = _market_reject_reason(c)
                if motivo:
                    log.debug("descarto %s: %s", c.symbol, motivo)
                    continue
                motivo = _security_reject_reason(c)
                if motivo:
                    log.info("descarto %s por seguridad: %s", c.symbol, motivo)
                    continue
                out.append(c)
    return out


def _gecko_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """GeckoTerminal público: ~30 llamadas/min, así que vamos despacio."""
    time.sleep(2.2)
    try:
        r = requests.get(GECKO + path, params=params, timeout=20,
                         headers={"Accept": "application/json;version=20230302"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("GeckoTerminal falló (%s): %s", path, e)
        return None


def _f(x, default=0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _pool_to_candidate(net: str, pool: dict) -> Optional[Candidate]:
    a = pool.get("attributes", {})
    rel = pool.get("relationships", {})
    base_id = (rel.get("base_token", {}).get("data") or {}).get("id", "")
    token_address = base_id.split("_", 1)[1] if "_" in base_id else ""
    if not token_address or token_address in QUOTE_TOKENS or token_address.lower() in QUOTE_TOKENS:
        return None
    try:
        created = dt.datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return None
    age_h = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600

    tx, vol, chg = a.get("transactions", {}), a.get("volume_usd", {}), a.get("price_change_percentage", {})
    h1, h24 = tx.get("h1", {}), tx.get("h24", {})
    liq = _f(a.get("reserve_in_usd"))
    features = {
        "token_address": token_address,
        "pool_address": a.get("address", ""),
        "dex": (rel.get("dex", {}).get("data") or {}).get("id", ""),
        "fdv_usd": _f(a.get("fdv_usd")),
        "volume_h1": _f(vol.get("h1")),
        "volume_24h": _f(vol.get("h24")),
        "vol_liq_ratio": _f(vol.get("h24")) / liq if liq else 0.0,
        "buys_h1": int(_f(h1.get("buys"))),
        "sells_h1": int(_f(h1.get("sells"))),
        "buyers_h1": int(_f(h1.get("buyers"))),
        "sellers_h1": int(_f(h1.get("sellers"))),
        "txns_h24": int(_f(h24.get("buys")) + _f(h24.get("sells"))),
        "price_change_h1": _f(chg.get("h1")),
        "price_change_h6": _f(chg.get("h6")),
        "price_change_h24": _f(chg.get("h24")),
    }
    return Candidate(
        token_id=f"{net}:{a.get('address', '')}",
        symbol=(a.get("name") or "?").split(" / ")[0],
        chain=net, liquidity_usd=liq, pool_age_hours=age_h,
        price_usd=_f(a.get("base_token_price_usd")), features=features,
    )


def _market_reject_reason(c: Candidate) -> Optional[str]:
    f = c.features
    if c.price_usd <= 0:
        return "sin precio"
    if c.liquidity_usd < CONFIG["min_liquidity_usd"]:
        return f"liquidez {c.liquidity_usd:,.0f}"
    if c.pool_age_hours * 60 < CONFIG["min_pool_age_minutes"]:
        return "demasiado nuevo"
    if c.pool_age_hours > CONFIG["max_pool_age_hours"]:
        return "demasiado viejo"
    if f["fdv_usd"] > CONFIG["max_fdv_usd"]:
        return f"FDV {f['fdv_usd']:,.0f}"
    if f["txns_h24"] < CONFIG["min_txns_h24"]:
        return "pocas transacciones"
    if f["buyers_h1"] < CONFIG["min_buyers_h1"]:
        return "pocos compradores en 1h"
    if f["price_change_h1"] > CONFIG["max_price_change_h1"]:
        return "vela vertical, no se persigue"
    if f["sells_h1"] == 0 and f["buys_h1"] > 20:
        return "nadie vende (posible honeypot)"
    return None


def _security_reject_reason(c: Candidate) -> Optional[str]:
    if c.chain == "solana":
        return _rugcheck_reason(c)
    if c.chain in GOPLUS_CHAIN_IDS:
        return _goplus_reason(c)
    return "red sin chequeo de seguridad"


def _goplus_reason(c: Candidate) -> Optional[str]:
    addr = c.features["token_address"].lower()
    try:
        r = requests.get(
            f"https://api.gopluslabs.io/api/v1/token_security/{GOPLUS_CHAIN_IDS[c.chain]}",
            params={"contract_addresses": addr}, timeout=20)
        r.raise_for_status()
        s = (r.json().get("result") or {}).get(addr)
    except Exception as e:  # noqa: BLE001
        return f"GoPlus no respondió ({e})"
    if not s:
        return "GoPlus sin datos"
    for flag, motivo in [("is_honeypot", "honeypot"), ("cannot_sell_all", "no deja vender todo"),
                         ("hidden_owner", "owner oculto"), ("owner_change_balance", "owner cambia balances"),
                         ("transfer_pausable", "transferencias pausables"), ("is_mintable", "mint abierto"),
                         ("is_blacklisted", "tiene blacklist"), ("selfdestruct", "selfdestruct")]:
        if s.get(flag) == "1":
            return motivo
    if s.get("is_open_source") != "1":
        return "contrato no verificado"
    tax = max(_f(s.get("buy_tax")), _f(s.get("sell_tax")))
    if tax > CONFIG["max_tax"]:
        return f"impuesto {tax:.0%}"
    top10 = sum(_f(h.get("percent")) for h in (s.get("holders") or [])[:10]
                if h.get("is_locked") != 1 and h.get("is_contract") != 1)
    if top10 > CONFIG["max_top10_pct"]:
        return f"top10 tiene {top10:.0%}"
    lp = s.get("lp_holders") or []
    lp_safe = sum(_f(h.get("percent")) for h in lp
                  if h.get("is_locked") == 1 or str(h.get("address", "")).lower().startswith("0x000000000000000000000000000000000000dead")
                  or str(h.get("address", "")) == "0x0000000000000000000000000000000000000000")
    if lp and lp_safe < CONFIG["min_lp_locked_pct"]:
        return f"LP bloqueado solo {lp_safe:.0%}"
    c.features.update({"top10_pct": round(top10, 3), "lp_locked_pct": round(lp_safe, 3),
                       "tax": tax, "holders": int(_f(s.get("holder_count")))})
    return None


def _rugcheck_reason(c: Candidate) -> Optional[str]:
    mint = c.features["token_address"]
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", timeout=20)
        r.raise_for_status()
        s = r.json()
    except Exception as e:  # noqa: BLE001
        return f"RugCheck no respondió ({e})"
    risks = s.get("risks") or []
    peligros = [x.get("name", "?") for x in risks if x.get("level") == "danger"]
    if peligros:
        return "RugCheck: " + ", ".join(peligros[:3])
    lp = s.get("lpLockedPct")
    if lp is not None and _f(lp) / 100 < CONFIG["min_lp_locked_pct"]:
        return f"LP bloqueado solo {_f(lp):.0f}%"
    c.features.update({"rugcheck_score": s.get("score_normalised", s.get("score")),
                       "rugcheck_warns": [x.get("name") for x in risks if x.get("level") == "warn"][:5],
                       "lp_locked_pct": round(_f(lp) / 100, 3) if lp is not None else None})
    return None


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
    """
    Fallback determinista: el candidato ya pasó los filtros de seguridad, así que
    aquí solo se puntúa la TRACCIÓN (0-100). Ver ESTRATEGIA_TOKENS_NUEVOS.md.
    """
    f = c.features
    ratio = f["buys_h1"] / max(1, f["sells_h1"])
    chg1 = f["price_change_h1"]
    parts = {
        "liquidez": min(20, c.liquidity_usd / 5_000),
        "compradores": min(20, f["buyers_h1"] / 5),
        "presión": max(0, min(15, (ratio - 1) * 15)),
        "vol/liq": min(15, f["vol_liq_ratio"] * 3),
        "momentum": 15 if 0 < chg1 <= 100 else 5 if chg1 > 100 else 0,
        "edad": 10 if 1 <= c.pool_age_hours <= 24 else 4,
        "fdv": 5 if f["fdv_usd"] < 1_000_000 else 0,
    }
    score = sum(parts.values())
    action = "alert" if score >= CONFIG["score_threshold"] else \
        "watch" if score >= CONFIG["score_threshold"] - 12 else "skip"
    detalle = ", ".join(f"{k} {v:.0f}" for k, v in parts.items())
    return Decision(
        token_id=c.token_id, symbol=c.symbol, action=action,
        confidence=min(1.0, score / 100),
        reasoning=f"Heurística: score {score:.0f} vs umbral {CONFIG['score_threshold']} ({detalle}).",
        features={**f, "chain": c.chain, "liquidity_usd": c.liquidity_usd,
                  "pool_age_hours": round(c.pool_age_hours, 1)},
        price_at_decision=c.price_usd,
    )


def _reason_with_llm(c: Candidate, track_record: str, api_key: str) -> Decision:
    """Razona con el modelo, inyectando el historial de aciertos como contexto."""
    system = (
        "Eres un analista de tokens recién creados (horas de vida). El candidato ya "
        "pasó filtros anti-rug; tú juzgas si la tracción es orgánica (compradores únicos, "
        "presión compradora, volumen/liquidez, momentum sin vela vertical) y si hay "
        "espacio para un 2x. Sé exigente: la mayoría de estos tokens van a cero. "
        "Evalúas riesgo/retorno y decides si vale una alerta. "
        "NUNCA recomiendas comprar ni manejar fondos; "
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
            features={**c.features, "chain": c.chain, "liquidity_usd": c.liquidity_usd,
                      "pool_age_hours": round(c.pool_age_hours, 1)},
            price_at_decision=c.price_usd,
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
        if d.action == "watch":   # no se guarda: se re-evalúa el próximo ciclo
            log.info("vigilando %s (conf %.2f)", d.symbol, d.confidence)
            continue
        store.record(d)
        if d.action == "alert":
            _send_telegram(_format_alert(d))
            log.info("ALERTA -> %s (conf %.2f)", d.symbol, d.confidence)


def _format_alert(d: Decision) -> str:
    f = d.features
    chain, pool = d.token_id.split(":", 1)
    return (
        f"🛰️ *HuskAgent* — {d.symbol} ({chain})\n"
        f"Confianza: {d.confidence:.0%}\n"
        f"Liq ${f.get('liquidity_usd', 0):,.0f} · FDV ${f.get('fdv_usd', 0):,.0f} · "
        f"edad {f.get('pool_age_hours', '?')}h · 1h {f.get('price_change_h1', 0):+.0f}%\n"
        f"{d.reasoning}\n"
        f"https://www.geckoterminal.com/{chain}/pools/{pool}\n\n"
        f"Plan si entras: tamaño fijo pequeño · stop -40% · vende 50% en 2x · "
        f"resto con trailing -30% desde el pico.\n"
        f"⚠️ Revisa y decide tú. El agente NO compra."
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
    Revisa alertas pendientes y SIMULA las reglas de salida de la estrategia:
    2x = acierto (TP1), -40% = stop, sin tracción en 72h = time stop. Avisa por
    Telegram cuando se dispara una salida. Esto alimenta el track_record del próximo ciclo.
    Nota: se muestrea cada ciclo, así que mechas intraciclo pueden no verse.
    """
    for row in store.pending_alerts():
        age_h = (dt.datetime.utcnow() - dt.datetime.fromisoformat(row["ts"])).total_seconds() / 3600
        current_price = _fetch_current_price(row["token_id"])
        if current_price is None:
            if age_h >= CONFIG["outcome_window_days"] * 24:
                store.resolve(row["rowid"], "miss", row["peak_multiple"])
            continue
        multiple = current_price / row["price_at_decision"] if row["price_at_decision"] else 1.0
        peak = max(row["peak_multiple"] or 1.0, multiple)

        if multiple >= CONFIG["outcome_hit_multiple"]:
            store.resolve(row["rowid"], "hit", peak)
            _send_telegram(f"🎯 {row['symbol']} llegó a {multiple:.1f}x: TP1, vende la mitad "
                           f"(recuperas capital) y deja el resto con trailing -30%.")
        elif multiple <= CONFIG["outcome_stop_multiple"]:
            store.resolve(row["rowid"], "miss", peak)
            _send_telegram(f"🛑 {row['symbol']} en {multiple:.2f}x: stop -40%. Si entraste, sal.")
        elif age_h >= CONFIG["time_stop_hours"] and peak < CONFIG["time_stop_min_multiple"]:
            store.resolve(row["rowid"], "miss", peak)
            _send_telegram(f"⌛ {row['symbol']} sin tracción en {CONFIG['time_stop_hours']}h "
                           f"({multiple:.2f}x): time stop, libera el capital.")
        elif age_h >= CONFIG["outcome_window_days"] * 24:
            store.resolve(row["rowid"], "miss", peak)
        else:
            store.update_peak(row["rowid"], peak)


def _fetch_current_price(token_id: str) -> Optional[float]:
    """Precio actual del token base del pool vía GeckoTerminal. None si no se pudo."""
    net, pool = token_id.split(":", 1)
    data = _gecko_get(f"/networks/{net}/pools/{pool}")
    if not data:
        return None
    price = _f((data.get("data") or {}).get("attributes", {}).get("base_token_price_usd"))
    return price or None


# --------------------------------------------------------------------------- #
# BUCLE PRINCIPAL
# --------------------------------------------------------------------------- #
def run_once(store: FeedbackStore):
    learn(store)                                   # cierra bucles viejos primero
    track = store.track_record_summary()           # qué ha funcionado
    candidates = perceive(store)                   # percibe
    log.info("perceive: %d candidatos", len(candidates))
    decisions = reason(candidates, track)          # razona con historial
    act(decisions, store)                          # actúa (solo alertas)


def main():
    log.info("HuskAgent arrancando. DB: %s", CONFIG["db_path"])
    store = FeedbackStore(CONFIG["db_path"])
    if "--once" in sys.argv:
        run_once(store)
        return
    while True:
        try:
            run_once(store)
        except Exception as e:  # noqa: BLE001
            log.exception("Ciclo falló: %s", e)
        time.sleep(CONFIG["cycle_seconds"])


if __name__ == "__main__":
    main()
