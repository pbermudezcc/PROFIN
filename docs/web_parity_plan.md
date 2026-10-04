# FAMILYFIN — Web con las mismas capacidades que la app de escritorio

**Objetivo de la próxima sesión** (fijado el 2026-10-04): gestión integral de FAMILYFIN desde la web
(iPhone/iPad), con el **iPad como uso principal**. Todo lo que hoy solo se puede hacer en el PC debe
poder hacerse desde el navegador, con la misma seguridad (HTTPS, sesiones, CSRF, datos por usuario)
y el mismo diseño.

Este documento es el punto de partida, no el plan cerrado: la sesión empieza revisándolo con el
usuario y priorizando.

## Estado al cerrar v0.5.0

Base técnica ya hecha (ver `app/web/`, `docs/agent/agent_log.md` 2026-10-04):
- Servidor FastAPI + uvicorn embebido en la app de escritorio, HTTPS con CA propia, onboarding por QR.
- Páginas Jinja2 + htmx, gráficos Chart.js, PWA, claro/oscuro, layout móvil (tab bar) y tablet (sidebar).
- Patrón para nuevas funciones: **servicio sin Qt en `app/…` (session, user_id) → router en
  `app/web/routers/` → plantillas**; la vista Qt pasa a usar el mismo servicio.
- Escrituras: `audit()` + `notify_change()` (refresca el escritorio), CSRF y propiedad del usuario
  comprobados en cada endpoint, tests en `tests/test_web.py`.

| Área del escritorio (`NAV_ITEMS` en `app/config.py`) | Web hoy | Falta para paridad |
|---|---|---|
| Posición Global (dashboard) | Resumen completo | — (revisar KPIs que falten) |
| Gastos / Ingresos | Completo (periodos, gráficos, detalle) | — |
| Transacciones | Lista, filtros, cambiar categoría, aplicar reglas, detalle lateral en iPad (W6–W7) | Marcar/resolver duplicados, edición de campos si procede (no existen en escritorio) |
| Inversiones | Posiciones, evolución, distribución, operaciones (alta/edición/borrado), fiscalidad + Excel | Buscar y dar de alta valores nuevos (Yahoo), actualizar cotizaciones, eventos pendientes (dividendos/splits), importar extractos DEGIRO/MyInvestor con vista previa editable, precio manual de fondos |
| Categorías | **Hecho (W7)**: crear, renombrar, borrar con aviso de uso, cargar por defecto | — |
| Reglas | **Hecho (W7)**: crear, editar, borrar, prioridad ↑↓, vista previa de coincidencias, aplicar (protege manuales) | Reglas globales solo desde el PC (decisión) |
| Importar (extractos CaixaBank) | **Hecho (W8)**: subida, vista previa con duplicados, confirmación, historial | Probar con un extracto real desde el iPad |
| Exportar | No existe (también es placeholder en escritorio) | Definir con el usuario |
| Previsiones | No existe (placeholder en escritorio) | Definir con el usuario |
| Configuración | No existe | Ajustes de usuario (contraseña, tema por defecto); copias de seguridad (crear/descargar); lo que afecte al PC (carpeta de datos, acceso web) probablemente solo en escritorio |
| Cuenta de usuario (menú de MainWindow) | No existe | Cambiar contraseña; borrar cuenta (con confirmación fuerte) |

## Avance 2026-10-04 (W6–W8)

- **W6** rediseño profesional: Inter, barra lateral de iconos (iPad vertical) / completa, grupo
  Administración, migas de pan, tablas ordenables, maestro-detalle en Movimientos, diálogo de
  confirmación propio, «Más» → «Ajustes».
- **W7** categorías y reglas (`app/categorization/admin_service.py`, compartido con el escritorio).
- **W8** importación de extractos por subida (`app/ingestion/import_service.py`, `app/web/uploads.py`).
- Pendiente: **W9** inversiones (alta de valores Yahoo, cotizaciones en segundo plano, eventos
  pendientes con descartes en BD, importar DEGIRO/MyInvestor) y **W10** ajustes y cuenta (cambiar
  contraseña, borrar cuenta revocando sesiones web, exportación de copia **por usuario**: la copia
  actual incluye a toda la familia y no debe descargarse desde la web).

## Directrices para la sesión

- **iPad primero**: diseñar para 820–1180 px (vertical y horizontal), maestro-detalle en tablas
  largas, objetivos táctiles ≥ 44 px, teclado en pantalla (formularios cortos, `inputmode`), subida de
  ficheros desde la app Archivos. El iPhone sigue funcionando con el layout de tab bar.
- **Sin acciones escondidas**: guardar al tocar o botón siempre visible (lección del selector de
  categorías, `docs/agent/lessons_learned.md`).
- **Seguridad igual o mayor**: subidas con límite de tamaño y tipo, operaciones destructivas con
  confirmación, nada de datos financieros en caché del dispositivo, todo auditado.
- **Red**: las funciones que llaman a Internet (Yahoo) se ejecutan en el servidor (PC), en segundo
  plano, nunca bloqueando la petición web.
- **Probar en navegador real** (iPad o Chrome headless a tamaño iPad) cada flujo táctil de punta a
  punta: toque → petición → base de datos.
- Fases cortas y publicables, como W1–W5 y F1–F7.

## Cómo probar la web (para cualquier sesión)

- Tests: `python -m pytest tests/test_web.py -q` (cliente HTTPS de LAN simulado, dos usuarios
  `ana`/`beto`, CSRF y Origin). Suite completa antes de publicar.
- Vista previa con datos de DEMO (nunca los reales): `python tools/web_preview.py [--reset]` →
  http://localhost:8765, usuario `demo` / `demo-pass`. Cambios de CSS/JS/plantillas sin reiniciar;
  los de Python sí (y reiniciar cierra la sesión).
- Tamaños de dispositivo sin redimensionar la ventana: `/__frame?w=820&h=1180&src=/movimientos`
  (iPad vertical 820, horizontal 1180, iPhone 390).
- Probar `/importar` en el navegador: `fetch('/__demo-extracto.xlsx')` → `new File(...)` →
  `DataTransfer` → `input.files`, y pulsar «Subir y revisar» (la herramienta de subida de Chrome
  no lee carpetas temporales).
- Recorrer cada flujo táctil de punta a punta (toque → petición → base de datos → toast) y leer la
  consola. Errores típicos ya vistos: `docs/agent/lessons_learned.md` (hx-target heredado,
  atributos `data-*` que chocan, refrescar `<body>` borra el toast, cookies `__Host-` en http).

## W9 — Inversiones: administración desde la web (brief para la próxima sesión)

Objetivo: lo que hoy solo se hace en el PC dentro de Inversiones. Revisar con el usuario el orden
y el alcance antes de empezar; propuesta en sub-fases publicables:

| Sub-fase | Qué | Lógica existente (sin Qt) | Qué falta |
|---|---|---|---|
| W9a | Buscar y dar de alta valores (Yahoo) | `YahooProvider.search` / `.security_info` (`app/investments/market_data.py`), `security_from_info`, `find_or_create_security` (`app/investments/prices.py`) | Extraer de `SecuritySearchDialog._add/_on_info` (`app/gui/investment_dialogs.py`) el «¿ya existe?» + alta; la búsqueda llama a Internet → hacerla en el servidor con tiempo límite; el catálogo de valores es compartido (sin `user_id`) |
| W9b | Actualizar cotizaciones | `refresh_market_data(session, provider)`, `is_stale`, `latest_prices` (`prices.py`) | El hilo actual es Qt (`PriceRefresher`/`PriceRefreshWorker`, `app/investments/refresh_worker.py`). Crear un ejecutor en segundo plano sin Qt con su propia sesión, **un único candado compartido con el escritorio** (nunca dos descargas a la vez sobre SQLite), estado consultable por la web (htmx polling) y aviso al escritorio con `notify_change(..., "investments")`. La web nunca debe bloquear la petición |
| W9c | Eventos pendientes (dividendos/splits) | `fetch_corporate_actions`, `held_symbols`, `pending_events` (`app/investments/events.py`) | Los «descartados» viven en QSettings por usuario (`investments_view._dismissed_key`) → moverlos a la BD (tabla nueva = migración de esquema + aviso en las notas) para compartirlos entre PC y web; «Registrar» = abrir `partials/op_form.html` precargado |
| W9d | Importar extractos DEGIRO/MyInvestor | `parse_any`, `already_imported`, `symbols_to_fetch`, `fetch_security_infos`, `fetch_closes`, `estimate_quantities`, `build_drafts`, `apply_import` (`app/investments/importers/service.py`), `ledger.import_drafts` | Vista previa **editable** (símbolo y tipo por fila) que hoy edita el objeto en `ImportStatementDialog`; reutilizar el patrón de W8 (`app/web/uploads.py`: token ligado a usuario + sesión, carpeta temporal, TTL) y su límite de tamaño (añadir la ruta a `UPLOAD_PATHS` en `app/web/security.py`) |
| W9e | Precio manual de fondos | `set_manual_price(session, security_id, day, close)` | No hay pantalla ni en el PC: confirmar con el usuario si se quiere |

Reglas a mantener (ver también «Directrices»): servicio sin Qt compartido con la vista del
escritorio → router → plantilla; `audit()` + `notify_change()`; propiedad y CSRF en cada escritura;
todo lo que llame a Internet en el servidor y en segundo plano; tests en `tests/test_web.py`
(simular el proveedor de mercado, nunca red real en tests).

Después: **W10** ajustes y cuenta (cambiar contraseña → nueva `change_password` en
`app/auth/auth_service.py`; borrar cuenta extrayendo `MainWindow._do_delete` y revocando sus
sesiones web; exportación **por usuario** en lugar de `backup_all_data`).
