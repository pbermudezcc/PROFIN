"""
app/gui/main_window.py
=======================
FAMILYFIN — Main Application Shell

Layout:
  ┌─────────────────────────────────────────────────────┐
  │  TOP BAR  (logo | breadcrumb | user | theme toggle) │
  ├──────────┬──────────────────────────────────────────┤
  │          │                                          │
  │ SIDEBAR  │           CONTENT AREA                  │
  │ (nav     │     (swapped by nav selection)          │
  │  tree)   │                                          │
  │          │                                          │
  └──────────┴──────────────────────────────────────────┘
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore    import Qt, QSettings, QSize, QTimer, QUrl
from PySide6.QtGui     import QDesktopServices, QFont, QIcon
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QFrame, QStackedWidget, QSizePolicy,
    QScrollArea, QApplication, QMessageBox,
)
import qtawesome as qta

from app.config      import APP_NAME, APP_VERSION, THEMES, NAV_ITEMS, NAV_ITEMS_TOP, NAV_ITEMS_BOTTOM
from app.db.models   import User
from app.manual      import MANUAL_INDEX
from app.settings    import SettingsManager
from sqlalchemy.orm  import Session


class _NavButton(QPushButton):
    """Sidebar navigation button."""

    def __init__(self, label: str, icon_name: str,
                 view_key: str, theme: dict) -> None:
        super().__init__()
        self.view_key  = view_key
        self._label    = label
        self._icon_name = icon_name
        self._active   = False

        self.setFixedHeight(44)
        self.setCheckable(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setFlat(True)
        self._refresh(theme)

    def set_active(self, active: bool, theme: dict) -> None:
        self._active = active
        self._refresh(theme)

    def _refresh(self, theme: dict) -> None:
        colour = (theme["text_sidebar_active"]
                  if self._active else theme["text_sidebar"])
        bg     = (f"background-color: {theme['bg_sidebar_hover']};"
                  if self._active else "background-color: transparent;")
        border = (f"border-left: 3px solid {theme['accent']};"
                  if self._active else "border-left: 3px solid transparent;")

        self.setIcon(qta.icon(self._icon_name, color=colour))
        self.setIconSize(QSize(20, 20))
        self.setText(f"  {self._label}")
        self.setStyleSheet(f"""
            QPushButton {{
                {bg}
                {border}
                color: {colour};
                text-align: left;
                padding: 0 16px;
                font-size: 13px;
                font-weight: {"600" if self._active else "400"};
                border-radius: 0px;
            }}
            QPushButton:hover {{
                background-color: {theme["bg_sidebar_hover"]};
            }}
        """)


class MainWindow(QWidget):
    """
    Main application window — fullscreen shell with sidebar navigation.
    """

    SETTINGS_ORG = "FAMILYFIN"
    SETTINGS_APP = "FAMILYFIN"
    KEY_THEME    = "ui/theme"
    KEY_NAV      = "ui/last_nav"
    AUTO_BACKUP_CHECK_MS = 30 * 60 * 1000

    def __init__(self, user: User, session: Session,
                 theme: str = "light") -> None:
        super().__init__()
        self._user    = user
        self._session = session
        stored_settings = SettingsManager().load()
        default_theme = stored_settings.theme if stored_settings.save_appearance_on_start else "light"
        selected_theme = theme if theme != "light" else default_theme
        self._theme   = selected_theme if selected_theme in THEMES else "light"
        self._t       = THEMES[self._theme]
        self._settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        self._nav_btns: dict[str, _NavButton] = {}
        self._current_view = ""
        self._stale_views: set[str] = set()

        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.showMaximized()

        self._build_ui()
        self._apply_theme()

        # Navigate to last view or default dashboard
        last_nav = self._settings.value(self.KEY_NAV, "dashboard")
        self._navigate(last_nav)

        # Automatic backups: check now, then periodically while the app runs.
        self._auto_backup_timer = QTimer(self)
        self._auto_backup_timer.setInterval(self.AUTO_BACKUP_CHECK_MS)
        self._auto_backup_timer.timeout.connect(self._run_auto_backup)
        self._auto_backup_timer.start()
        QTimer.singleShot(0, self._run_auto_backup)

        # Changes made from the LAN web front-end (queued from its thread).
        from app.gui.web_bridge import web_bridge
        web_bridge().data_changed.connect(self._on_web_data_changed)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar ────────────────────────────────────────────────────
        self._top_bar = self._make_top_bar()
        root.addWidget(self._top_bar)

        # ── Body (sidebar + content) ───────────────────────────────────
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._sidebar   = self._make_sidebar()
        self._stack     = QStackedWidget()
        self._stack.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)

        body_layout.addWidget(self._sidebar)
        body_layout.addWidget(self._stack)
        root.addWidget(body)

        # ── Populate stacked views ─────────────────────────────────────
        self._views: dict[str, QWidget] = {}
        self._register_views()

    def _make_top_bar(self) -> QFrame:
        """
        Minimal modern top bar:
        [☰] [● FAMILYFIN  v0.1.x] ── [breadcrumb] ──────── [Pedro ▾] [🌙] [⏻]
        Height: 48px. Single accent-colored bottom border. No visual clutter.
        """
        bar = QFrame()
        bar.setFixedHeight(48)
        bar.setObjectName("topBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(4, 0, 12, 0)
        layout.setSpacing(0)

        # ── Sidebar toggle ────────────────────────────────────────────
        self._btn_menu = QPushButton()
        self._btn_menu.setFixedSize(48, 48)
        self._btn_menu.setFlat(True)
        self._btn_menu.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_menu.setToolTip("Mostrar / ocultar menú lateral")
        self._btn_menu.clicked.connect(self._toggle_sidebar)
        layout.addWidget(self._btn_menu)

        layout.addSpacing(4)

        # ── Logo dot + brand + version (single compact group) ────────
        lbl_logo = QLabel()
        lbl_logo.setObjectName("topBarLogo")
        lbl_logo.setFixedSize(18, 18)
        self._lbl_topbar_logo = lbl_logo
        layout.addWidget(lbl_logo)

        layout.addSpacing(6)

        lbl_brand = QLabel(f"{APP_NAME}  ")
        lbl_brand.setObjectName("topBarBrand")
        layout.addWidget(lbl_brand)

        lbl_ver = QLabel(f"v{APP_VERSION}")
        lbl_ver.setObjectName("topBarVersion")
        layout.addWidget(lbl_ver)

        layout.addSpacing(20)

        # ── Breadcrumb (current view name) ────────────────────────────
        self._lbl_breadcrumb = QLabel("Posición Global")
        self._lbl_breadcrumb.setObjectName("breadcrumb")
        layout.addWidget(self._lbl_breadcrumb)

        layout.addStretch()

        # ── User chip (avatar dot + name, no extra separator) ─────────
        initials = self._user.username[:2].upper()
        lbl_av = QLabel(initials)
        self._lbl_top_avatar = lbl_av
        lbl_av.setObjectName("topBarAvatar")
        lbl_av.setFixedSize(26, 26)
        lbl_av.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_av)

        layout.addSpacing(5)

        self._lbl_user = QLabel(self._user.username.capitalize())
        self._lbl_user.setObjectName("topBarUsername")
        layout.addWidget(self._lbl_user)

        layout.addSpacing(12)

        # ── User manual (docs/manual, opened in the browser) ──────────
        self._btn_help = QPushButton()
        self._btn_help.setFixedSize(32, 32)
        self._btn_help.setFlat(True)
        self._btn_help.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_help.setToolTip("Manual de usuario")
        self._btn_help.clicked.connect(self._open_manual)
        layout.addWidget(self._btn_help)

        layout.addSpacing(2)

        # ── Theme toggle ──────────────────────────────────────────────
        self._btn_theme = QPushButton()
        self._btn_theme.setFixedSize(32, 32)
        self._btn_theme.setFlat(True)
        self._btn_theme.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_theme.setToolTip("Cambiar tema claro / oscuro")
        self._btn_theme.clicked.connect(self._toggle_theme)
        layout.addWidget(self._btn_theme)

        layout.addSpacing(2)

        # ── Logout ────────────────────────────────────────────────────
        self._btn_logout = QPushButton()
        self._btn_logout.setFixedSize(32, 32)
        self._btn_logout.setFlat(True)
        self._btn_logout.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_logout.setToolTip("Cerrar sesión")
        self._btn_logout.clicked.connect(self._on_logout)
        layout.addWidget(self._btn_logout)

        layout.addSpacing(16)

        # ── Window controls (frameless) ───────────────────────────────
        sep_wc = QFrame()
        sep_wc.setFrameShape(QFrame.Shape.VLine)
        sep_wc.setFixedHeight(20)
        sep_wc.setObjectName("topBarSepV")
        layout.addWidget(sep_wc)

        layout.addSpacing(8)

        btn_min = QPushButton()
        btn_min.setFixedSize(28, 28)
        btn_min.setFlat(True)
        btn_min.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_min.setToolTip("Minimizar")
        btn_min.setObjectName("wctrlMin")
        btn_min.setIcon(qta.icon("mdi.minus", color=self._t["text_secondary"]))
        btn_min.clicked.connect(self.showMinimized)
        layout.addWidget(btn_min)

        self._btn_maximize = QPushButton()
        self._btn_maximize.setFixedSize(28, 28)
        self._btn_maximize.setFlat(True)
        self._btn_maximize.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_maximize.setToolTip("Restaurar / Maximizar")
        self._btn_maximize.setObjectName("wctrlMax")
        self._btn_maximize.setIcon(qta.icon("mdi.crop-square", color=self._t["text_secondary"]))
        self._btn_maximize.clicked.connect(self._toggle_maximize)
        layout.addWidget(self._btn_maximize)

        btn_close = QPushButton()
        btn_close.setFixedSize(28, 28)
        btn_close.setFlat(True)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setToolTip("Cerrar aplicación")
        btn_close.setObjectName("wctrlClose")
        btn_close.setIcon(qta.icon("mdi.close", color=self._t["text_secondary"]))
        btn_close.clicked.connect(self.close)
        layout.addWidget(btn_close)

        layout.addSpacing(4)

        # ── Drag support for frameless window ─────────────────────────
        bar._drag_pos = None

        def _bar_mouse_press(e):
            if e.button() == Qt.MouseButton.LeftButton:
                bar._drag_pos = (
                    e.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
        def _bar_mouse_move(e):
            if bar._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
                if self.isMaximized():
                    self.showNormal()
                    bar._drag_pos = (
                        e.globalPosition().toPoint() - self.frameGeometry().topLeft()
                    )
                self.move(e.globalPosition().toPoint() - bar._drag_pos)
        def _bar_mouse_release(e):
            bar._drag_pos = None
        def _bar_double_click(e):
            self._toggle_maximize()

        bar.mousePressEvent   = _bar_mouse_press
        bar.mouseMoveEvent    = _bar_mouse_move
        bar.mouseReleaseEvent = _bar_mouse_release
        bar.mouseDoubleClickEvent = _bar_double_click

        return bar

    def _make_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(2)

        # User avatar area
        avatar_area = QFrame()
        avatar_area.setObjectName("sidebarUserCard")
        avatar_area.setFixedHeight(72)
        avatar_layout = QHBoxLayout(avatar_area)
        avatar_layout.setContentsMargins(16, 8, 16, 8)

        lbl_avatar = QLabel()
        lbl_avatar.setFixedSize(36, 36)
        lbl_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_avatar.setObjectName("avatarCircle")
        initials = self._user.username[:2].upper()
        lbl_avatar.setText(initials)
        self._lbl_sidebar_avatar = lbl_avatar
        avatar_layout.addWidget(lbl_avatar)

        avatar_layout.addSpacing(8)

        user_info = QVBoxLayout()
        user_info.setSpacing(1)
        lbl_name = QLabel(self._user.username.capitalize())
        self._lbl_sidebar_name = lbl_name
        lbl_name.setObjectName("sidebarUsername")
        lbl_role = QLabel("Personal")
        lbl_role.setObjectName("sidebarRole")
        user_info.addWidget(lbl_name)
        user_info.addWidget(lbl_role)
        avatar_layout.addLayout(user_info)

        avatar_layout.addStretch()

        # Account settings button
        self._btn_account = QPushButton()
        self._btn_account.setFixedSize(28, 28)
        self._btn_account.setFlat(True)
        self._btn_account.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_account.setToolTip("Configuración de cuenta")
        self._btn_account.clicked.connect(self._on_account_settings)
        avatar_layout.addWidget(self._btn_account)

        layout.addWidget(avatar_area)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("sidebarSep")
        layout.addWidget(sep)
        layout.addSpacing(4)

        # ── Top nav section ───────────────────────────────────────────
        for label, icon_name, view_key in NAV_ITEMS_TOP:
            btn = _NavButton(label, icon_name, view_key, self._t)
            btn.clicked.connect(
                lambda checked=False, k=view_key: self._navigate(k)
            )
            layout.addWidget(btn)
            self._nav_btns[view_key] = btn

        # ── Flexible spacer pushes bottom section down ────────────────
        layout.addStretch()

        # ── Bottom section separator ──────────────────────────────────
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setObjectName("sidebarSep")
        layout.addWidget(sep2)
        layout.addSpacing(4)

        # ── Bottom nav section (tools) ────────────────────────────────
        for label, icon_name, view_key in NAV_ITEMS_BOTTOM:
            btn = _NavButton(label, icon_name, view_key, self._t)
            btn.clicked.connect(
                lambda checked=False, k=view_key: self._navigate(k)
            )
            layout.addWidget(btn)
            self._nav_btns[view_key] = btn

        layout.addSpacing(8)

        # ── Version at very bottom ────────────────────────────────────
        lbl_ver = QLabel(f"v{APP_VERSION}")
        lbl_ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_ver.setObjectName("sidebarVersion")
        layout.addWidget(lbl_ver)

        return sidebar

    def _register_views(self) -> None:
        """Register a factory per view; each widget is built on first visit.

        Building every view at login ran all of their DB queries up front.
        Views are now created (with the theme current at that moment) the
        first time the user navigates to them, then kept in the stack.
        """
        from app.gui.dashboard         import DashboardView
        from app.gui.import_view       import ImportView
        from app.gui.transactions_view import TransactionsView
        from app.gui.categories_view   import CategoriesView
        from app.gui.rules_view        import RulesView
        from app.gui.expenses_view     import ExpensesView
        from app.gui.incomes_window    import IncomesWindow
        from app.gui.investments_view  import InvestmentsView
        from app.gui.settings_view      import SettingsView

        self._view_factories = {
            "dashboard":    lambda: DashboardView(
                                self._user, self._session, self._theme
                            ),
            "expenses":     lambda: ExpensesView(
                                self._session, self._user, self._theme
                            ),
            "income":       lambda: IncomesWindow(
                                self._user, self._session, self._theme
                            ),
            "investments":  lambda: InvestmentsView(
                                self._user, self._session, self._theme
                            ),
            "transactions": lambda: TransactionsView(
                                self._user, self._session, self._theme
                            ),
            "import":       lambda: ImportView(
                                self._user, self._session, self._theme
                            ),
            "categories":   lambda: CategoriesView(
                                self._user, self._session, self._theme
                            ),
            "rules":        lambda: RulesView(
                                self._user, self._session, self._theme
                            ),
            "forecast":     lambda: _PlaceholderView(
                                "Previsiones",
                                "mdi.calendar-clock-outline", self._t
                            ),
            "export":       lambda: _PlaceholderView(
                                "Exportar", "mdi.download-outline", self._t
                            ),
            "settings":     lambda: SettingsView(
                                self._user, self._session, self._theme
                            ),
        }


    def _ensure_view(self, view_key: str) -> QWidget:
        """Return the view for view_key, building it on first use."""
        widget = self._views.get(view_key)
        if widget is None:
            widget = self._view_factories[view_key]()
            self._stack.addWidget(widget)
            self._views[view_key] = widget
        return widget

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _navigate(self, view_key: str) -> None:
        if view_key not in self._view_factories:
            view_key = "dashboard"

        # Update sidebar active state
        if self._current_view and self._current_view in self._nav_btns:
            self._nav_btns[self._current_view].set_active(False, self._t)
        if view_key in self._nav_btns:
            self._nav_btns[view_key].set_active(True, self._t)

        # Update breadcrumb
        label = next(
            (lbl for lbl, _, k in NAV_ITEMS if k == view_key),
            view_key.capitalize()
        )
        self._lbl_breadcrumb.setText(label)

        self._stack.setCurrentWidget(self._ensure_view(view_key))
        self._current_view = view_key
        if view_key in self._stale_views:
            self._reload_view(view_key)
        self._settings.setValue(self.KEY_NAV, view_key)

    # ------------------------------------------------------------------
    # Web front-end changes
    # ------------------------------------------------------------------

    _RELOAD_METHODS = ("refresh_data", "refresh", "reload", "_load_data")

    def _on_web_data_changed(self, user_id: int, area: str) -> None:
        """Someone changed this user's data from a phone: drop the cached ORM
        state and reload the visible view; the others reload on next visit.
        «account»: renamed (or new password) from the web → show the new name;
        «account_deleted»: this account was deleted from the web → back to login."""
        if user_id != self._user.id:
            return
        if area == "account_deleted":
            self._on_account_deleted_from_web()
            return
        try:
            self._session.expire_all()
        except Exception:
            logging.getLogger(__name__).exception("Web: no se pudo refrescar la sesión")
            return
        if area == "account":
            self._refresh_username()
            return
        self._stale_views = set(self._views)
        if self._current_view in self._views:
            self._reload_view(self._current_view)

    def _refresh_username(self) -> None:
        from app.db.models import User as UserModel
        fresh = self._session.get(UserModel, self._user.id)
        if fresh is None:
            return
        old_name = self._user.username
        self._user = fresh
        name = fresh.username
        for label in (getattr(self, "_lbl_user", None), getattr(self, "_lbl_sidebar_name", None)):
            if label is not None:
                label.setText(name.capitalize())
        for label in (getattr(self, "_lbl_top_avatar", None), getattr(self, "_lbl_sidebar_avatar", None)):
            if label is not None:
                label.setText(name[:2].upper())
        if old_name != name and self._settings.value("auth/last_username", "", type=str) == old_name:
            self._settings.setValue("auth/last_username", name)

    def _on_account_deleted_from_web(self) -> None:
        self._settings.remove("auth/last_username")
        QMessageBox.information(
            self, "Cuenta borrada",
            "Esta cuenta se ha borrado desde la web. Se guardó una copia de seguridad "
            "en la carpeta de copias de este PC.",
        )
        self._on_logout()

    def _reload_view(self, view_key: str) -> None:
        self._stale_views.discard(view_key)
        view = self._views.get(view_key)
        for name in self._RELOAD_METHODS:
            method = getattr(view, name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    logging.getLogger(__name__).exception("Web: recarga de la vista %s", view_key)
                return

    def closeEvent(self, event) -> None:
        # Views with background threads (Inversiones price refresh, Yahoo
        # lookups) must let them finish: destroying a running QThread aborts.
        from app.gui._background import wait_for_background_threads
        for view in self._views.values():
            if hasattr(view, "shutdown"):
                view.shutdown()
        wait_for_background_threads()
        super().closeEvent(event)

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self._btn_maximize.setIcon(
                qta.icon("mdi.crop-square", color=self._t["text_secondary"])
            )
            self._btn_maximize.setToolTip("Maximizar")
        else:
            self.showMaximized()
            self._btn_maximize.setIcon(
                qta.icon("mdi.window-restore", color=self._t["text_secondary"])
            )
            self._btn_maximize.setToolTip("Restaurar")

    def _toggle_sidebar(self) -> None:
        visible = self._sidebar.isVisible()
        self._sidebar.setVisible(not visible)

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _open_manual(self) -> None:
        if not MANUAL_INDEX.is_file():
            logging.getLogger(__name__).warning("Manual no encontrado: %s", MANUAL_INDEX)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(MANUAL_INDEX)))

    def _toggle_theme(self) -> None:
        self._theme = "dark" if self._theme == "light" else "light"
        self._t     = THEMES[self._theme]
        self._settings.setValue(self.KEY_THEME, self._theme)
        self._apply_theme()

        # Propagate theme to ALL registered views
        for key, view in self._views.items():
            # DashboardView uses refresh_theme(str)
            if hasattr(view, "refresh_theme"):
                view.refresh_theme(self._theme)
            # TransactionsView, ExpensesView, CategoriesView,
            # RulesView, ImportView use apply_theme(str)
            elif hasattr(view, "apply_theme"):
                view.apply_theme(self._theme)

    def _apply_theme(self) -> None:
        t = self._t
        is_dark = self._theme == "dark"

        icon_color = t["text_primary"]
        self._btn_menu.setIcon(
            qta.icon("mdi.menu", color=t["text_sidebar_active"])
        )
        self._btn_menu.setIconSize(QSize(22, 22))

        theme_icon = ("mdi.weather-night"
                      if self._theme == "light" else "mdi.weather-sunny")
        self._btn_theme.setIcon(qta.icon(theme_icon, color=icon_color))
        self._btn_theme.setIconSize(QSize(20, 20))

        self._btn_help.setIcon(qta.icon("mdi.help-circle-outline", color=icon_color))
        self._btn_help.setIconSize(QSize(20, 20))

        self._btn_logout.setIcon(
            qta.icon("mdi.logout", color=icon_color)
        )
        self._btn_logout.setIconSize(QSize(20, 20))

        self._btn_account.setIcon(
            qta.icon("mdi.dots-vertical", color=self._t["text_sidebar"])
        )
        self._btn_account.setIconSize(QSize(16, 16))

        # Refresh nav buttons
        for key, btn in self._nav_btns.items():
            btn._refresh(t)

        # Refresh logo icon (accent color changes with theme)
        self._lbl_topbar_logo.setPixmap(
            qta.icon("mdi.chart-areaspline",
                     color=t["accent"]).pixmap(26, 26)
        )

        self.setStyleSheet(f"""
            QWidget {{
                background-color: {t["bg_primary"]};
                color: {t["text_primary"]};
                font-family: "Segoe UI";
                font-size: 13px;
            }}
                        QPushButton#wctrlMin:hover,
            QPushButton#wctrlMax:hover {{
                background-color: {t["bg_hover"]};
                border-radius: 6px;
            }}
            QPushButton#wctrlClose:hover {{
                background-color: #EF4444;
                border-radius: 6px;
            }}
            QPushButton#wctrlClose:hover {{
                /* icon color via setIcon no cambia con CSS — ok */
            }}
            QFrame#topBarSepV {{
                color: {t["border"]};
                background-color: {t["border"]};
                max-width: 1px;
            }}
            QFrame#topBar {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {t["bg_secondary"]},
                    stop:1 {t["bg_primary"]});
                border-bottom: 1px solid {t["border"]};
                border-top: 1px solid rgba(255,255,255,0.25);
            }}
            QLabel#topBarBrand {{
                color: {t["text_primary"]};
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#topBarVersion {{
                color: {t["text_secondary"]};
                font-size: 11px;
                font-weight: 400;
            }}
            QLabel#breadcrumb {{
                color: {t["text_secondary"]};
                font-size: 12px;
            }}
            QLabel#topBarAvatar {{
                background-color: {t["accent"]};
                color: #FFFFFF;
                border-radius: 13px;
                font-size: 10px;
                font-weight: 700;
            }}
            QLabel#topBarUsername {{
                color: {t["text_primary"]};
                font-size: 12px;
                font-weight: 600;
            }}
            QFrame#sidebar {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {t["bg_sidebar"]},
                    stop:1 {t["bg_sidebar"]});
                border-right: 1px solid {t["border"]};
            }}
            QFrame#sidebarUserCard {{
                background-color: {t["bg_sidebar_card"]};
                border: 1px solid {t["bg_sidebar_hover"]};
                border-radius: 12px;
            }}
            QLabel#avatarCircle {{
                background-color: {t["accent"]};
                color: #FFFFFF;
                border-radius: 18px;
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#sidebarUsername {{
                color: {t["text_sidebar_active"]};
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#sidebarRole {{
                color: {t["text_sidebar_muted"]};
                font-size: 11px;
            }}
            QFrame#sidebarSep {{
                color: {t["border"]};
                background-color: {t["border"]};
                max-height: 1px;
            }}
            QLabel#sidebarVersion {{
                color: {t["text_sidebar_muted"]};
                font-size: 11px;
            }}
        """)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _run_auto_backup(self) -> None:
        """Create an automatic backup if one is due. Never interrupts the user."""
        try:
            SettingsManager().run_auto_backup_if_due(user_name=self._user.username)
        except Exception:
            logging.getLogger(__name__).exception(
                "No se pudo crear la copia de seguridad automática."
            )

    def _on_logout(self) -> None:
        self._session.close()
        from app.gui.login_window import LoginWindow
        self._login = LoginWindow()
        self._login.show()
        self.close()

    def _on_account_settings(self) -> None:
        """Open account settings dialog with delete account option."""
        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        t = self._t

        dlg = QDialog(self)
        dlg.setWindowTitle("Configuración de cuenta")
        dlg.setFixedWidth(380)
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)

        # Header — avatar + username
        header = QHBoxLayout()
        lbl_av = QLabel(self._user.username[:2].upper())
        lbl_av.setFixedSize(48, 48)
        lbl_av.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_av.setStyleSheet(f"""
            background-color: {t["accent"]};
            color: #FFFFFF;
            border-radius: 24px;
            font-size: 16px;
            font-weight: 700;
        """)
        header.addWidget(lbl_av)
        header.addSpacing(12)

        info = QVBoxLayout()
        lbl_uname = QLabel(self._user.username.capitalize())
        lbl_uname.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {t['text_primary']};"
        )
        lbl_joined = QLabel(
            f"Cuenta creada: "
            f"{self._user.created_at.strftime('%d %b %Y')}"
            if self._user.created_at else "Cuenta activa"
        )
        lbl_joined.setStyleSheet(
            f"font-size: 11px; color: {t['text_secondary']};"
        )
        info.addWidget(lbl_uname)
        info.addWidget(lbl_joined)
        header.addLayout(info)
        header.addStretch()
        layout.addLayout(header)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {t['border']};")
        layout.addWidget(sep)

        # Delete account button
        btn_delete = QPushButton(
            "  Eliminar cuenta permanentemente"
        )
        btn_delete.setIcon(
            qta.icon("mdi.account-remove-outline", color="#FFFFFF")
        )
        btn_delete.setFixedHeight(42)
        btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_delete.setStyleSheet(f"""
            QPushButton {{
                background-color: {t["expense"]};
                color: #FFFFFF;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
                padding: 0 16px;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: #B91C1C;
            }}
        """)
        layout.addWidget(btn_delete)

        lbl_warn = QLabel(
            "Se eliminarán todos tus datos: transacciones, cuentas,\n"
            "categorías y reglas. Esta acción no se puede deshacer."
        )
        lbl_warn.setStyleSheet(
            f"font-size: 11px; color: {t['text_secondary']};"
        )
        lbl_warn.setWordWrap(True)
        layout.addWidget(lbl_warn)

        # Close button
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {t['border']};")
        layout.addWidget(sep2)

        btn_close = QPushButton("Cerrar")
        btn_close.setFixedHeight(38)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {t["text_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {t["bg_hover"]};
            }}
        """)
        btn_close.clicked.connect(dlg.reject)
        layout.addWidget(btn_close)

        dlg.setStyleSheet(f"""
            QDialog {{
                background-color: {t["bg_secondary"]};
            }}
        """)

        def _on_delete_clicked() -> None:
            dlg.accept()
            self._confirm_delete_account()

        btn_delete.clicked.connect(_on_delete_clicked)
        dlg.exec()

    def _confirm_delete_account(self) -> None:
        """
        Two-step confirmation dialog before permanently deleting the account.
        Step 1: Warning + confirm button.
        Step 2: Type username to confirm.
        """
        from PySide6.QtWidgets import QDialog, QLineEdit

        t = self._t
        username = self._user.username

        dlg = QDialog(self)
        dlg.setWindowTitle("Confirmar eliminación de cuenta")
        dlg.setFixedWidth(420)
        dlg.setModal(True)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(14)

        # Warning icon + title
        lbl_icon = QLabel()
        lbl_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_icon.setPixmap(
            qta.icon("mdi.alert-circle-outline",
                     color=t["expense"]).pixmap(48, 48)
        )
        layout.addWidget(lbl_icon)

        lbl_title = QLabel("¿Eliminar cuenta definitivamente?")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_title.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {t['text_primary']};"
        )
        layout.addWidget(lbl_title)

        lbl_msg = QLabel(
            f"Vas a eliminar la cuenta <b>{username}</b> y "
            f"<b>todos sus datos</b>.\n\n"
            "Antes se guardará una copia de seguridad en la carpeta de copias de este PC.\n\n"
            f"Para confirmar, escribe tu nombre de usuario y tu contraseña:"
        )
        lbl_msg.setWordWrap(True)
        lbl_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_msg.setStyleSheet(
            f"font-size: 12px; color: {t['text_secondary']};"
        )
        layout.addWidget(lbl_msg)

        inp_confirm = QLineEdit()
        inp_confirm.setPlaceholderText(f"Escribe «{username}» para confirmar")
        inp_confirm.setFixedHeight(42)
        inp_confirm.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inp_confirm.setStyleSheet(f"""
            QLineEdit {{
                background-color: {t["bg_primary"]};
                border: 2px solid {t["expense"]};
                border-radius: 8px;
                padding: 8px 12px;
                color: {t["text_primary"]};
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border: 2px solid #B91C1C;
            }}
        """)
        layout.addWidget(inp_confirm)

        inp_password = QLineEdit()
        inp_password.setPlaceholderText("Tu contraseña")
        inp_password.setEchoMode(QLineEdit.EchoMode.Password)
        inp_password.setFixedHeight(42)
        inp_password.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inp_password.setStyleSheet(inp_confirm.styleSheet())
        layout.addWidget(inp_password)

        # Error label
        lbl_err = QLabel("")
        lbl_err.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_err.setStyleSheet(
            f"color: {t['expense']}; font-size: 11px;"
        )
        lbl_err.setVisible(False)
        layout.addWidget(lbl_err)

        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        btn_cancel = QPushButton("Cancelar")
        btn_cancel.setFixedHeight(40)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {t["text_secondary"]};
                border: 1px solid {t["border"]};
                border-radius: 8px;
                font-size: 13px;
            }}
            QPushButton:hover {{ background-color: {t["bg_hover"]}; }}
        """)
        btn_cancel.clicked.connect(dlg.reject)

        btn_confirm = QPushButton("  Eliminar cuenta")
        btn_confirm.setIcon(
            qta.icon("mdi.delete-forever-outline", color="#FFFFFF")
        )
        btn_confirm.setFixedHeight(40)
        btn_confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_confirm.setEnabled(False)   # disabled until username matches
        btn_confirm.setStyleSheet(f"""
            QPushButton {{
                background-color: {t["expense"]};
                color: #FFFFFF;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
                padding: 0 16px;
            }}
            QPushButton:hover {{ background-color: #B91C1C; }}
            QPushButton:disabled {{
                background-color: {t["border"]};
                color: {t["text_secondary"]};
            }}
        """)

        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_confirm)
        layout.addLayout(btn_row)

        dlg.setStyleSheet(f"QDialog {{ background-color: {t['bg_secondary']}; }}")

        # Enable confirm button only when username matches exactly
        def _on_text_changed(_text: str = "") -> None:
            btn_confirm.setEnabled(
                inp_confirm.text().strip() == username and bool(inp_password.text())
            )
            lbl_err.setVisible(False)

        inp_confirm.textChanged.connect(_on_text_changed)
        inp_password.textChanged.connect(_on_text_changed)
        inp_confirm.returnPressed.connect(inp_password.setFocus)
        inp_password.returnPressed.connect(
            lambda: btn_confirm.click() if btn_confirm.isEnabled() else None
        )

        def _do_delete() -> None:
            from app.auth.account_service import AccountError, delete_user

            # Shared with the web «Borrar mi cuenta»: checks the name and the
            # password, refuses while family rules depend on this user's
            # categories, takes a backup first (nothing is deleted if it
            # fails) and leaves an audit row that survives the cascade.
            try:
                self._session.expire_all()
                backup = delete_user(
                    self._session, self._user.id, inp_password.text(), inp_confirm.text(),
                    backup=lambda: SettingsManager().backup_before_delete(username),
                    source="el PC",
                )
            except AccountError as exc:
                lbl_err.setText(exc.message)
                lbl_err.setVisible(True)
                if exc.field == "password":
                    inp_password.clear()
                    inp_password.setFocus()
                return
            except Exception:
                self._session.rollback()
                logging.getLogger(__name__).exception("No se pudo borrar la cuenta")
                lbl_err.setText("No se pudo borrar la cuenta. No se ha cambiado nada.")
                lbl_err.setVisible(True)
                return

            # Its web sessions and invitations die with it.
            from app.web.server import get_web_server
            get_web_server().sessions.revoke_user(self._user.id)
            get_web_server().invites.revoke_by(self._user.id)
            logging.getLogger(__name__).info("Copia previa al borrado: %s", backup)

            dlg.accept()

            # Clear last user from settings and return to login
            from PySide6.QtCore import QSettings
            settings = QSettings("FAMILYFIN", "FAMILYFIN")
            settings.remove("auth/last_username")

            self._session.close()

            from app.gui.login_window import LoginWindow
            self._login = LoginWindow()
            self._login.show()
            self.close()

        btn_confirm.clicked.connect(_do_delete)
        dlg.exec()


class _PlaceholderView(QWidget):
    """Temporary placeholder for views not yet implemented."""

    def __init__(self, title: str, icon_name: str, theme: dict) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(16)

        lbl_icon = QLabel()
        lbl_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_icon.setPixmap(
            qta.icon(icon_name, color=theme["neutral"]).pixmap(64, 64)
        )
        layout.addWidget(lbl_icon)

        lbl = QLabel(f"<b>{title}</b>")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            f"font-size: 20px; color: {theme['text_secondary']};"
        )
        layout.addWidget(lbl)

        lbl2 = QLabel("En construcción — Próximas fases")
        lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl2.setStyleSheet(
            f"font-size: 13px; color: {theme['neutral']};"
        )
        layout.addWidget(lbl2)