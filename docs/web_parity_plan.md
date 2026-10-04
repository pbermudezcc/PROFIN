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
