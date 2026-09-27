# FAMILYFIN Agent — Lessons Learned

This file is the persistent bug/lesson memory for the `familyfin-engineer` agent
(`.claude/agents/familyfin-engineer.md`). Before investigating any bug or
regression, the agent reads this file for prior root causes in the same area.
After resolving a bug, it appends a new entry here — never edits history,
only appends.

Entry format:

```
## YYYY-MM-DD — short title
- Area: e.g. rules_engine / excel_parser / dashboard / auth
- Symptom: what was observed
- Root cause: the actual defect, not the symptom
- Fix: file(s) and the shape of the fix (not a full diff)
- Prevention: what to check next time to avoid recurrence, or a test added
```

## 2026-09-16 — Passive tab-visit silently reverted manual categorization
- Area: rules_engine / transactions_view
- Symptom: a transaction a family member manually recategorized could silently revert to the auto-rule's category just from switching away from and back to the "Transacciones" tab.
- Root cause: `TransactionsView._refresh_with_rules()` (called from `showEvent`, i.e. on every passive tab visit, not just an explicit user action) called `apply_rules_and_commit(..., overwrite=True)`. `overwrite=True` re-categorizes transactions that already have a `category_id`, which is only appropriate for an explicit "re-run rules including manual ones" action — not for a side effect of navigating to a tab. The function's own docstring already said "uncategorized transactions", contradicting the `overwrite=True` argument actually passed.
- Fix: changed the call in `app/gui/transactions_view.py` to `overwrite=False`, matching the documented intent — the automatic pass now only fills transactions with no category, never touches ones a user already set (manually or via a prior rule run).
- Prevention: any code path reachable from a passive Qt event (`showEvent`, `resizeEvent`, etc.) must never call a data-mutating function with a "force overwrite" flag. Added `tests/test_rules_engine.py::test_r11_no_overwrite_preserves_manual_category` to lock in the non-overwrite guarantee.

## 2026-09-26 — Running pytest wiped the family's real app preferences
- Area: settings / tests
- Symptom: after a test run, the real `HKCU\Software\GEVernova\FAMILYFIN` registry key was empty (theme, last user, backup folder etc. reset). Data was unaffected because it lives in the default folder.
- Root cause: `tests/test_runtime_paths.py` builds `QSettings("GEVernova", "FAMILYFIN")` and calls `.clear()`; `QSettings(org, app)` is the native (registry) store, so tests operated on the real machine's settings.
- Fix: root `conftest.py` replaces `PySide6.QtCore.QSettings` before anything imports it with a subclass that sends every `QSettings(org, app)` to INI files in a temp folder.
- Prevention: tests must never touch real user state (registry, `%LOCALAPPDATA%\FAMILYFIN`). Verified by checking the registry value count before/after a full `pytest` run.

## 2026-09-26 — "Re-apply all rules" overwrote manually set categories
- Area: rules_engine / rules_view
- Symptom: after pressing the explicit re-apply button in Reglas, transactions a family member had recategorized by hand reverted to the rule's category.
- Root cause: `apply_rules_and_commit(overwrite=True)` could not distinguish manual from rule-set categories — the schema had no record of who set `category_id`.
- Fix: new `transactions.category_source` ('manual' | 'rule' | NULL); `assign_transaction_category` sets 'manual', the rules engine sets 'rule' and skips 'manual' rows in both modes. Schema migration v1 adds the column to existing DBs and backfills (matches current rules → 'rule', else 'manual').
- Prevention: tests R-13/R-14/R-15 in `tests/test_rules_engine.py`; migration tests in `tests/test_backup_and_migrations.py`.

## 2026-09-26 — Restore overwrote the live database; sqlite handles left open
- Area: settings / db
- Symptom: (1) "Restaurar respaldo" wrote `profin.db` while the app held it open in WAL mode and left stale `-wal/-shm` files — a corruption risk. (2) On Windows, a file checked by `_sqlite_integrity_errors` could not be deleted afterwards (WinError 32).
- Root cause: (1) restore wrote directly over the open DB. (2) `with sqlite3.connect(...)` only commits/rolls back; it does NOT close the connection.
- Fix: restore now stages `profin.restore-pending.db`, which `init_db()` → `apply_pending_restore()` validates and swaps in at the next start (keeping `profin.before-restore.*.db`). Backups use `snapshot_database()` (SQLite backup API). Integrity check uses `contextlib.closing`.
- Prevention: always wrap `sqlite3.connect` in `closing(...)`; never write over a DB file the engine may have open.

## 2026-09-27 — Global rules assigned another user's categories
- Area: rules_engine / db
- Symptom: 49 transactions of one family member were categorized with category ids belonging to the other member's tree (views showed them as uncategorized or under the wrong name).
- Root cause: categories are per user, but a global rule (`user_id IS NULL`) stores its creator's `category_id`; `apply_rules` assigned that id verbatim to every user's transactions.
- Fix: `rules_engine._resolve_rule_categories` maps a global rule to the same-named category (same level and parent type name) in the target user's tree, or skips it. Schema migration v3 repairs existing rows (remap, else clear to NULL). Dry run on a copy of the real DB: 44 remapped, 5 cleared.
- Prevention: tests R-16/R-17/R-18 and `test_migration_3_repairs_categories_owned_by_another_user`. Any id that crosses users must be resolved in the reader's own tree.

## 2026-09-27 — Changing the data folder could lose or overwrite family data
- Area: settings / db / gui
- Symptom: (1) choosing a folder that already held a database overwrote it with the current one; (2) changing the folder from "Guardar" in Configuración never copied the database (the new folder opened empty on next start); (3) after a switch, the running session kept writing to the old file; (4) picking a folder with an older database at login skipped schema migrations.
- Root cause: (1) `apply_data_folder` copied `profin.db/-wal/-shm` over any non-empty target; (2) `SettingsManager.save()` persisted the new `settings/data_folder` before `apply_data_folder` read the "current" folder, so source == target; (3) nothing rebound the shared Session; (4) the login picker called `refresh_runtime_paths()` instead of `init_db()`.
- Fix: `apply_data_folder(folder, current_dir=None)` never overwrites a DB that has users, sets a user-less one aside as `profin.replaced.<ts>.db`, and copies via `snapshot_database`. `SettingsView._switch_data_folder` passes the pre-save folder, runs `init_db()` and logs the user out. Login picker uses `init_db()`.
- Prevention: tests in `tests/test_runtime_paths.py` (users preserved, user-less set aside, explicit current_dir). Read "current state" before any call that persists the new state.

## 2026-09-27 — Dashboard crashed when the latest transaction had no balance
- Area: dashboard
- Symptom: `TypeError: bad operand type for abs(): 'NoneType'` building the dashboard (found by the new offscreen GUI smoke test).
- Root cause: `running_balance` is nullable, but the KPI took the balance of the latest transaction unconditionally.
- Fix: query the latest non-NULL `running_balance` (default 0,00).
- Prevention: `tests/test_gui_smoke.py` builds the shell and every data view against a throwaway DB and fails on any logged error.

## 2026-09-27 — Daily line chart in Gastos/Ingresos out of order across months
- Area: expenses_view
- Symptom: in multi-month periods the daily chart ordered "01/09" before "30/08".
- Root cause: points were keyed and sorted by the formatted "dd/mm" string.
- Fix: aggregation moved to `app/analytics/period_summary.py` (SQL-side), grouped and ordered by the real date; the view only formats labels.
- Prevention: `tests/test_period_summary.py::test_summarize_period_groupings`. Never sort by a display-formatted date.
