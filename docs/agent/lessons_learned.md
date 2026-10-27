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

## 2026-09-27 — Dashboard donut slices overlapped
- Area: dashboard (reused by Inversiones → Distribución)
- Symptom: the donut showed overlapping slices and gaps (visible in the Inversiones allocation screenshot).
- Root cause: `_paint_donut` drew each slice with a positive span (counter-clockwise in Qt) while moving `start_angle` clockwise, so every slice was painted back over the previous one.
- Fix: `painter.drawPie(arc_rect, start_angle, -span)` in `app/gui/dashboard.py`.
- Prevention: render charts to an image (`widget.grab()`, `WA_DontShowOnScreen` on the real Windows platform) and look at them; unit tests only proved the data was right.

## 2026-10-04 — Web login rejected as cross-origin (Origin: null)
- Area: web / security
- Symptom: every form POST on the LAN web front-end (login first) answered 403 in a real browser, while TestClient tests passed.
- Root cause: `Referrer-Policy: no-referrer` makes browsers send `Origin: null` on same-site form posts; the same-origin check treated "null" as foreign. Tests set the Origin header by hand, so they never saw it.
- Fix: `Referrer-Policy: same-origin` (still nothing sent to other sites) and the check falls back to the Referer when Origin is "null" (app/web/security.py).
- Prevention: `tests/test_web.py::test_null_origin_falls_back_to_referer`; check security headers in a real browser, not only with TestClient.

## 2026-10-04 — Web page froze the browser tab (Chart.js resize loop)
- Area: web / charts
- Symptom: the Resumen page made the Chrome tab unresponsive.
- Root cause: chart canvases inside CSS grid tracks sized `1fr` (= minmax(auto, 1fr)): the track's minimum follows the canvas width, each Chart.js resize widens the track, which triggers another resize.
- Fix: `minmax(0, 1fr)` tracks, `min-width: 0` on grid items, `.chart-box` with width 100% / overflow hidden (app/web/static/css/app.css).
- Prevention: any responsive canvas (Chart.js) must sit in a container whose width cannot depend on its content.

## 2026-10-04 — Web: picking a category on the iPad did not save it
- Area: web / transactions
- Symptom: on the iPad, choosing another category for a transaction left it unchanged; the server received no request at all (no audit_log entry, nothing in the log).
- Root cause: UX, not the write path. The sheet listed ~50 categories and saving needed a "Guardar" button placed at the end of that list (sticky with a negative offset, i.e. off-screen); tapping a category only selected it. Also, requests rejected by the hardening middleware were not logged, which made it look like a silent write failure.
- Fix: the picker saves on tap (`hx-trigger="change"`), has a search box and shows the transaction's own type group first with the count of categories; middleware 403s are logged with host/origin/referer (app/web/templates/partials/tx_sheet.html, app/web/security.py).
- Prevention: `tests/test_web.py::test_category_sheet_saves_on_tap_and_lists_every_category`; verify touch flows end to end in a browser (tap → request → DB), not only the endpoint.

## 2026-10-04 — Disabled button looked like "nothing happens"
- Area: gui / web_access_view
- Symptom: the user pressed "Configurar dispositivo" and nothing happened; the logs showed the web server had never been started.
- Root cause: the button was disabled while the server was off, with no hint why; disabled Qt buttons give no feedback on click.
- Fix: the button is always enabled; when the server is off it offers to start it and then opens the onboarding; a PC without a LAN address gets an explicit message (app/gui/web_access_view.py, app/web/server.py).
- Prevention: `tests/test_web_tls.py::test_configure_button_is_usable_while_server_is_off`. Prefer an enabled control that explains the prerequisite over a silently disabled one.

## 2026-10-04 — Web: htmx attributes inherit (live preview replaced the whole sheet)
- Area: web / htmx
- Symptom: opening "Nueva regla" showed an empty sheet.
- Root cause: the live-preview `<div hx-get>` sat inside a form with `hx-target="#sheet-body"`; htmx inherits `hx-target`, so the (empty) preview response replaced the whole sheet on load.
- Fix: explicit `hx-target="this"` on the preview element (partials/rule_form.html).
- Prevention: every nested htmx element inside a form/container that sets hx-target must declare its own target; test the form open → type → preview flow in a browser.

## 2026-10-04 — Web: a "Saved" toast vanished after the page refresh
- Area: web / js
- Symptom: after saving a rule the toast never appeared.
- Root cause: the `refreshPage` event swapped the whole `<body>`, including the toast element that had just been shown.
- Fix: refresh only `#main` (`select: "#main"`, outerHTML); toast, sheet and confirm dialog live outside it (static/js/app.js).
- Prevention: keep global UI (toast, dialogs) outside the region that partial refreshes replace.

## 2026-10-04 — Upload temp folders left behind by tests / a killed process
- Area: web / imports
- Symptom: `familyfin-import-*` folders with statements stayed in %TEMP%.
- Root cause: a preview that was never confirmed nor cancelled is only cleaned on expiry or WebServer.stop(); tests (and a crash) never call stop(). FastAPI here has no `add_event_handler`, so a shutdown hook was not an option.
- Fix: `sweep_orphans()` removes folders older than the preview TTL when the app is created; the test fixture clears the store on teardown (app/web/uploads.py, app/web/app.py, tests/test_web.py).
- Prevention: `tests/test_web.py::test_orphaned_upload_folders_are_swept`; check %TEMP% after test runs that touch uploads.

## 2026-10-08 — Windows Smart App Control blocks pandas DLLs (web server "Detenido - Error")
- Area: environment / web / imports
- Symptom: on the laptop (public Wi-Fi, holidays) the web server stayed in "Detenido - Error"; log: `ImportError: DLL load failed while importing parsers: An Application Control policy has blocked this file.`
- Root cause: Smart App Control (enforce mode) refused `pandas/_libs/parsers.cp311-win_amd64.pyd` of pandas 3.0.5 (pandas 3.0.6 was worse: `pandas_parser` blocked). The web imports router imported the ingestion pipeline (and pandas) at startup, so one blocked DLL took the whole web server down.
- Fix: lazy import of the ingestion pipeline in `app/web/routers/imports.py` (Importar shows "no disponible" instead) — `7e32a36`; global pandas moved to 3.0.3, which loads, and pinned in requirements.txt — `bfaf7a0`.
- Prevention: when a native import fails, read `Microsoft-Windows-CodeIntegrity/Operational` first (events 3033/3077/3118). Test candidate versions in an isolated `pip install --target <scratch>` + PYTHONPATH and run the full suite there before changing the global install. Never "fix" it by upgrading blindly — new builds have no reputation yet.

## 2026-10-27 — Dev web preview read the family database from a background thread
- Area: web / dev tooling
- Symptom: in `tools/web_preview.py`, «Actualizar cotizaciones» listed the family's real symbols (SU.PA, 0P… funds) as "sin datos"; the demo's own securities were not refreshed.
- Root cause: the preview overrides `get_db` (request sessions) with its demo DB, but the new background price refresh opens sessions through `app.state.session_factory`, which defaults to `database.SessionLocal` — the real `%LOCALAPPDATA%\FAMILYFIN\data\profin.db`. Verified nothing was written (the fake provider had no data for real symbols).
- Fix: `create_app(session_factory=…)`; the preview and `tests/test_web.py` pass their own factory.
- Prevention: anything in the web layer that opens a session outside a request must go through `app.state.session_factory`, and every harness that overrides `get_db` must set it too. Same for `market_provider` (tests use an offline one by default: never the network).
