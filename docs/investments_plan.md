# Plan — Módulo de Inversiones

Estado: **aprobado el diseño, pendiente F0** (2026-09-27).
Sustituye al `_PlaceholderView("Inversiones")` de `app/gui/main_window.py`.

## Decisiones tomadas con el usuario
- Vista **solo por usuario** (sin consolidado "Familia" por ahora).
- Activos: **acciones, ETFs y fondos de inversión** (sin cripto).
- Mercados: **principalmente EE. UU.** comprado a través de brókers europeos en cuentas EUR.
- Brókers: **DEGIRO, Trade Republic, MyInvestor / Scalable / otros**.
- Entrada de operaciones: **manual primero**; importación CSV del bróker como fase opcional (F7).
- Cada operación guarda el **precio en la divisa original + el importe total cargado en EUR**
  (según el extracto), junto con el tipo de cambio implícito y las comisiones.

## Principios
- Las posiciones se **calculan** a partir de las operaciones (compra, venta, dividendo, split). Nunca se guardan a mano.
- Ganancias realizadas por **FIFO**, igual que en el IRPF español.
- Proveedor de cotizaciones **intercambiable** (`yfinance` por defecto). Los precios y tipos de cambio se guardan en SQLite, así que la app funciona sin conexión y avisa cuando los datos son antiguos.
- Todo se muestra en **EUR**. Los tipos de cambio salen de Yahoo (`EURUSD=X`).
- El listado de valores es compartido entre usuarios; las operaciones son siempre de un solo usuario.
- Los fondos sin cotización en Yahoo admiten un **valor liquidativo (NAV) manual**.

## Vista
- Barra superior: filtro por bróker, "última actualización", botón de refresco y botón "+ Operación".
- Tarjetas de resumen: valor de mercado, capital invertido, ganancia latente (€ y %), variación del día, realizado en el año, dividendos del año y rentabilidad anualizada (XIRR).
- Pestañas: Posiciones · Operaciones · Evolución (con índice de referencia) · Distribución · Dividendos y fiscalidad.

## Fases
| # | Fase | Estado |
|---|------|--------|
| F0 | Prueba técnica: yfinance desde la red de casa, tickers reales (USD y EUR), búsqueda por ISIN de los fondos, empaquetado con PyInstaller | **hecho** 2026-09-27 |
| F1 | Esquema (`securities`, `investment_transactions`, `price_history`, `fx_rates`) + migración v4 + cálculos (posiciones, FIFO, P/L, XIRR) + tests | **hecho** 2026-09-27 |
| F2 | Proveedor de cotizaciones + caché + actualización en segundo plano + tests con proveedor simulado | **hecho** 2026-09-27 |
| F3 | Pestaña Operaciones: diálogo de alta/edición y listado | **hecho** 2026-09-27 |
| F4 | Pestaña Posiciones + tarjetas + refresco (primera versión usable) | **hecho** 2026-09-27 |
| F5 | Gráficas de evolución, índice de referencia y distribución | **hecho** 2026-09-27 |
| F6 | Dividendos, splits e informe fiscal anual FIFO | **hecho** 2026-09-27 |
| F7 | Importación CSV (DEGIRO, Trade Republic, MyInvestor, Scalable) — opcional | pendiente |
| F8 | Retoques finales y versión 0.5.0 | pendiente |

## Resultados F0 (2026-09-27, yfinance 1.7.0)
- `yf.Search(texto)` encuentra valores por ticker o por nombre y devuelve símbolo, tipo, bolsa y nombre → sirve para el autocompletado del formulario.
  Los tickers cortos son ambiguos: `ENR` → Energizer (NYSE) o Siemens Energy (`ENR.DE`); `TKE` → Take-Two en Xetra (`TKE.DE`);
  `AMZ` → Amazon en Xetra (`AMZ.DE`, en EUR) frente a `AMZN` (NASDAQ, en USD); `GEV` → NYSE en USD, y en Alemania `Y5C.DE`.
  Por eso el formulario debe mostrar siempre la bolsa y la divisa.
- Fondo Boreas Cartera Crecimiento R FI → `0P00008XG7.F` (MUTUALFUND, EUR). Hay valor liquidativo diario
  con 2–3 días hábiles de retraso (lo normal en fondos). Yahoo no devuelve el ISIN (`Ticker.isin` = "-"), así que
  guardaremos el ISIN como dato opcional introducido por el usuario.
- `info`/`fast_info` tarda ~1,5 s por ticker → leer los datos descriptivos (nombre, sector, país, divisa) **una sola vez** al dar de alta el valor.
  Para los precios, usar `yf.download([...])` por lotes (11 tickers × 1 año ≈ 1 s).
- Tipos de cambio históricos `EURUSD=X` disponibles; dividendos y splits disponibles en `Ticker.dividends/.splits`.
- PyInstaller 6.22: el ejecutable con yfinance funciona sin hooks extra (curl_cffi incluido); añade ~12 MB
  (lxml 6,7 MB + curl_cffi 3,8 MB). Hay que respetar el límite de rutas de 260 caracteres de Windows (lxml incluye rutas profundas).
- `yfinance>=1.7.0` añadido a `requirements.txt`.

## Resultados F1 (2026-09-27)
- Valores confirmados: ENR = Siemens Energy (`ENR.DE`), TKE = Take-Two (`TKE.DE`), AMZ = Amazon (`AMZ.DE`),
  GEV = GE Vernova en NYSE (`GEV`, USD) comprada en **EquatePlus** con compras normales (sin RSU ni ESPP).
  La operación `grant` existe en el esquema, pero el formulario (F3) la ocultará por defecto.
- Modelos en `app/db/models.py`: `Security`, `InvestmentTransaction`, `PriceHistory`, `FxRate`,
  y los enums `AssetType` e `InvestmentKind`. No se puede borrar un valor que tiene operaciones (RESTRICT);
  al borrar un usuario se borran sus operaciones (CASCADE).
- Migración v4 (`_migration_4_investments`): solo añade tablas. Ensayo sobre una copia de la base real: v3→v4 en 0,03 s,
  8.727 transacciones y sumas idénticas, integridad correcta.
- Cálculos en `app/investments/portfolio.py` (posiciones, lotes FIFO, `ledger_error`, `value_position`,
  `realized_by_year`) y `app/investments/returns.py` (`investor_cash_flows`, `xirr`). Tests en
  `tests/test_investments_portfolio.py` y `test_migration_4_…`.

## Resultados F2 (2026-09-27)
- `app/investments/market_data.py`: protocolo `MarketDataProvider` + `YahooProvider` (`search`, `security_info`,
  `daily_closes` por lotes). `yfinance` se importa solo cuando hace falta. Los fallos de red se convierten en
  `MarketDataError` con un mensaje en castellano. Las divisas en céntimos (GBp) se convierten con el tipo de GBP ×100.
- `app/investments/prices.py`: `refresh_market_data` (primera descarga desde la primera operación −7 días;
  después, solo los últimos 5 días; nunca sobrescribe precios manuales), `latest_prices` (con el cierre anterior para
  la variación del día), `fx_rate`, `set_manual_price`, `find_or_create_security`, `is_stale` (15 min).
- `app/investments/refresh_worker.py`: `PriceRefresher` (un hilo con su propia sesión, nunca dos actualizaciones a la vez,
  guarda la hora de `last_success`). La vista de F4 llamará a `refresh_if_stale()` al mostrarse y a `refresh()` desde el botón.
- Prueba real (base de datos temporal, 5 valores desde 01/2025): catálogo 1,5 s; primera actualización 0,4 s
  (1.986 precios + 453 tipos de cambio); siguientes 0,2 s. GEV 957,63 USD → 839,94 EUR.
- Pendiente para F6: el "Close" de Yahoo se ajusta por splits, así que cuando haya un split habrá que volver
  a descargar el histórico completo de ese valor.

## Resultados F3 (2026-09-27)
- `app/investments/ledger.py`: `save_transaction` y `delete_transaction` repasan todas las operaciones del valor
  antes de escribir (se rechaza editar o borrar algo que dejaría imposible una venta posterior) y filtran siempre
  por usuario. También `list_transactions`, `known_brokers`, `picker_securities` y `estimate_amount_eur`.
- `app/gui/investments_view.py`: barra superior (estado de las cotizaciones, botón de refresco, "+ Operación") y
  pestañas Posiciones (provisional hasta F4) y Operaciones (filtros, tabla ordenable, editar con doble clic
  o menú contextual, borrar con confirmación y tecla Supr, pantalla vacía con botón para la primera operación).
- `app/gui/investment_dialogs.py`: `TransactionDialog` (importe en EUR estimado hasta que el usuario lo edita,
  "Usar" el cierre del día, "Tienes N… Vender todas", errores dentro del propio diálogo) y `SecuritySearchDialog`
  (búsqueda en Yahoo en segundo plano, con la bolsa visible e ISIN opcional).
- `app/gui/_background.py`: llamadas a internet en hilos propios que se retienen hasta terminar; la ventana
  principal espera a que acaben al cerrarse (`closeEvent`). Los hilos de refresco abren su sesión sobre la misma
  base de datos que la vista.

## Resultados F4 (2026-09-27)
- `app/investments/snapshot.py`: `portfolio_snapshot(session, user_id, as_of, broker)` → filas por valor
  (valor de mercado en EUR, coste FIFO, ganancia no realizada, variación del día con el tipo de cambio de cada día,
  peso, realizado, dividendos) y totales (realizado y dividendos del año y totales, XIRR). Un valor sin precio
  o sin tipo de cambio se valora a coste y se avisa; un precio de más de 7 días se marca como antiguo.
- `app/gui/investment_positions.py`: 7 tarjetas (reutilizan `_KPICard` del panel principal), aviso de precios
  que faltan o están antiguos, tabla ordenable por valor, opción de mostrar posiciones cerradas y filtro por bróker.
  Doble clic en una posición → Operaciones filtradas por ese valor. La XIRR solo se muestra con al menos 30 días de historial.
- La vista abre en Posiciones si el usuario ya tiene operaciones, y en Operaciones si todavía no tiene ninguna.
  Las posiciones se recargan después de cada actualización de cotizaciones.

## Resultados F5 (2026-09-27)
- Índices de referencia (`app/investments/benchmarks.py`), todos en EUR: MSCI World `IWDA.AS` (por defecto),
  S&P 500 `SXR8.DE`, IBEX 35 `^IBEX`. Se guardan como un valor más del catálogo y se actualizan junto con
  vuestras posiciones (solo si hay operaciones).
- `app/investments/performance.py`: `portfolio_history` (valor diario, capital invertido FIFO y «el mismo dinero
  en el índice», con las mismas aportaciones y retiradas) y `allocation` (por valor, bróker, divisa o tipo de activo).
- `app/gui/investment_charts.py`: pestaña Evolución (periodos 1M/6M/Año/1A/3A/Todo, selector de índice que se
  recuerda en la configuración, lectura al pasar el ratón, frase de resumen) y pestaña Distribución (gráfico circular
  o de barras del panel principal). Solo se recalculan cuando su pestaña está a la vista.
- Corregido de paso un fallo del gráfico circular de Posición Global (`dashboard._paint_donut`): los sectores
  se superponían.

## Resultados F6 (2026-09-27)
- Decisiones: proponer los dividendos y splits detectados en Yahoo (el usuario confirma el neto); informe en pantalla
  y en Excel; marcar la regla antirrecompra; una sola retención (en origen), sin migración.
- `market_data.corporate_actions` + `app/investments/events.py`: un dividendo se considera pendiente si ese día
  tenías acciones y no hay ningún dividendo de ese valor registrado entre 5 días antes y 60 días después de la
  fecha ex-dividendo. Un split, si no hay ninguno registrado en ±7 días. Los descartados se guardan en QSettings
  (`investments/dismissed_events/<user_id>`). La comprobación se hace tras actualizar las cotizaciones, como mucho cada 6 h.
- Splits: al guardar o borrar un split se borran los precios descargados de ese valor
  (`prices.reset_price_history`), porque Yahoo recalcula todo el histórico anterior al split, y se vuelve a descargar.
  `portfolio.split_factor_after` corrige esos precios en el histórico de la cartera y en la sugerencia de
  «cierre del día» del formulario.
- `app/investments/tax_report.py`: ventas por lote FIFO, regla antirrecompra (art. 33.5.f de la Ley del IRPF:
  2 meses para acciones y ETF cotizados, 1 año para fondos; lo recomprado bloquea en orden FIFO la parte de la pérdida
  que corresponde), dividendos (íntegro = neto + retención) y `export_xlsx` (hojas Resumen, Ganancias y pérdidas,
  Dividendos). Pestaña Fiscalidad en `app/gui/investment_tax.py`.
- Aviso en la vista («Yahoo Finance indica N dividendos… Revisar») → `PendingEventsDialog` → formulario prerrellenado.

## Riesgos
- `yfinance` no es oficial y puede dejar de funcionar si Yahoo cambia su web → proveedor aislado, caché local y alternativas (Stooq, Twelve Data).
- Poca cobertura de fondos por ISIN en Yahoo → NAV manual.
- Trade Republic y Scalable operan en EUR en mercados alemanes → el ticker de Yahoo es distinto (p. ej. `APC.DE`), así que cada valor se guarda con su lugar de cotización.
