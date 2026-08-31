from PySide6.QtCore import QSettings

from app.config import THEMES
from app.gui.categories_view import CategoriesView
from app.gui.rules_view import RulesView
from app.gui.transactions_view import TransactionsView
from app.settings import AppSettings, SettingsManager


def test_theme_apply_updates_view_theme_state():
    for view_cls in (TransactionsView, RulesView, CategoriesView):
        view = view_cls.__new__(view_cls)
        view._theme = "dark"
        view._t = THEMES["dark"]
        view._apply_theme = lambda: None

        view.apply_theme("light")

        assert view._theme == "light"
        assert view._t == THEMES["light"]


def test_settings_manager_persists_appearance_on_start_flag():
    qsettings = QSettings("PROFIN_TEST_THEME", "PROFIN")
    qsettings.clear()
    manager = SettingsManager(qsettings)

    manager.save(AppSettings(theme="dark", save_appearance_on_start=False))
    loaded = manager.load()

    assert loaded.theme == "dark"
    assert loaded.save_appearance_on_start is False
    qsettings.clear()
