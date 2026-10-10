<div align="center">

# FAMILYFIN

**Gestor de finanzas personales y familiares para Windows, con acceso desde iPhone/iPad en la red de casa.**

[![Versión](https://img.shields.io/badge/versión-0.7.0-2563EB)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Qt](https://img.shields.io/badge/UI-PySide6%20%28Qt%206%29-41CD52?logo=qt&logoColor=white)](https://doc.qt.io/qtforpython-6/)
[![Plataforma](https://img.shields.io/badge/plataforma-Windows%2010%2F11-0078D6?logo=windows&logoColor=white)](#requisitos)
[![Tests](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)](#tests)

[Características](#características) ·
[Instalación](#instalación) ·
[Uso](#uso) ·
[Especificación](#especificación-funcional) ·
[Arquitectura](#arquitectura) ·
[Desarrollo](#desarrollo)

</div>

---

## Tabla de contenidos

- [Sobre el proyecto](#sobre-el-proyecto)
- [Características](#características)
- [Stack tecnológico](#stack-tecnológico)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Uso](#uso)
- [Especificación funcional](#especificación-funcional)
- [Arquitectura](#arquitectura)
- [Datos, copias de seguridad y privacidad](#datos-copias-de-seguridad-y-privacidad)
- [Desarrollo](#desarrollo)
- [Solución de problemas](#solución-de-problemas)
- [Roadmap](#roadmap)
- [Versionado](#versionado)
- [Contribuir](#contribuir)
- [Seguridad](#seguridad)
- [Licencia](#licencia)

---

## Sobre el proyecto

**FAMILYFIN** (el repositorio se llama `PROFIN`) es una aplicación de escritorio que:

1. **importa** los extractos del banco,
2. **categoriza** los movimientos automáticamente con reglas,
3. **muestra** en qué se va el dinero, y
4. lleva el **seguimiento de una cartera de inversiones** con su rentabilidad y su fiscalidad.

Nació para una familia, pero cualquier persona u hogar puede usarla. Cada usuario tiene sus propios
datos, separados y protegidos por contraseña.

### Principios de diseño

| Principio | Qué significa |
|---|---|
| **Local primero** | Todo se guarda en tu PC. No hay nube, servidor externo ni cuentas en terceros. |
| **Nada se guarda sin confirmar** | Las importaciones muestran una vista previa y no escriben nada hasta que confirmas. |
| **Lo manual manda** | Las reglas automáticas nunca sobrescriben una categoría puesta a mano. |
| **Una sola lógica** | El escritorio y la web comparten el mismo código de negocio y se comportan igual. |
| **Datos protegidos** | Hay migraciones automáticas con copia previa y copias de seguridad periódicas. |

La única conexión a Internet que hace la app es la descarga **opcional** de cotizaciones desde Yahoo Finance.

---

## Características

| Área | Funcionalidad |
|---|---|
| 📊 **Resumen** | Ingresos, gastos, ahorro y evolución por periodo |
| 💸 **Gastos / Ingresos** | Desglose por categoría con gráficos y comparativa entre periodos |
| 🔎 **Movimientos** | Buscador y filtros, cambio de categoría y aplicación a movimientos iguales |
| 📥 **Importar** | Extractos Excel de CaixaBank con vista previa y detección de duplicados |
| 🏷️ **Categorías y reglas** | Árbol de categorías y reglas automáticas por comercio o concepto, con prioridades |
| 📈 **Inversiones** | Cartera FIFO, cotizaciones, TIR, comparación con índices, informe fiscal, importación de DEGIRO y MyInvestor |
| 📱 **Acceso web** | Versión web para iPhone/iPad en la red local, siempre por HTTPS |
| ⚙️ **Configuración** | Tema claro/oscuro, carpeta de datos, copias de seguridad automáticas |

---

## Stack tecnológico

| Capa | Tecnología |
|---|---|
| Lenguaje | Python 3.11 |
| Interfaz de escritorio | [PySide6](https://doc.qt.io/qtforpython-6/) (Qt 6) + [qtawesome](https://github.com/spyder-ide/qtawesome) |
| Persistencia | SQLite + [SQLAlchemy 2](https://www.sqlalchemy.org/), con migraciones de esquema automáticas |
| Lectura de Excel | pandas, openpyxl, xlrd |
| Seguridad | bcrypt (contraseñas), cryptography (certificado TLS propio) |
| Web local | [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn + Jinja2 (servidor dentro de la app de escritorio) |
| Datos de mercado | [yfinance](https://github.com/ranaroussi/yfinance) |
| Tests | pytest (+ httpx para el cliente de pruebas de FastAPI) |
| Empaquetado | PyInstaller |

---

## Requisitos

- **Windows 10 u 11.** Es la plataforma objetivo, porque el empaquetado y las rutas de datos usan `%LOCALAPPDATA%`.
- **Python 3.11**, solo si se ejecuta desde el código fuente.
- *(Opcional)* Conexión a Internet para las cotizaciones.
- *(Opcional)* iPhone/iPad en la **misma red Wi-Fi** que el PC para el acceso web.

---

## Instalación

### Opción A: ejecutable (usuarios finales)

1. Descarga el `.zip` de la última versión desde **[Releases](https://github.com/pbermudezcc/PROFIN/releases/latest)**.
2. Descomprímelo en una carpeta, por ejemplo `C:\Programas\FAMILYFIN`.
3. Ejecuta `FAMILYFIN.exe`.

No hace falta instalar Python.

### Opción B: desde el código fuente (desarrolladores)

```powershell
git clone https://github.com/pbermudezcc/PROFIN.git
cd PROFIN

# Entorno virtual aislado
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# Dependencias
python -m pip install --upgrade pip
pip install -r requirements.txt

# Arrancar
python main.py
```

> [!IMPORTANT]
> **pandas está fijado a la versión `3.0.3`.** En algunos equipos con *Smart App Control* de Windows,
> las versiones 3.0.5/3.0.6 se bloquean al cargar sus DLL nativas. No cambies la versión sin probarla
> antes en un entorno aislado.

### Opción C: generar tu propio ejecutable

```powershell
build_profin.bat
```

El resultado se genera en `dist\`. Por debajo llama a `tools\build_windows.py`.

---

## Uso

### Primeros pasos

1. **Crea tu usuario.** En la pantalla de inicio, pulsa **«¿Primera vez? Crear cuenta»**. Cada miembro del hogar puede tener su propio usuario.
2. **Prepara las categorías.** En **Categorías**, carga las categorías por defecto o crea las tuyas.
3. **Importa un extracto.** Descarga el Excel de movimientos desde la banca online de CaixaBank y ábrelo en **Importar**.
   Revisa la vista previa (nuevos, posibles duplicados, ya importados) y confirma.
4. **Categoriza.** Al asignar una categoría a un movimiento puedes además:
   - ☑️ *Aplicar también a N movimientos iguales* que aún no tienen categoría;
   - ☑️ *Crear una regla para los próximos* movimientos de ese comercio.
5. *(Opcional)* **Inversiones.** Registra operaciones a mano o importa el estado de cuenta de DEGIRO o MyInvestor.
6. *(Opcional)* **Acceso desde el móvil.** Ve a **Configuración → Acceso web**.

### Acceso desde iPhone / iPad

1. En el PC: **Configuración → Acceso web → Activar**.
2. En el móvil, escanea el **código QR** para instalar el certificado de casa. Solo hace falta la primera vez en cada dispositivo.
3. Abre la dirección que muestra la app (puerto **8765** por defecto) e inicia sesión con tu usuario.
4. *(Opcional)* Safari → Compartir → **Añadir a pantalla de inicio**, para usarla como una app.

---

## Especificación funcional

<details>
<summary><b>Usuarios y sesión</b></summary>

- Usuarios locales con contraseña cifrada con **bcrypt**.
- **Aislamiento por usuario:** cuentas, movimientos, categorías, reglas e inversiones pertenecen a un único usuario.
- Hay reglas **globales** (compartidas) y reglas **personales** de cada usuario.
- Se guarda un **registro de auditoría** (`audit_log`) de las acciones relevantes.

</details>

<details>
<summary><b>Importación de extractos bancarios</b></summary>

- **Formato soportado:** Excel de movimientos de **CaixaBank** (`.xls` / `.xlsx`).
- **Proceso:**
  ```
  lectura → validación → detección de duplicados → categorización con reglas → vista previa → confirmación
  ```
- Cada importación se registra como un **lote** (`ImportBatch`), así se sabe de qué fichero viene cada movimiento.
- **Desde la web** se puede subir el Excel desde la app *Archivos* del iPad. El fichero temporal se borra al terminar, y los ficheros que hayan quedado de importaciones sin confirmar se limpian al arrancar el servidor.

</details>

<details>
<summary><b>Categorización</b></summary>

- La jerarquía tiene dos niveles: **Tipo → Categoría**. Por ejemplo, *Gasto → Supermercado*.
- **Reglas automáticas:** coincidencia exacta o parcial sobre el comercio o el concepto, con **prioridad** configurable.
- Al editar una regla se ve en el momento a qué movimientos afecta.
- **Invariante:** una regla nunca sobrescribe una categoría asignada manualmente.
- **Movimientos iguales:** se consideran iguales los del mismo comercio y del mismo tipo. Un gasto nunca se mezcla con una devolución.

</details>

<details>
<summary><b>Inversiones</b></summary>

- **Operaciones:** compra, venta, dividendo y split, en cualquier divisa.
- **Cartera calculada con FIFO**, el método que exige la normativa fiscal española.
- Cotizaciones y tipos de cambio que se descargan en segundo plano y se guardan para poder trabajar **sin conexión**.
- **Indicadores:** valor, plusvalía, **TIR** (rentabilidad anualizada), realizado y dividendos.
- **Benchmark:** comparación con MSCI World, S&P 500 o IBEX 35 simulando las mismas aportaciones.
- **Distribución** por valor, bróker, divisa o tipo de activo.
- **Informe fiscal anual** de ventas y dividendos con la regla de recompra (2 meses, o 1 año en fondos), exportable a Excel.
- Avisos de **dividendos y splits pendientes** de registrar.
- **Importadores:**
  - **DEGIRO** («Estado de cuenta»): junta ejecuciones parciales, añade comisiones e impuestos y valora los dividendos en euros recibidos.
  - **MyInvestor:** propone tipo, valor y participaciones, que se pueden corregir antes de importar.
  - Los dos importadores evitan duplicar lo que ya se importó.

</details>

<details>
<summary><b>Acceso web</b></summary>

- **Servidor embebido:** se ejecuta dentro de la app de escritorio y se activa o desactiva desde la propia app.
- **Siempre HTTPS**, con una autoridad certificadora local que se genera en el PC.
- **Solo en la red local:** no se expone nada a Internet.
- Sesión por usuario con **cierre automático tras 30 minutos** sin uso.
- **Funciones:** consultar el resumen, los gastos, los ingresos, los movimientos y las inversiones; cambiar categorías; gestionar categorías y reglas; importar extractos; registrar operaciones de inversión.
- Diseño adaptado a iPhone y iPad (vertical y horizontal). Se puede instalar como app en la pantalla de inicio.

</details>

---

## Arquitectura

```
PROFIN/
├── main.py                  # Punto de entrada: logging → BD → login → ventana principal
├── app/
│   ├── config.py            # Nombre, versión y temas (claro/oscuro)
│   ├── db/                  # Modelos SQLAlchemy, conexión y migraciones de esquema
│   ├── auth/                # Alta de usuarios, login y sesión
│   ├── ingestion/           # Parser Excel, validación, duplicados, servicio de importación
│   ├── categorization/      # Categorías, motor de reglas, administración
│   ├── analytics/           # Resúmenes por periodo, dashboard, búsqueda
│   ├── investments/         # Ledger, cartera FIFO, precios, TIR, fiscalidad
│   │   └── importers/       #   DEGIRO, MyInvestor
│   ├── settings/            # Preferencias y copias de seguridad
│   ├── gui/                 # Vistas PySide6 (una por sección)
│   └── web/                 # FastAPI: routers, plantillas Jinja2, TLS, servidor
├── tests/                   # Suite pytest
├── tools/                   # Build, releases, iconos web, previsualización web
├── docs/                    # Planes de trabajo y registro de desarrollo
├── CHANGELOG.md
└── requirements.txt
```

### Capas

```
┌──────────────────────────┐   ┌──────────────────────────┐
│  Escritorio (app/gui)    │   │  Web local (app/web)     │
│  PySide6                 │   │  FastAPI + Jinja2 + TLS  │
└────────────┬─────────────┘   └────────────┬─────────────┘
             └──────────────┬───────────────┘
                            ▼
      ┌────────────────────────────────────────────┐
      │  Lógica de negocio (sin dependencias Qt)   │
      │  ingestion · categorization · analytics    │
      │  investments · auth · settings             │
      └─────────────────────┬──────────────────────┘
                            ▼
      ┌────────────────────────────────────────────┐
      │  Persistencia: SQLAlchemy 2 → SQLite       │
      └────────────────────────────────────────────┘
```

La lógica de negocio **no importa Qt**. El escritorio y la web la comparten, así que se comportan igual
y la lógica se puede probar sin interfaz gráfica.

### Modelo de datos

| Tabla | Contenido |
|---|---|
| `users` | Usuarios y hash de contraseña |
| `accounts` | Cuentas bancarias de cada usuario |
| `categories` | Árbol Tipo → Categoría |
| `transactions` | Movimientos bancarios |
| `import_batches` | Lotes de importación (trazabilidad) |
| `auto_categorization_rules` | Reglas automáticas (globales o personales) |
| `audit_log` | Registro de acciones |
| `securities` | Valores: acciones, ETF, fondos… |
| `investment_transactions` | Operaciones de inversión |
| `price_history` | Cotizaciones históricas guardadas |
| `fx_rates` | Tipos de cambio |

---

## Datos, copias de seguridad y privacidad

| Qué | Ubicación por defecto |
|---|---|
| Base de datos (SQLite) | `%LOCALAPPDATA%\FAMILYFIN\data\` (se puede cambiar en *Configuración*) |
| Logs | `%LOCALAPPDATA%\FAMILYFIN\logs\profin.log` (en desarrollo, en `logs\` del repositorio) |
| Preferencias | Registro de Windows (QSettings, «FAMILYFIN») |

- **Copias automáticas** cada 24 h por defecto, a una carpeta configurable.
- **Migraciones seguras:** antes de actualizar el esquema se guarda `profin.pre-migration-vN.<fecha>.db` junto a la base.
- Los logs rotan automáticamente (1 MB × 3 ficheros).

> [!WARNING]
> El `.gitignore` excluye `*.db`, `data/` y los `.zip` de copias de seguridad.
> **Nunca subas datos financieros reales al repositorio.**

---

## Desarrollo

### Configurar el entorno

Sigue la [Opción B](#opción-b-desde-el-código-fuente-desarrolladores) de la instalación.

### Tests

```powershell
pytest                                 # suite completa
pytest tests/test_rules_engine.py -v   # un módulo
pytest -k "duplicate"                  # por nombre
```

La suite cubre parsers, detección de duplicados, motor de reglas, migraciones, inversiones (FIFO, TIR, fiscalidad, importadores), la web (incluido TLS) y pruebas *smoke* de la interfaz.

### Previsualizar la web

```powershell
python tools/web_preview.py          # http://localhost:8765  (usuario: demo / demo-pass)
python tools/web_preview.py --reset  # regenera la base de datos de demo
```

Usa una base de datos **de demostración** en `dist/web_preview/` y nunca toca los datos reales.

### Convenciones

- **Ramas:** `main` es estable; el trabajo se hace en `feature/<tema>` o `fix/<tema>`.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/es/), por ejemplo `feat(web): …`, `fix(ingestion): …`, `docs: …`, `chore(deps): …`.
- **Antes de tocar** la importación, las migraciones o la base de datos, lee [`docs/agent/lessons_learned.md`](docs/agent/lessons_learned.md).
- La lógica nueva va en las capas sin Qt y se reutiliza desde `gui/` y `web/`.

---

## Solución de problemas

| Síntoma | Causa probable / solución |
|---|---|
| Error al cargar pandas / la página *Importar* aparece como no disponible | *Smart App Control* bloquea las DLL de pandas. Comprueba que tienes `pandas==3.0.3`. |
| El iPhone muestra «Conexión no privada» | El certificado de casa no está instalado o no es de confianza. Vuelve a escanear el QR y actívalo en *Ajustes → General → Información → Ajustes de confianza de certificados*. |
| No se puede abrir la web desde el móvil | El PC y el móvil deben estar en la misma Wi-Fi, y el Firewall de Windows debe permitir el puerto (8765 por defecto). |
| Las cotizaciones no se actualizan | Comprueba la conexión a Internet. La app sigue funcionando con los últimos precios guardados. |
| Algo falla y no sabes qué | Revisa `profin.log` (ver [Datos](#datos-copias-de-seguridad-y-privacidad)). |

---

## Roadmap

- [x] Importación de CaixaBank con detección de duplicados
- [x] Reglas automáticas de categorización
- [x] Módulo de inversiones (FIFO, TIR, fiscalidad, DEGIRO, MyInvestor)
- [x] Acceso web HTTPS para iPhone/iPad
- [ ] Exportar e importar la biblioteca de categorías y reglas
- [ ] Administración de inversiones desde la web
- [ ] Previsiones
- [ ] Exportación de datos
- [ ] Soporte para extractos de otros bancos

---

## Versionado

El proyecto usa el formato **`X.Y.Z`**:

| Parte | Significado |
|---|---|
| **X** | Release mayor |
| **Y** | Nueva fase principal de desarrollo |
| **Z** | Funcionalidad o corrección |

Cada versión se publica en [GitHub Releases](https://github.com/pbermudezcc/PROFIN/releases) con el ejecutable
empaquetado. El historial completo está en [`CHANGELOG.md`](CHANGELOG.md).

---

## Contribuir

1. Haz un *fork* del repositorio y crea una rama: `git checkout -b feature/mi-mejora`.
2. Escribe tests para el cambio y comprueba que `pytest` pasa entero.
3. Usa mensajes de commit con el formato Conventional Commits.
4. Abre un *Pull Request* contra `main` explicando qué cambia y por qué.

**Una buena primera contribución:** un parser para otro banco en `app/ingestion/`, siguiendo el modelo de `excel_parser.py`.

---

## Seguridad

- Las contraseñas se guardan con **bcrypt**; nunca en texto plano.
- La web solo funciona por **HTTPS** y únicamente en la red local.
- Si encuentras una vulnerabilidad, **no abras un issue público**: contacta directamente con el mantenedor a través de GitHub.

---

## Licencia

Pendiente de definir. Mientras no haya un archivo `LICENSE`, todos los derechos están reservados al autor.

---

<div align="center">
Hecho con ❤️ para gestionar mejor las finanzas de casa.
</div>
