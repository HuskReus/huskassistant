# Estrategia: tokens recién creados (momentum temprano con filtro anti-rug)

Archivo: `huskagent.py` (el radar). Como todo HuskAssistant, **solo detecta, puntúa
y avisa; nunca compra ni toca llaves**. Tú ejecutas.

## Aviso honesto primero

Los tokens nuevos son el rincón más peligroso de cripto. De los miles de pools que
se crean al día, **la gran mayoría termina en cero** (rug pull, honeypot, dev que vende,
o simplemente nadie compra). Los ganadores existen y pueden dar 5x–100x, pero son
pocos: los resultados siguen una ley de potencia. Por eso la estrategia no busca
"acertar siempre" sino:

1. **Eliminar lo que es trampa** (filtros duros de seguridad).
2. **Entrar solo donde ya hay tracción orgánica** (no en el minuto 1).
3. **Perder poco en cada fallo y dejar correr los pocos aciertos** (salidas fijas).

Con una tasa de acierto de 25–35% puede salir positiva *si* respetas los stops.
Si no respetas los stops, no hay estrategia que te salve.

## 1. Dónde buscar (percepción)

- Fuente: `new_pools` de GeckoTerminal en las redes de `CONFIG["networks"]`
  (por defecto Solana y Base: barato operar, mucho flujo de lanzamientos).
- Ventana de edad: **30 minutos a 96 horas**. Los primeros minutos los dominan
  snipers, bundles y bots: entrar ahí es darles tu liquidez. Esperar 30–60 min
  cuesta algo de subida pero filtra la mayoría de rugs instantáneos.
- Punto dulce de puntuación: **1 a 24 horas** de vida.

## 2. Filtros duros de mercado (baratos, se aplican primero)

| Filtro | Valor | Por qué |
|---|---|---|
| Liquidez | ≥ $20k | Menos = slippage alto y rug barato |
| FDV | ≤ $5M | Arriba ya no es "temprano"; poco espacio para 2x rápido |
| Transacciones 24h | ≥ 150 | Actividad real, no 3 wallets moviéndose |
| Compradores únicos 1h | ≥ 25 | Tracción de personas distintas |
| Subida 1h | ≤ +150% | No perseguir velas verticales (sueles comprar el techo) |
| Compras sin ninguna venta | descartado | Síntoma clásico de honeypot |

## 3. Filtros duros de seguridad (fail-closed: si la API no responde, se descarta)

**EVM (Base, BSC, Ethereum) vía GoPlus:** honeypot, no se puede vender todo,
owner oculto, owner que cambia balances, transferencias pausables, mint abierto,
blacklist, selfdestruct, contrato no verificado, impuesto > 10%, top 10 holders
(sin LP/lockers) > 35%, LP quemado/bloqueado < 80%.

**Solana vía RugCheck:** cualquier riesgo nivel `danger` (mint/freeze authority
activa, holders concentrados, etc.) y LP bloqueado < 80%.

## 4. Puntuación de tracción (0–100, umbral 62)

| Componente | Máx | Cómo |
|---|---|---|
| Liquidez | 20 | $5k = 1 punto |
| Compradores únicos 1h | 20 | 5 compradores = 1 punto |
| Presión compradora | 15 | compras/ventas 1h: 1.0 → 0, 2.0 → 15 |
| Volumen 24h / liquidez | 15 | 5x = máximo (rotación sana) |
| Momentum 1h | 15 | 0 a +100% = 15; > +100% = 5; negativo = 0 |
| Edad | 10 | 1–24h = 10 |
| FDV < $1M | 5 | Espacio para subir |

≥ 62 → **alerta** · 50–61 → **vigilar** (se re-evalúa cada ciclo) · < 50 → descartar.
Con `ANTHROPIC_API_KEY` el modelo razona sobre los mismos datos más su propio
historial de aciertos.

## 5. Cómo operar la alerta (esto lo haces tú)

**Antes de entrar (2 minutos):** abre el link del pool; mira que el gráfico no sea
una sola vela, que las compras vengan de muchas wallets distintas, y que haya
redes sociales reales (X/Telegram con gente, no bots). Si algo huele raro, pasa.

**Tamaño:**
- Define un **bankroll "degen"**: máximo 5–10% de tu capital cripto, dinero que
  aceptas perder entero.
- Cada operación: **tamaño fijo de 2–5% del bankroll degen**. Nunca promedies a la baja.
- Máximo **5 posiciones abiertas** a la vez.
- **Límite de pérdida diaria:** si pierdes 3 stops en un día, paras hasta mañana.

**Entrada:** compra escalonada o de una vez con slippage ≤ 5–10%. Si necesitas más
slippage, la liquidez no da: no entres.

**Salidas (fijas, se deciden ANTES de entrar):**
1. **Stop -40%**: sales entero. Sin excepciones, sin "ya rebota".
2. **TP1 en 2x**: vendes el **50%** → recuperaste tu capital; lo que queda es gratis.
3. **Resto con trailing stop -30% desde el pico** (o vende otro 25% en 5x y deja
   un 25% "moonbag").
4. **Time stop**: si en 72h no pasó de +30%, sales y liberas el capital.
5. Salida inmediata si: el dev vende fuerte, se retira liquidez, o GoPlus/RugCheck
   cambia a peligro.

El radar te avisa por Telegram cuando un token alertado toca 2x (🎯), el stop (🛑)
o el time stop (⌛), para que ejecutes la regla.

## 6. Aprendizaje y validación

Cada alerta queda en `huskagent.db` y se resuelve con las mismas reglas de salida
(2x = acierto; -40% o time stop = fallo). Pregúntale al asistente "estado del radar"
para ver la tasa de acierto.

**Antes de poner dinero real:**
1. Corre el radar **2–4 semanas en papel** y junta al menos 30 alertas resueltas.
2. Calcula la expectativa: `acierto × ganancia media − fallo × 0.40`. Con TP1 en 2x
   (≈ +50% de la posición asegurado más el resto), necesitas > ~30% de acierto.
3. Si la tasa es baja, sube `score_threshold` o `min_liquidity_usd`, no bajes los stops.
4. Limitación: el precio se muestrea cada 8 minutos; mechas cortas pueden no
   registrarse y en la vida real el slippage empeora las salidas.

## Uso

**Windows:** doble clic en `radar.bat` (la primera vez te abre `.env` para poner
tus datos de Telegram). Para un solo ciclo de prueba: `radar.bat --once`.

**Linux/VPS:**

```bash
python huskagent.py            # bucle continuo (cada 8 min)
python huskagent.py --once     # un solo ciclo, para probar
```

Configura redes y umbrales en `CONFIG` al inicio de `huskagent.py`. Para correrlo
24/7 en el VPS: `tmux new -s radar` → `./.venv/bin/python huskagent.py`.
