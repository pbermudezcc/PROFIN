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
| F2 | Proveedor de cotizaciones + caché + actualización en segundo plano + tests con proveedor simulado | pendiente |
| F3 | Pestaña Operaciones: diálogo de alta/edición y listado | pendiente |
| F4 | Pestaña Posiciones + tarjetas + refresco (primera versión usable) | pendiente |
| F5 | Gráficas de evolución, índice de referencia y distribución | pendiente |
| F6 | Dividendos, splits e informe fiscal anual FIFO | pendiente |
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

## Riesgos
- `yfinance` no es oficial y puede dejar de funcionar si Yahoo cambia su web → proveedor aislado, caché local y alternativas (Stooq, Twelve Data).
- Poca cobertura de fondos por ISIN en Yahoo → NAV manual.
- Trade Republic y Scalable operan en EUR en mercados alemanes → el ticker de Yahoo es distinto (p. ej. `APC.DE`), así que cada valor se guarda con su lugar de cotización.
