# Estrategia de trading cripto: tendencia + control de volatilidad

Archivo: `estrategia.py`. Como el resto de HuskAssistant, **solo calcula y avisa;
no ejecuta órdenes ni toca llaves**.

## Aviso honesto primero

No existe una estrategia con ganancias garantizadas. Si alguien te vende una, miente.
La mayoría de quienes hacen trading activo en cripto pierden contra simplemente
comprar y mantener, sobre todo por comisiones, apalancamiento y decisiones emocionales.
Lo que sí tiene respaldo (décadas de evidencia en futuros, acciones y cripto) es
el **seguimiento de tendencia**: no gana más en cada subida, pero evita la mayor
parte de las caídas del 75–85% que cripto sufre cada pocos años. Ese es el objetivo:
**mejor rentabilidad ajustada a riesgo**, no hacerse rico rápido.

## Reglas

Se revisa **una vez al día**, después del cierre diario (00:00 UTC):

1. **Universo:** BTC y ETH, mitad del capital para cada uno. Nada de altcoins
   pequeñas (liquidez baja, rugs, tendencias que no se sostienen).
2. **Filtro de tendencia:** si el cierre está **por encima de la media móvil de 100 días**,
   se puede tener el activo. Si cierra por debajo, esa mitad pasa a stablecoin.
3. **Tamaño por volatilidad:** peso = `min(1, 50% / volatilidad anualizada de 30 días)`.
   Si BTC se mueve a 80% anualizado, solo tienes ~62% de su mitad; el resto en stablecoin.
4. **Rebalanceo mínimo:** solo ajustas si el peso cambia más de 20 puntos
   (menos operaciones = menos comisiones). Salir por tendencia rota se hace siempre.
5. **Sin apalancamiento, sin futuros, sin stops intradía.** Spot y paciencia.

## Gestión de riesgo (la parte que realmente importa)

- Usa solo dinero que puedas perder entero. Cripto no debería ser todo tu patrimonio.
- Espera caídas de 30–45% **incluso con la estrategia**: sale tarde por diseño.
- En mercados laterales vas a entrar y salir varias veces perdiendo un poco. Es normal;
  es el “seguro” que pagas para estar fuera en los bajistas grandes.
- No cambies las reglas después de 3 pérdidas seguidas. La disciplina es la ventaja.
- Impuestos: cada salida a stablecoin puede ser un evento gravable en tu país.

## Uso

```bash
python estrategia.py backtest            # historial real de Binance desde 2018
python estrategia.py backtest --sma 50   # prueba otra media
python estrategia.py senal               # qué % tener hoy
python estrategia.py papel               # avanza la cartera simulada en vivo
python estrategia.py papel --telegram    # ... y te manda el reporte a tu bot
python estrategia.py backtest --sintetico  # prueba offline
```

El script lee tu `.env` solo (usa `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`).

## Medir: histórico + en vivo

- **Histórico (`backtest`)**: qué habría pasado desde 2018. Rápido, pero es fácil
  engañarse (sabes lo que pasó).
- **En vivo en papel (`papel`)**: una cartera simulada que empieza el día que la
  arrancas (1000 por defecto, `--capital` para cambiarlo) y avanza un cierre por
  día, con las mismas reglas y comisiones que el backtest. Se guarda en
  `estrategia.db` y cada día te dice cuánto va la estrategia contra buy & hold,
  su peor caída y si toca **COMPRAR/VENDER** algo. Esta es la prueba honesta:
  nadie conoce el futuro.
- Correrlo dos veces el mismo día no duplica nada. Si un día falla, al siguiente
  se pone al día usando los precios de cierre.
- Para empezar de cero: borra `estrategia.db`.

## Conectarlo en tu VPS, paso a paso

1. **Entra al VPS** y ve a la carpeta de HuskAssistant:
   ```bash
   ssh usuario@IP_DEL_VPS
   cd /ruta/huskassistant
   ```
2. **Baja la versión nueva** desde GitHub:
   ```bash
   git pull origin main
   ```
   (Si instalaste subiendo archivos a mano, sube `estrategia.py` a esa carpeta.)
3. **Asegúrate de tener el entorno**: si ya corriste `./start.sh`, existe `.venv`.
   Si no: `python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`
4. **Prueba el histórico**:
   ```bash
   ./.venv/bin/python estrategia.py backtest
   ```
   Si sale un error de conexión, tu VPS no llega a Binance (algunos países o
   proveedores lo bloquean); prueba otro VPS o región.
5. **Arranca el seguimiento en vivo** (primer día):
   ```bash
   ./.venv/bin/python estrategia.py papel --telegram
   ```
   Te debe llegar un mensaje "🧪 Seguimiento en vivo" al Telegram.
6. **Prográmalo para cada día**: `crontab -e` y agrega al final (cambia la ruta):
   ```
   10 0 * * * cd /ruta/huskassistant && ./.venv/bin/python estrategia.py papel --telegram >> estrategia.log 2>&1
   ```
   Corre a las 00:10 UTC, justo después del cierre diario. Comprueba con `crontab -l`.
7. **Revisa** al día siguiente que llegó el mensaje. Si no, mira `estrategia.log`.

**En Windows** (PC siempre encendida): Programador de tareas → Crear tarea básica →
Diaria → Programa: `C:\ruta\huskassistant\.venv\Scripts\python.exe`,
argumentos `estrategia.py papel --telegram`, "Iniciar en": `C:\ruta\huskassistant`.
Pon la hora local equivalente a las 00:10 UTC.

## Antes de poner dinero

1. Corre `backtest` con datos reales y compáralo con buy & hold: mira **máx. caída**
   y **Calmar**, no solo el múltiplo.
2. Prueba `--sma 50`, `100`, `150`, `200`. Si solo funciona con un número exacto,
   es sobreajuste: desconfía. Una estrategia robusta funciona razonablemente con todos.
3. Deja correr `papel` al menos 1–3 meses. Si en vivo se comporta muy distinto al
   backtest, algo está mal: no pongas dinero hasta entender por qué.
