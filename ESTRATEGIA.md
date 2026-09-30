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
python estrategia.py backtest            # datos reales de Binance desde 2018
python estrategia.py backtest --sma 50   # prueba otra media
python estrategia.py senal               # qué % tener hoy
python estrategia.py senal --telegram    # y mandarlo a tu bot (usa tu .env)
python estrategia.py backtest --sintetico  # prueba offline
```

Para recibir la señal diaria automáticamente (Linux/VPS), en `crontab -e`:

```
10 0 * * * cd /ruta/huskassistant && ./.venv/bin/python estrategia.py senal --telegram
```

## Antes de poner dinero

1. Corre `backtest` con datos reales y compáralo con buy & hold: mira **máx. caída**
   y **Calmar**, no solo el múltiplo.
2. Prueba `--sma 50`, `100`, `150`, `200`. Si solo funciona con un número exacto,
   es sobreajuste: desconfía. Una estrategia robusta funciona razonablemente con todos.
3. Síguela en papel 1–3 meses antes de usar capital real.
