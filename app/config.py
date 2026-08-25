"""
app/config.py
==============
PROFIN — Application configuration constants.
"""

APP_NAME    = "PROFIN"
APP_VERSION = "0.2.0"

# ---------------------------------------------------------------------------
# Theme tokens — Light & Dark
# ---------------------------------------------------------------------------

THEMES = {
    "light": {
        # Background layers
        "bg_primary":    "#F8F9FA",   # main window background
        "bg_secondary":  "#FFFFFF",   # cards, panels
        "bg_sidebar":    "#1E2A3A",   # sidebar (always dark)
        "bg_hover":      "#E9ECEF",   # hover state

        # Text
        "text_primary":  "#212529",
        "text_secondary":"#6C757D",
        "text_sidebar":  "#ADB5BD",   # sidebar text (inactive)
        "text_sidebar_active": "#FFFFFF",

        # Accent
        "accent":        "#2563EB",   # primary blue
        "accent_hover":  "#1D4ED8",
        "accent_light":  "#DBEAFE",

        # Status colours
        "income":        "#16A34A",   # green
        "expense":       "#DC2626",   # red
        "investment":    "#9333EA",   # purple
        "warning":       "#D97706",   # amber
        "neutral":       "#6B7280",

        # Borders
        "border":        "#DEE2E6",
        "border_strong": "#ADB5BD",

        # Cards
        "card_shadow":   "rgba(0,0,0,0.06)",
    },
    "dark": {
        "bg_primary":    "#0F172A",
        "bg_secondary":  "#1E293B",
        "bg_sidebar":    "#0D1623",
        "bg_hover":      "#334155",

        "text_primary":  "#F1F5F9",
        "text_secondary":"#94A3B8",
        "text_sidebar":  "#64748B",
        "text_sidebar_active": "#F1F5F9",

        "accent":        "#3B82F6",
        "accent_hover":  "#2563EB",
        "accent_light":  "#1E3A5F",

        "income":        "#22C55E",
        "expense":       "#EF4444",
        "investment":    "#A855F7",
        "warning":       "#F59E0B",
        "neutral":       "#9CA3AF",

        "border":        "#334155",
        "border_strong": "#475569",

        "card_shadow":   "rgba(0,0,0,0.3)",
    },
}

# ---------------------------------------------------------------------------
# Sidebar navigation items
# ---------------------------------------------------------------------------

# NAV_ITEMS: (label, icon_name, view_key)
# Use None as sentinel to render a visual separator in the sidebar.
NAV_ITEMS_TOP = [
    ("Posición Global",  "mdi.view-dashboard-outline",  "dashboard"),
    ("Gastos",           "mdi.trending-down",            "expenses"),
    ("Ingresos",         "mdi.trending-up",              "income"),
    ("Inversiones",      "mdi.chart-line",               "investments"),
    ("Previsiones",      "mdi.calendar-clock-outline",   "forecast"),
    ("Transacciones",    "mdi.format-list-bulleted",     "transactions"),
]

NAV_ITEMS_BOTTOM = [
    ("Categorías",       "mdi.tag-multiple-outline",    "categories"),
    ("Reglas",           "mdi.auto-fix",                "rules"),
    ("Importar",         "mdi.upload-outline",          "import"),
    ("Exportar",         "mdi.download-outline",        "export"),
    ("Configuración",    "mdi.cog-outline",             "settings"),
]

# Flat list kept for breadcrumb lookup and other consumers
NAV_ITEMS = NAV_ITEMS_TOP + NAV_ITEMS_BOTTOM