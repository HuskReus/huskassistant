#!/usr/bin/env python3
"""
Estrategia de tendencia con control de volatilidad (spot, solo largos, sin apalancamiento).

Reglas (se evalúan una vez al día, con el cierre diario UTC):
  1. Filtro de tendencia: un activo solo se mantiene si su cierre > SMA(sma_dias).
     Si está por debajo -> 100% en stablecoin (efectivo) para ese activo.
  2. Tamaño por volatilidad: peso = min(1, vol_objetivo / vol_realizada_30d),
     repartido en partes iguales entre los activos del universo.
     Cuando el mercado se pone violento, se reduce la exposición sola.
  3. Anti-comisiones: solo se rebalancea si el peso cambia más de `umbral_rebalanceo`.

No promete ganancias: busca capturar las tendencias alcistas largas de cripto y
esquivar la mayor parte de los mercados bajistas (-75%/-85% en BTC/ETH). A cambio,
pierde en mercados laterales (entradas y salidas falsas) y siempre llega tarde.

Igual que huskagent.py: NUNCA ejecuta órdenes ni toca llaves. Solo calcula y avisa.

Uso:
  python estrategia.py backtest            # backtest con datos reales de Binance
  python estrategia.py senal               # posición recomendada para hoy
  python estrategia.py senal --telegram    # ... y la manda a tu bot
  python estrategia.py papel --telegram    # avanza la cartera simulada en vivo (1 vez al día)
  python estrategia.py backtest --sintetico  # prueba offline con datos simulados
"""

import os
import sys
import math
import json
import random
import sqlite3
import argparse
import datetime as dt

import requests

CONFIG = {
    "activos": ["BTCUSDT", "ETHUSDT"],
    "sma_dias": 100,              # filtro de tendencia
    "vol_dias": 30,               # ventana de volatilidad realizada
    "vol_objetivo": 0.50,         # 50% anualizada por activo (BTC suele estar en 40-80%)
    "umbral_rebalanceo": 0.20,    # no mover menos de 20 puntos de peso (menos comisiones)
    "comision": 0.001,            # 0.10% por lado (Binance spot sin BNB)
    "desde": "2018-01-01",
}

DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DIR, "estrategia.db")

BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]


# --------------------------------------------------------------------------- #
# DATOS
# --------------------------------------------------------------------------- #
def descargar_cierres(simbolo: str, desde: str) -> list[tuple[dt.date, float]]:
    """Cierres diarios de Binance (API pública, sin llave)."""
    inicio_ms = int(dt.datetime.fromisoformat(desde).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    ultimo_error = None
    for host in BINANCE_HOSTS:
        try:
            filas, cursor = [], inicio_ms
            while True:
                r = requests.get(f"{host}/api/v3/klines", timeout=20, params={
                    "symbol": simbolo, "interval": "1d", "startTime": cursor, "limit": 1000})
                r.raise_for_status()
                lote = r.json()
                if not lote:
                    break
                filas.extend(lote)
                if len(lote) < 1000:
                    break
                cursor = lote[-1][0] + 86_400_000
            hoy = dt.datetime.now(dt.timezone.utc).date()
            datos = [(dt.datetime.fromtimestamp(k[0] / 1000, dt.timezone.utc).date(), float(k[4])) for k in filas]
            return [(d, c) for d, c in datos if d < hoy]  # descarta la vela de hoy (aún abierta)
        except requests.RequestException as e:
            ultimo_error = e
    raise RuntimeError(f"No pude descargar {simbolo}: {ultimo_error}")


def cierres_sinteticos(simbolo: str, dias: int = 2500) -> list[tuple[dt.date, float]]:
    """Serie simulada con regímenes alcistas/bajistas, solo para probar el código offline."""
    rng = random.Random(sum(map(ord, simbolo)))
    precio, deriva, fecha = 10_000.0, 0.0, dt.date(2019, 1, 1)
    serie = []
    for i in range(dias):
        if i % 250 == 0:
            deriva = rng.choice([0.004, -0.003, 0.0])
        precio *= math.exp(deriva + rng.gauss(0, 0.035))
        serie.append((fecha + dt.timedelta(days=i), precio))
    return serie


def alinear(series: dict[str, list[tuple[dt.date, float]]]):
    """Deja solo las fechas que tienen todos los activos."""
    comunes = set.intersection(*(set(d for d, _ in s) for s in series.values()))
    fechas = sorted(comunes)
    precios = {sym: dict(s) for sym, s in series.items()}
    return fechas, {sym: [precios[sym][d] for d in fechas] for sym in series}


# --------------------------------------------------------------------------- #
# SEÑAL
# --------------------------------------------------------------------------- #
def peso_objetivo(cierres: list[float], i: int, cfg: dict) -> float:
    """Peso (0..1) de UN activo al cierre del día i, usando solo datos hasta i."""
    n_sma, n_vol = cfg["sma_dias"], cfg["vol_dias"]
    if i < max(n_sma, n_vol + 1) - 1:
        return 0.0
    sma = sum(cierres[i - n_sma + 1:i + 1]) / n_sma
    if cierres[i] <= sma:
        return 0.0
    rets = [math.log(cierres[j] / cierres[j - 1]) for j in range(i - n_vol + 1, i + 1)]
    media = sum(rets) / n_vol
    vol = math.sqrt(sum((r - media) ** 2 for r in rets) / (n_vol - 1)) * math.sqrt(365)
    return min(1.0, cfg["vol_objetivo"] / vol) if vol > 0 else 0.0


# --------------------------------------------------------------------------- #
# BACKTEST
# --------------------------------------------------------------------------- #
def rebalancear(pesos: dict, historia: dict[str, list[float]], cfg: dict):
    """Aplica las reglas al último cierre de `historia`. Devuelve (pesos, costo, cambios)."""
    parte = 1.0 / len(historia)
    nuevos, costo, cambios = dict(pesos), 0.0, {}
    for a, cierres in historia.items():
        objetivo = peso_objetivo(cierres, len(cierres) - 1, cfg) * parte
        if abs(objetivo - pesos[a]) >= cfg["umbral_rebalanceo"] * parte or (objetivo == 0 < pesos[a]):
            costo += abs(objetivo - pesos[a]) * cfg["comision"]
            cambios[a] = (pesos[a], objetivo)
            nuevos[a] = objetivo
    return nuevos, costo, cambios


def derivar(pesos: dict, crecimiento: dict) -> tuple[dict, float]:
    """Mueve el portafolio con los precios. Devuelve (pesos nuevos, retorno del periodo)."""
    r = sum(pesos[a] * (crecimiento[a] - 1) for a in pesos)
    return {a: pesos[a] * crecimiento[a] / (1 + r) for a in pesos}, r


def backtest(fechas, precios: dict[str, list[float]], cfg: dict) -> dict:
    activos = list(precios)
    parte = 1.0 / len(activos)
    pesos = {a: 0.0 for a in activos}
    equity, bh = [1.0], [1.0]
    operaciones, costo_total = 0, 0.0

    for i in range(len(fechas) - 1):
        # 1) decidir con el cierre de hoy
        pesos, costo, cambios = rebalancear(pesos, {a: precios[a][:i + 1] for a in activos}, cfg)
        equity[-1] *= (1 - costo)
        costo_total += costo
        operaciones += len(cambios)
        # 2) aplicar el retorno de mañana (sin mirar al futuro)
        crec = {a: precios[a][i + 1] / precios[a][i] for a in activos}
        pesos, r_port = derivar(pesos, crec)
        r_bh = sum(parte * (crec[a] - 1) for a in activos)
        equity.append(equity[-1] * (1 + r_port))
        bh.append(bh[-1] * (1 + r_bh))

    return {"fechas": fechas, "estrategia": metricas(equity), "buy_hold": metricas(bh),
            "operaciones": operaciones, "costo_total": costo_total, "pesos_hoy": pesos}


def metricas(curva: list[float]) -> dict:
    años = (len(curva) - 1) / 365
    rets = [curva[i] / curva[i - 1] - 1 for i in range(1, len(curva))]
    media = sum(rets) / len(rets)
    desv = math.sqrt(sum((r - media) ** 2 for r in rets) / (len(rets) - 1))
    pico, max_dd = curva[0], 0.0
    for v in curva:
        pico = max(pico, v)
        max_dd = min(max_dd, v / pico - 1)
    cagr = curva[-1] ** (1 / años) - 1 if años > 0 else 0.0
    return {
        "multiplo": curva[-1],
        "cagr": cagr,
        "max_drawdown": max_dd,
        "sharpe": (media / desv) * math.sqrt(365) if desv > 0 else 0.0,
        "calmar": cagr / abs(max_dd) if max_dd < 0 else 0.0,
    }


def imprimir_reporte(res: dict):
    f = res["fechas"]
    print(f"\nPeriodo: {f[0]} -> {f[-1]}  ({len(f)} días)")
    print(f"{'':14}{'Estrategia':>12}{'Buy & Hold':>12}")
    for clave, etiqueta, fmt in [("multiplo", "Múltiplo", "{:.2f}x"), ("cagr", "CAGR", "{:.1%}"),
                                 ("max_drawdown", "Máx. caída", "{:.1%}"), ("sharpe", "Sharpe", "{:.2f}"),
                                 ("calmar", "Calmar", "{:.2f}")]:
        e, b = res["estrategia"][clave], res["buy_hold"][clave]
        print(f"{etiqueta:14}{fmt.format(e):>12}{fmt.format(b):>12}")
    print(f"\nOperaciones: {res['operaciones']}  |  comisiones pagadas: {res['costo_total']:.2%} del capital")


# --------------------------------------------------------------------------- #
# SEÑAL DE HOY
# --------------------------------------------------------------------------- #
def senal_hoy(fechas, precios: dict[str, list[float]], cfg: dict) -> str:
    i = len(fechas) - 1
    parte = 1.0 / len(precios)
    lineas = [f"📈 Estrategia de tendencia — cierre {fechas[i]}"]
    efectivo = 1.0
    for a, cierres in precios.items():
        n = cfg["sma_dias"]
        sma = sum(cierres[i - n + 1:i + 1]) / n
        peso = peso_objetivo(cierres, i, cfg) * parte
        efectivo -= peso
        estado = "ALCISTA" if cierres[i] > sma else "bajista"
        lineas.append(f"• {a}: {cierres[i]:,.2f} vs SMA{n} {sma:,.2f} ({estado}) -> {peso:.0%} del capital")
    lineas.append(f"• Stablecoin/efectivo: {efectivo:.0%}")
    lineas.append("Solo es una señal. Tú decides y ejecutas.")
    return "\n".join(lineas)


def enviar_telegram(texto: str):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("(Falta TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID; no se envió)")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": texto}, timeout=15)
        if r.ok:
            print("(Enviado a Telegram)")
        else:
            print(f"(Telegram rechazó el mensaje: {r.json().get('description', r.status_code)})")
    except requests.RequestException as e:
        print(f"(No pude conectar con Telegram: {e})")


# --------------------------------------------------------------------------- #
# SEGUIMIENTO EN VIVO (paper trading)
# --------------------------------------------------------------------------- #
class Papel:
    """Cartera simulada que avanza un día por ejecución y se guarda en estrategia.db."""

    def __init__(self, path: str = DB_PATH):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS dias (
            fecha TEXT PRIMARY KEY, precios TEXT, pesos TEXT,
            equity REAL, buy_hold REAL, operaciones TEXT)""")

    def dias(self) -> list[tuple]:
        return self.db.execute(
            "SELECT fecha, precios, pesos, equity, buy_hold, operaciones FROM dias ORDER BY fecha").fetchall()

    def avanzar(self, fechas, precios: dict[str, list[float]], cfg: dict, capital: float) -> tuple[bool, dict]:
        """Registra el último cierre. Devuelve (hubo_dia_nuevo, cambios de posición)."""
        hoy = fechas[-1].isoformat()
        filas = self.dias()
        if filas and filas[-1][0] >= hoy:
            return False, {}
        activos = list(precios)
        cierre = {a: precios[a][-1] for a in activos}
        if filas:
            _, p_ant, w_ant, equity, bh, _ = filas[-1]
            p_ant, w_ant = json.loads(p_ant), json.loads(w_ant)
            crec = {a: cierre[a] / p_ant[a] for a in activos}
            pesos, r = derivar(w_ant, crec)
            equity *= 1 + r
            bh *= 1 + sum(crec[a] - 1 for a in activos) / len(activos)
        else:
            pesos, equity, bh = {a: 0.0 for a in activos}, capital, capital
        pesos, costo, cambios = rebalancear(pesos, precios, cfg)
        equity *= 1 - costo
        self.db.execute("INSERT INTO dias VALUES (?,?,?,?,?,?)",
                        (hoy, json.dumps(cierre), json.dumps(pesos), equity, bh, json.dumps(cambios)))
        self.db.commit()
        return True, cambios

    def reporte(self, cambios: dict) -> str:
        filas = self.dias()
        if not filas:
            return "Seguimiento en vivo: todavía sin datos."
        inicio, ultimo = filas[0], filas[-1]
        curva = [f[3] for f in filas]
        pico, max_dd = curva[0], 0.0
        for v in curva:
            pico = max(pico, v)
            max_dd = min(max_dd, v / pico - 1)
        pesos = json.loads(ultimo[2])
        lineas = [
            f"🧪 Seguimiento en vivo (papel) — cierre {ultimo[0]}",
            f"Desde {inicio[0]} ({len(filas)} días registrados)",
            f"• Estrategia: {ultimo[3]:,.2f} ({ultimo[3] / inicio[3] - 1:+.1%})",
            f"• Buy & hold: {ultimo[4]:,.2f} ({ultimo[4] / inicio[4] - 1:+.1%})",
            f"• Máx. caída de la estrategia: {max_dd:.1%}",
            "Posición actual: " + ", ".join(f"{a} {w:.0%}" for a, w in pesos.items())
            + f", efectivo {1 - sum(pesos.values()):.0%}",
        ]
        for a, (antes, despues) in cambios.items():
            accion = "COMPRAR" if despues > antes else "VENDER"
            lineas.append(f"⚠️ {accion} {a}: de {antes:.0%} a {despues:.0%} del capital")
        if not cambios:
            lineas.append("Sin cambios hoy: mantener.")
        return "\n".join(lineas)


def cargar_env(path: str = os.path.join(DIR, ".env")):
    """Lee .env (KEY=valor) sin pisar variables ya definidas."""
    try:
        with open(path, encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if linea and not linea.startswith("#") and "=" in linea:
                    k, v = linea.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def main():
    p = argparse.ArgumentParser(description="Estrategia de tendencia cripto (solo señales)")
    p.add_argument("modo", choices=["backtest", "senal", "papel"])
    p.add_argument("--sintetico", action="store_true", help="usar datos simulados (sin internet)")
    p.add_argument("--telegram", action="store_true", help="enviar la señal a Telegram")
    p.add_argument("--sma", type=int, help="días de la media móvil (defecto 100)")
    p.add_argument("--capital", type=float, default=1000.0, help="capital simulado inicial en papel")
    p.add_argument("--db", default=DB_PATH, help="base de datos del seguimiento en papel")
    args = p.parse_args()
    cargar_env()

    cfg = dict(CONFIG)
    if args.sma:
        cfg["sma_dias"] = args.sma
    fuente = cierres_sinteticos if args.sintetico else (lambda s: descargar_cierres(s, cfg["desde"]))
    fechas, precios = alinear({a: fuente(a) for a in cfg["activos"]})

    if args.modo == "backtest":
        imprimir_reporte(backtest(fechas, precios, cfg))
    elif args.modo == "papel":
        papel = Papel(args.db)
        nuevo, cambios = papel.avanzar(fechas, precios, cfg, args.capital)
        texto = papel.reporte(cambios)
        if not nuevo:
            texto += "\n(Este cierre ya estaba registrado; no se simuló nada nuevo.)"
        print(texto)
        if args.telegram and nuevo:
            enviar_telegram(texto)
    else:
        texto = senal_hoy(fechas, precios, cfg)
        print(texto)
        if args.telegram:
            enviar_telegram(texto)


if __name__ == "__main__":
    sys.exit(main())
