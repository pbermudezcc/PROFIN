"""
app/config.py
==============
FAMILYFIN — Application configuration constants.
"""

APP_NAME    = "FAMILYFIN"
APP_VERSION = "0.4.0"
APP_RELEASE_DATE = "2026-09-27"

# ---------------------------------------------------------------------------
# Theme tokens — Light & Dark
# ---------------------------------------------------------------------------

THEMES = {
    "light": {
        # Background layers
        "bg_primary":    "#F6F8FC",   # main window background (soft, airy)
        "bg_secondary":  "#FFFFFF",   # cards, panels, headers
        "bg_sidebar":    "#132C50",   # sidebar (refreshed navy, less saturated)
        "bg_sidebar_card":"#1E4A8C",   # user card
        "bg_sidebar_hover":"#25528F", # navigation hover
        "bg_hover":      "#EAF0FA",   # hover state

        # Text
        "text_primary":  "#0F172A",
        "text_secondary":"#54627A",
        "text_sidebar":  "#DCEBFF",   # sidebar text (inactive)
        "text_sidebar_active": "#FFFFFF",
        "text_sidebar_muted": "#BFDBFE",  # role and version

        # Accent
        "accent":        "#2563EB",   # primary blue
        "accent_hover":  "#1D4ED8",
        "accent_light":  "#DBEAFE",

        # Status colours
        "income":        "#16A34A",   # green
        "income_hover":  "#15803D",
        "expense":       "#DC2626",   # red
        "expense_hover": "#B91C1C",
        "investment":    "#9333EA",   # purple
        "warning":       "#D97706",   # amber
        "neutral":       "#6B7280",

        # Borders
        "border":        "#E2E8F0",
        "border_strong": "#C7D2E0",

        # Cards
        "card_shadow":   "rgba(15,23,42,0.08)",

        # Selection gradient (list/tree row selection, light theme only)
        "selection_grad_start": "#DDEBFF",
        "selection_grad_end":   "#CFE1FF",
        "selection_hover_start": "#CFE1FF",
        "selection_hover_end":   "#BFD8FF",
        "selection_text":        "#1E3A8A",
    },
    "dark": {
        "bg_primary":    "#0F172A",
        "bg_secondary":  "#1E293B",
        "bg_sidebar":    "#0D1623",
        "bg_sidebar_card":"#16263A",
        "bg_sidebar_hover":"#334155",
        "bg_hover":      "#2C3B52",

        "text_primary":  "#F1F5F9",
        "text_secondary":"#9AACC2",
        "text_sidebar":  "#64748B",
        "text_sidebar_active": "#F1F5F9",
        "text_sidebar_muted": "#CBD5E1",

        "accent":        "#3B82F6",
        "accent_hover":  "#2563EB",
        "accent_light":  "#1E3A5F",

        "income":        "#22C55E",
        "income_hover":  "#16A34A",
        "expense":       "#EF4444",
        "expense_hover": "#DC2626",
        "investment":    "#A855F7",
        "warning":       "#F59E0B",
        "neutral":       "#9CA3AF",

        "border":        "#324258",
        "border_strong": "#48597A",

        "card_shadow":   "rgba(0,0,0,0.35)",

        # Selection gradient — dark theme uses flat accent, kept for API parity.
        "selection_grad_start": "#3B82F6",
        "selection_grad_end":   "#3B82F6",
        "selection_hover_start": "#2563EB",
        "selection_hover_end":   "#2563EB",
        "selection_text":        "#FFFFFF",
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