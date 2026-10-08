# FAMILYFIN Agent — Change Log

This file tracks every change session performed by the `familyfin-engineer`
agent (`.claude/agents/familyfin-engineer.md`). It is separate from
`CHANGELOG.md` (which is auto-generated per release by
`tools/ExportVersionHandler.py`) — this log tracks work at the session/task
level, including proposals that were evaluated but not yet implemented.

Entry format:

```
## YYYY-MM-DD — short title
- Type: feature / bugfix / optimization-proposal / optimization-applied / refactor
- Files touched: list, or "none (proposal only)"
- Summary: what changed and why, in 1-3 sentences
- Validation: tests/commands run and result
- Follow-up: anything deferred or still at risk
```

## 2026-09-15 — Initial engine/UI optimization survey
- Type: optimization-proposal
- Files touched: none (proposal only, no code changed)
- Summary: Full read-only survey of app/db, app/categorization, app/ingestion, and app/gui for performance/UI/architecture issues. Highest-impact finding: RulesView and TransactionsView re-run the full categorization rules engine over the entire transaction history synchronously on the GUI thread on every tab visit/rule edit, and TransactionsView does so with overwrite=True (silently re-categorizes manually-corrected transactions). No SQL indexes exist on user_id/transaction_date/category_id despite every view filtering/sorting on them. All app views are eagerly constructed (and query the DB) at startup instead of lazily on first navigation.
- Validation: none — analysis only, no tests run, no code changed.
- Follow-up: awaiting user approval on which items to implement; see summary presented to user on 2026-09-15 for the prioritized list.

## 2026-09-16 — Rebrand PROFIN to FAMILYFIN
- Type: refactor
- Files touched: app/config.py, app/db/database.py, app/settings/settings_manager.py, app/gui/main_window.py, app/gui/login_window.py, app/gui/settings_view.py, app/gui/categories_view.py, app/ingestion/excel_parser.py, app/ingestion/validator.py, main.py, plus header docstrings across app/auth, app/categorization, app/ingestion, app/gui and app/db/models.py; tests/test_runtime_paths.py, tests/test_theme_views.py; tools/ExportVersionHandler.py, tools/build_windows.py; CHANGELOG.md title; .claude/agents/familyfin-engineer.md (renamed from profin-engineer.md), .github/agents/familyfin-developer.agent.md (renamed from profin-developer.agent.md), this file, and lessons_learned.md.
- Summary: Renamed the app's brand identity from PROFIN to FAMILYFIN everywhere user-visible (window titles, QSettings org/app identity, default data folder, agent definitions). New installs use %LOCALAPPDATA%\FAMILYFIN and QSettings("GEVernova","FAMILYFIN"); existing installs auto-migrate via a new one-time QSettings key copy in get_user_data_dir() plus the existing legacy-data-folder migration path (extended with an explicit old %LOCALAPPDATA%\PROFIN candidate). Internal-only filenames (profin.db, profin.log, profin_backup_*.zip) and build tooling (build_profin.spec/.bat, PROFIN_CHANNEL env var, PROFIN.* build artifact naming) were intentionally left unchanged per user decision. Historical versions/*.md/.prj snapshots and existing CHANGELOG.md dated entries were left untouched as frozen historical records.
- Validation: tests/test_runtime_paths.py (12 passed, including a new migration test) and tests/test_theme_views.py (1 passed) after the rename; full `pytest` suite green (49 passed) before Phase B.
- Follow-up: proceed to Phase B (fix the rules-engine overwrite/full-rescan bug in TransactionsView/RulesView).

## 2026-09-16 — Fix rules-engine overwrite bug + reduce matching overhead
- Type: bugfix
- Files touched: app/gui/transactions_view.py, app/categorization/rules_engine.py, tests/test_rules_engine.py, docs/agent/lessons_learned.md.
- Summary: `TransactionsView._refresh_with_rules()` (run on every passive tab visit via `showEvent`) called `apply_rules_and_commit(..., overwrite=True)`, silently re-categorizing transactions a user had manually fixed; changed to `overwrite=False` to match the function's own documented intent. Separately investigated `RulesView`'s full-rescan-per-CRUD-action pattern flagged in the original survey: it only recomputes on genuine rule/data changes (create/edit/delete/apply), not on passive navigation, so no correctness bug there — instead applied the lower-risk constant-factor fix from the survey (item #2): `rules_engine.py` now lower-cases each rule's pattern once per `apply_rules()` call and each transaction's issuer/concept once per transaction, instead of re-lowercasing on every rule x field comparison. Also removed the redundant per-row `session.get()` loop in `apply_rules_and_commit` by reusing the `Transaction` objects `apply_rules` already loaded (new `RulesEngineResult.transactions_by_id`).
- Validation: added `test_r11_no_overwrite_preserves_manual_category` and `test_r12_apply_and_commit_persists_multiple_assignments` to tests/test_rules_engine.py; full `pytest` suite green (51 passed).
- Follow-up: the deeper architectural items from the original survey (no DB indexes, eager view construction at startup, no background/worker threads for the rules engine or imports, ExpensesView loading full ORM rows instead of SQL-side aggregation) remain open and were not part of this fix — revisit per the earlier prioritized proposal if the user wants to continue.

## 2026-09-16 — Catch up missing GitHub tag for v0.2.5 (release)
- Type: release
- Files touched: none in the working tree (git tag only, no source changes).
- Summary: `CHANGELOG.md` already documented FAMILYFIN v0.2.5 (commit `9dd82c8`) as released, but no matching git tag existed (repo only had `v0.2.1`) and no GitHub Release had been published. Created annotated tag `v0.2.5` directly on commit `9dd82c8` (not on HEAD, since the working tree had unrelated in-progress UI-overhaul and release-skill changes that are not part of the 0.2.5 release) and pushed it to `origin`.
- Validation: `git tag -v v0.2.5` confirms the tag points at `9dd82c8` with the CHANGELOG's 0.2.5 description as the tag message; `git push origin v0.2.5` succeeded (`new tag v0.2.5 -> v0.2.5`).
- Follow-up: `gh` CLI is not installed in this environment, so the actual GitHub Release (title/notes/prerelease flag) was not published — only the tag. Run `gh release create v0.2.5 --title "FAMILYFIN v0.2.5" --notes-file <changelog excerpt>` from a machine with `gh` installed and authenticated to finish publishing. The in-progress UI-overhaul + release-skill changes remain uncommitted and will ship as part of the next release.

## 2026-09-16 — UI overhaul: palette refresh, card elevation, chart axis labels, scroll areas, theme-token consistency, login DB-path display
- Type: feature (UI-only, pre-approved plan)
- Files touched: app/config.py; new app/gui/_style_utils.py; app/gui/dashboard.py, app/gui/expenses_view.py, app/gui/categories_view.py, app/gui/transactions_view.py, app/gui/rules_view.py, app/gui/settings_view.py, app/gui/main_window.py, app/gui/login_window.py.
- Summary: (1) Refreshed `THEMES["light"]`/`THEMES["dark"]` token values in config.py (softer bg/borders, refreshed sidebar navy, kept all existing keys) and added new tokens (`income_hover`, `expense_hover`, `selection_grad_*`, `selection_hover_*`, `selection_text`) so previously-hardcoded gradient/hover colors in categories_view/transactions_view/rules_view could route through the theme instead of literal hex. (2) Added `app/gui/_style_utils.py::apply_card_shadow()` (QGraphicsDropShadowEffect built from the `card_shadow` token) and applied/re-applied it on every theme toggle to: dashboard's KPI/chart/recent-transactions cards, expenses_view's chart cards and KPI chips, categories_view's action panel, transactions_view's filter/pagination bars, settings_view's nav sidebar, and rules_view's bottom bar. (3) Added Y-axis currency labels + left axis line to `_MonthlyChartWidget` (dashboard.py) and `_ComparisonChartWidget`/`_MonthlyCategoryChartWidget` (expenses_view.py), mirroring `_LineChartWidget`'s existing pattern; replaced fixed-character-count label truncation with `QFontMetrics.elidedText`-based dynamic elision in the dashboard donut/hbar legend and the comparison chart's category labels. (4) Wrapped `_ComparisonChartWidget` and `_MonthlyCategoryChartWidget` canvases in horizontal `QScrollArea`s (matching the existing vertical pattern already used by `_CategoryChartWidget`'s hbar mode) so many categories/months scroll instead of squeezing. (5) Replaced remaining generic hardcoded hex in main_window.py (window-control icon colors, two theme-ternaries that duplicated existing token values) with theme-token lookups; left intentional fixed semantic colors (income/expense/investment, danger-hover reds) untouched. (6) Replaced login_window.py's read-only `QLineEdit` DB-path field with a new `_ElidedPathLabel` (QLabel subclass) that elides the path from the front via `QFontMetrics.elidedText(..., ElideLeft)`, re-elides on resize, and always exposes the full path via `setToolTip`; `_select_data_folder`/`_refresh_database_info` updated to use `.set_path()`/`.full_path()`.
- Validation: full `pytest` suite green (51 passed, same as baseline) including `tests/test_theme_views.py`; targeted offscreen (`QT_QPA_PLATFORM=offscreen`) smoke tests instantiating and painting `_CategoryChartWidget`, `_MonthlyChartWidget`, `_ComparisonChartWidget`, `_MonthlyCategoryChartWidget`, `_LineChartWidget` in both themes, and instantiating `LoginWindow` to confirm the new elided path label renders and its tooltip carries the full path; confirmed `python -c "import ..."` succeeds for every touched gui module.
- Follow-up: did not add drop-shadow to `main_window.py`'s `sidebarUserCard` (dark-on-dark, low visual payoff) or to individual `QGroupBox` sections in settings_view.py's per-page forms — out of the plan's explicit card list, can revisit if requested. No data-layer files were touched.

## 2026-09-16 — Agent guardrails + release v0.2.5
- Type: refactor
- Files touched: .claude/agents/familyfin-engineer.md, .github/agents/familyfin-developer.agent.md, app/config.py (via tools/ExportVersionHandler.py), CHANGELOG.md (via tool), versions/FAMILYFIN.R.0.2.5.md, versions/FAMILYFIN.R.0.2.5.prj, this file.
- Summary: Added four guardrail rules to both agent definitions, distilled from this session's two lessons: (1) never let a passive Qt event trigger an overwriting pass, (2) append new fallback/legacy candidates at the lowest priority in a search list, never the front, (3) an identity rename that is also a storage key needs an explicit one-time migration plus a test, (4) drive interactive CLI tools non-interactively via direct function calls/monkeypatching rather than guessed stdin. Ran `tools/ExportVersionHandler.py`'s `export_version()` directly (monkeypatching only the free-text description prompt, per guardrail 4) to cut release v0.2.5: bumped `app/config.py` APP_VERSION to 0.2.5, wrote `versions/FAMILYFIN.R.0.2.5.md/.prj`, and prepended the CHANGELOG.md entry describing the FAMILYFIN rebrand and the rules-engine overwrite fix.
- Validation: full `pytest` suite green (51 passed) both before and after the version bump.
- Follow-up: commit and push v0.2.5 to GitHub with a descriptive commit message; Windows build (`build_profin.bat`) was not run as part of this release per no explicit request to package a new binary.

## 2026-09-16 — Release v0.3.0
- Type: release
- Files touched: app/config.py (via tools/ExportVersionHandler.py), CHANGELOG.md (via tool), versions/FAMILYFIN.R.0.3.0.md, versions/FAMILYFIN.R.0.3.0.prj, plus the full set of UI-overhaul and release-skill files from the two entries above (all committed together in this release).
- Summary: Used the new `release` skill (`.claude/skills/release/SKILL.md`) to cut a main/stable release. Confirmed with the user this bundles the pending UI overhaul + new `/release` skill + agent-guardrail doc updates as a MINOR bump (0.2.5 -> 0.3.0, backward compatible, no data migration). Ran `export_version(version="0.3.0", ver_type="R", also_prj=True)` non-interactively, staged all pending files explicitly (never `git add -A`), committed `chore(release): v0.3.0`, created annotated tag `v0.3.0`, and pushed both `main` and the tag to `origin`.
- Validation: `git push origin main` -> `9dd82c8..c528789 main -> main`; `git push origin v0.3.0` -> `new tag v0.3.0 -> v0.3.0`.
- Follow-up: `gh` CLI was installed mid-session (`winget install --id GitHub.cli`); the user then completed `gh auth login --web` (device code flow, logged in as `pbermudezcc`) in a follow-up turn, and the GitHub Release was published: `gh release create v0.3.0 --title "FAMILYFIN v0.3.0" --notes "<release notes>"` -> https://github.com/pbermudezcc/PROFIN/releases/tag/v0.3.0.

## 2026-09-16 — Fix build_profin.spec version hardcoding + attach v0.3.0 Windows build
- Type: bugfix
- Files touched: build_profin.spec.
- Summary: User asked to build the Windows exe and attach it to the v0.3.0 release. First `python tools/build_windows.py --channel release` run silently produced `dist/PROFIN.R.0.2.4/PROFIN.R.0.2.4.exe` instead of `PROFIN.R.0.3.0` — `build_profin.spec` had `APP_VERSION = "0.2.4"` hardcoded (stale since the 0.2.4 release) instead of reading `app/config.py` like `tools/build_windows.py::read_version()` already does, so the two version sources had silently diverged. Fixed the spec to derive `APP_VERSION` from `app/config.py` at build time (mirrors `read_version()`), matching `tools/build_windows.py`'s existing convention. Rebuilt; artifact this time correctly landed at `dist/PROFIN.R.0.3.0/PROFIN.R.0.3.0.exe` (~15 MB) and was uploaded via `gh release upload v0.3.0 dist/PROFIN.R.0.3.0/PROFIN.R.0.3.0.exe`.
- Validation: `gh release view v0.3.0 --json assets` confirms `PROFIN.R.0.3.0.exe` is attached to the release. `build/` and `dist/` are gitignored so only `build_profin.spec` needed committing.
- Follow-up: worth adding a CI/pre-release check that fails the build if the spec's resolved `APP_VERSION` doesn't match `app/config.py`, to catch this class of drift automatically next time (not done here — out of scope of this session's ask).

## 2026-09-26 — Schema migrations, manual-category protection, real auto-backups
- Type: feature + bugfix
- Files touched: app/db/models.py, app/db/database.py, app/categorization/rules_engine.py, app/categorization/category_manager.py, app/settings/settings_manager.py, app/gui/main_window.py, app/gui/settings_view.py, app/gui/rules_view.py (comment), conftest.py, tests/test_rules_engine.py, new tests/test_backup_and_migrations.py, docs/agent/lessons_learned.md.
- Summary: Added a PRAGMA user_version migration runner (`migrate_schema`, snapshot before migrating) with migration v1 adding and backfilling `transactions.category_source`; the rules engine never overwrites 'manual' categories. The auto-backup setting (previously saved but never acted on) now runs on login and every 30 min when due, keeping the newest 10 automatic zips (manual backups never pruned; default folder `%LOCALAPPDATA%\FAMILYFIN\backups`). Backups use the SQLite backup API; restore is staged and applied on next start. Tests no longer touch the real registry.
- Validation: full `pytest` 63 passed (51 before + 12 new); registry value count unchanged across the run; migration dry-run on a copy of the real DB: 2105 rule / 1 manual / 6621 uncategorized, integrity ok, 0.25 s.
- Follow-up: first launch migrates the real DB automatically (pre-migration copy written next to it). Remaining review items: rotate the committed OpenAI key, Float→cents for money, AuditLog.record_id, global-rule/per-user category mismatch.

## 2026-09-27 — Rewrite familyfin-engineer agent prompt with full codebase context
- Type: refactor
- Files touched: .claude/agents/familyfin-engineer.md, this file.
- Summary: Rewrote the agent definition after a full read of app/, tests/, tools/ and docs/agent/. Added a module map, domain invariants (user_id scoping, amount sign, type derived only from sign, category tree, category_source, rules/duplicate semantics, CaixaBank format), runtime/persistence rules (data-dir resolution, import-time engine binding, migration recipe, staged restore, SQLite closing), GUI conventions (shared session, eager views, refresh-on-show, theme contract, IncomesWindow parity, thread risk), a known-open-issues list, and a stricter workflow/definition of done. Kept all previous rules and guardrails.
- Validation: `python -m pytest -q` → 63 passed (no code changed). Facts cited in the prompt checked against the current working tree.
- Follow-up: `.github/agents/familyfin-developer.agent.md` still describes v0.2.5; suspected data-loss path in `SettingsManager.apply_data_folder` (overwrites an existing non-empty target DB) is listed as an open issue, not fixed.

## 2026-09-27 — Close every open issue from the agent's backlog
- Type: bugfix + optimization-applied (explicitly approved by the user: "arregla todo lo que esté abierto")
- Files touched: app/db/models.py, app/db/database.py, app/categorization/rules_engine.py, app/ingestion/excel_parser.py, app/ingestion/importer.py, app/ingestion/duplicate_detector.py, app/settings/settings_manager.py, new app/analytics/period_summary.py, app/gui/main_window.py, app/gui/expenses_view.py, app/gui/transactions_view.py, app/gui/import_view.py, app/gui/settings_view.py, app/gui/login_window.py, app/gui/dashboard.py; tests: test_backup_and_migrations.py, test_rules_engine.py, test_duplicate_detector.py, test_excel_parser.py, test_runtime_paths.py, new test_importer.py, test_period_summary.py, test_gui_smoke.py; docs/agent/*, .claude/agents/familyfin-engineer.md, .github/agents/familyfin-developer.agent.md.
- Summary: Migration v2 (transaction indexes + amounts rounded to cents, parser rounds too) and v3 (repair cross-user categories); global rules resolved per user; AuditLog.record_id and raw_details now persisted; D-3 implemented for zero-amount exact matches; data-folder switch made non-destructive and session-safe; import worker uses its own Session; views built lazily; passive rules pass only touches uncategorized rows; Gastos/Ingresos aggregate in SQL via app/analytics; dashboard NULL-balance crash and daily-chart ordering fixed.
- Validation: `python -m pytest -q` → 82 passed (63 before). Offscreen smoke test of MainWindow + all data views. Dry run of migrations v2+v3 on a snapshot copy of the real DB: v1→v3, 3 indexes, totals unchanged, 44 remapped / 5 cleared cross-user categories, integrity ok (copy deleted afterwards).
- Follow-up: money is still a REAL column (rounded to cents, not integer cents) — full integer-cents migration left for an explicit decision. The real DB migrates to v3 on the next app start (pre-migration snapshot written next to it). Rules engine still runs on the GUI thread (now only over uncategorized rows on passive refresh).

## 2026-09-27 — Release tooling hardening + broader test coverage
- Type: feature (tooling) + test
- Files touched: new tools/release_tools.py, tools/ExportVersionHandler.py, tools/build_windows.py, .claude/skills/release/SKILL.md, .gitignore, `.env` removed from the index (file kept locally); new tests/test_release_tooling.py, tests/test_schema_and_startup.py, tests/test_category_manager.py, tests/test_import_pipeline.py.
- Summary: The release skill now delegates every mechanical check to `tools/release_tools.py` (`preflight` test gate + repo/version/tag/gh checks, `draft` notes from commits/areas/migrations since the last tag, `verify-notes`, `package` zips the one-dir build exe + `_internal`, `postflight` checks CHANGELOG/config/snapshot/tag and the published GitHub Release). `export_version()` takes `description`/`notes` non-interactively and rejects bare `---` lines before writing. `build_windows.py` writes a `.build-ok` marker only after a successful build, so a partial build cannot be packaged. `.gitignore` now covers `*.db`, WAL/SHM and backup zips. New tests: fresh vs. migrated schema identical, every model column exists after migrations, init_db idempotent + safety copy, all modules import; category seeding/deletion/manual source; end-to-end import (no duplicates on re-import, overlapping exports, multi-account file rejected before any write).
- Validation: `python -m pytest -q` → 147 passed (82 before). `release_tools.py preflight --skip-tests` correctly flags the dirty tree; `draft --version 0.4.0` produces a draft with commits, areas and agent-log sources.
- Follow-up: the OpenAI key in `.env` is still in git history (commit `d886c33`, already pushed) — it must be rotated on the OpenAI side; untracking does not remove it. All of this work plus the 2026-09-26/27 entries is uncommitted and ready for the next release (schema migrations v1–v3 ⇒ MINOR, 0.4.0).

## 2026-09-27 — Release v0.4.0
- Type: release
- Files touched: app/config.py, CHANGELOG.md (via `export_version`), versions/FAMILYFIN.R.0.4.0.md, versions/FAMILYFIN.R.0.4.0.prj; pending work committed first as 05c4316 (db), 5a5e173 (import), 2b1979e (gui), 3ba2edf (release tooling), d1adab3 (agent docs). Note: the already-staged `.env` removal landed in 05c4316.
- Summary: Stable (R) release from `main` using the `/release` skill. MINOR bump 0.3.0 -> 0.4.0 because of schema migrations v1–v3 (v0 -> v3 on first start, pre-migration copy written). Release commit `fa5452b chore(release): v0.4.0`, annotated tag `v0.4.0` (notes verbatim), asset `PROFIN.R.0.4.0-windows.zip` (~84 MB, one-dir build + `_internal`).
- Validation: preflight 147 passed; `verify-notes` OK; `tests/test_release_tooling.py` 19 passed after the version bump; postflight OK locally and `--published`. Release: https://github.com/pbermudezcc/PROFIN/releases/tag/v0.4.0
- Follow-up: rotate the OpenAI key still present in git history (`d886c33`); integer-cents money migration and moving the rules engine off the GUI thread remain open.

## 2026-09-27 — Inversiones F0 (market-data spike) + F1 (schema v4 and portfolio engine)
- Type: feature
- Files touched: requirements.txt (yfinance>=1.7.0), build_profin.spec (app.investments), app/db/models.py, app/db/database.py, new app/investments/{__init__,portfolio,returns}.py, new tests/test_investments_portfolio.py, tests/test_backup_and_migrations.py, new docs/investments_plan.md, this file.
- Summary: F0 checked yfinance 1.7.0 from the home network (search, batch download, fund NAV `0P00008XG7.F`, EURUSD=X history) and inside a PyInstaller 6.22 one-dir exe (~+12 MB, no extra hooks). F1 adds the shared `securities` catalogue, the per-user `investment_transactions` ledger (buy/sell/dividend/split/grant; EUR amount from the broker statement plus the price in the quote currency) and the `price_history`/`fx_rates` cache. It also adds migration v4 (additive only), a FIFO replay engine (positions, lots, realized gains per lot/tax year, oversell detection with Spanish messages, EUR valuation with FX) and XIRR.
- Validation: `python -m pytest -q` → 178 passed (147 before). Dry run of migration v4 on a snapshot copy of the real DB: v3→v4 in 0.03 s, 8727 transactions and SUM(amount) unchanged, integrity ok, FK check clean (copy deleted).
- Follow-up: F2 market-data provider + cache + background refresh. The real DB migrates to v4 on the next app start (pre-migration copy written). The PyInstaller build must stay under a short path (lxml ships deep resource paths).

## 2026-09-27 — Inversiones F2: market-data provider, price/FX cache, background refresh
- Type: feature
- Files touched: new app/investments/{market_data,prices,refresh_worker}.py, new tests/test_investments_prices.py, build_profin.spec (yfinance hidden import), docs/investments_plan.md, this file.
- Summary: Swappable MarketDataProvider protocol with a lazily-imported YahooProvider (search, one-off security info, batched daily closes; network failures → MarketDataError with Spanish text). SQLite price/FX cache filled by refresh_market_data (first run from first trade −7 d, then a 5-day overlap window; upserts never overwrite `manual` prices; chunked INSERTs). Read helpers latest_prices (with previous close) and fx_rate (GBp ×100), set_manual_price, find_or_create_security. PriceRefresher runs one refresh at a time on a QThread with its own Session and tracks last_success for the 15-minute staleness rule.
- Validation: `python -m pytest -q` → 200 passed (178 before; 19 new + 3 incidental from parametrization). Live run against Yahoo on a temp DB with ENR.DE/TKE.DE/AMZ.DE/GEV/0P00008XG7.F: first refresh 0.4 s (1986 prices, 453 FX), incremental 0.2 s, values sane.
- Follow-up: F3 operations tab (entry dialog with symbol search + ledger_error validation). Yahoo Close is split-adjusted: on a split (F6) the full history of that security must be re-downloaded.

## 2026-09-27 — Inversiones F3: operations tab, entry dialog, security search
- Type: feature
- Files touched: new app/investments/ledger.py, app/investments/prices.py (security_from_info, close_on), app/investments/refresh_worker.py (longer shutdown wait), new app/gui/{investments_view,investment_dialogs,_background,_format}.py, app/gui/main_window.py (InvestmentsView registered, closeEvent waits for background threads), new tests/test_investments_ledger.py, tests/test_investments_gui.py, tests/test_gui_smoke.py (visits investments), docs/investments_plan.md, this file.
- Summary: Ledger writes are replayed per security before committing (edits/deletes that break a later sale are rejected) and are user-scoped. The Inversiones view replaces the placeholder: an Operaciones tab with filters, a sortable table and edit/delete, plus a TransactionDialog (EUR estimate from cached or fetched FX, close-of-day hint, held shares for sales, Spanish number boxes) and a SecuritySearchDialog. Network calls run in QThread subclasses kept alive in a module registry until they finish.
- Validation: `python -m pytest -q` → 234 passed (200 before). Screenshots rendered with the real Windows platform (WA_DontShowOnScreen) in both themes; fixed label backgrounds, decimal comma, field widths and a truncated hint.
- Lesson: in PySide6 a signal connected to a lambda/plain function runs in the emitting thread; deliver worker results through a bound method of a GUI-thread QObject. The price-refresh worker must bind its Session to the view's engine, not the module default (the GUI smoke test caught this).
- Follow-up: F4 positions tab + KPI cards. Spin boxes do not show thousands separators (display only).

## 2026-09-27 — Inversiones F4: positions tab and KPI cards
- Type: feature
- Files touched: new app/investments/snapshot.py, new app/gui/investment_positions.py, app/gui/investments_view.py (positions panel, default tab, explicit default sort), new tests/test_investments_snapshot.py, tests/test_investments_gui.py, docs/investments_plan.md, this file.
- Summary: portfolio_snapshot prices the FIFO positions from the local cache (EUR via the FX rate of each close, day change against the previous close and its FX rate, weights, YTD/total realized and dividends, XIRR). Missing price/FX → valued at cost and listed in a banner; prices older than 7 days are flagged. The Posiciones tab shows 7 KPI cards (dashboard _KPICard), the banner, and a positions table (closed positions toggle, broker filter, double-click → operations of that security).
- Validation: `python -m pytest -q` → 246 passed (234 before). Screenshots with real Yahoo prices on a temp DB in both themes.
- Lesson: QTableView.setSortingEnabled(True) immediately sorts by the header's current indicator (column 0), so "sortColumn() < 0" never means "not sorted yet"; set the default order explicitly with sortByColumn when building the table.
- Follow-up: F5 charts (portfolio value vs invested capital over time, benchmark, allocation donut).

## 2026-09-27 — Inversiones F5: evolution chart, benchmark, allocation
- Type: feature + bugfix
- Files touched: new app/investments/{benchmarks,performance}.py, app/investments/prices.py (benchmarks refreshed, global start fallback), new app/gui/investment_charts.py, app/gui/investments_view.py (Evolución / Distribución tabs, lazy reload, benchmark selection), app/gui/dashboard.py (donut direction fix), new tests/test_investments_performance.py, tests/test_investments_gui.py, docs/investments_plan.md, docs/agent/lessons_learned.md, this file.
- Summary: Daily portfolio value against FIFO invested capital and a benchmark fed with the same cash flows (EUR ETFs/indices IWDA.AS, SXR8.DE, ^IBEX, verified on Yahoo), plus allocation by security/broker/currency/asset type. QPainter line chart with periods, remembered benchmark choice, hover read-out and summary; the allocation reuses the dashboard donut card. Fixed the dashboard donut slice direction.
- Validation: `python -m pytest -q` → 258 passed (246 before). Screenshots with real Yahoo prices in both themes (evolution with hover, allocation).
- Follow-up: F6 dividends/splits/tax report. The dashboard donut still uses English-style number formatting (`€11.1k`, `33.6%`) — pre-existing.

## 2026-09-27 — Inversiones F6: dividends/splits detection, split handling, tax report
- Type: feature
- Files touched: app/investments/{market_data,prices,portfolio,ledger,performance}.py, new app/investments/{events,tax_report}.py, app/gui/investment_dialogs.py (prefill, PendingEventsDialog, split-aware close hint), new app/gui/investment_tax.py, app/gui/investments_view.py (Fiscalidad tab, pending-events banner), new tests/test_investments_tax.py, tests/test_investments_gui.py, docs/investments_plan.md, this file.
- Summary: Yahoo `actions` → pending dividends (held the day before ex-date, no dividend registered in [-5, +60] days) and splits (±7 days), dismissible per user (QSettings), offered through a banner and a prefilled entry form. Saving/deleting a split resets that security's downloaded prices (Yahoo re-adjusts history) and history/close hints multiply pre-split closes by later split ratios. Yearly tax report: FIFO sale lots, anti-repurchase rule (2 months listed / 1 year funds; repurchased shares block losing lots in FIFO order), dividends gross/withholding/net, Excel export (Resumen / Ganancias y pérdidas / Dividendos). User decisions: detect+propose, screen+Excel, flag the rule, single withholding field (no migration).
- Validation: `python -m pytest -q` → 275 passed (258 before). Live run: 6 real unregistered GEV/ENR dividends detected; tax tab and Excel checked in both themes (blocked TKE loss −43,80 € of −87,60 € with 2 of 4 shares repurchased).
- Lesson: QHeaderView ResizeToContents does not re-measure rows appended to an unsorted QStandardItemModel, and resizeColumnsToContents() removes a Stretch section — call it, then restore the stretch.
- Follow-up: F7 (optional broker CSV import) and F8 (release 0.5.0, schema v4 ⇒ MINOR).

## 2026-09-27 — Inversiones F7: DEGIRO account statement import
- Type: feature + schema migration v5
- Files touched: app/db/models.py (InvestmentTransaction.broker_ref + partial unique index), app/db/database.py (migration v5), app/investments/ledger.py (broker_ref, import_drafts all-or-nothing, existing_refs), new app/investments/importers/{__init__,degiro,symbols,service}.py, new app/gui/investment_import.py, app/gui/investments_view.py (import buttons), new tests/test_investments_import.py, tests/test_investments_gui.py, tests/test_backup_and_migrations.py, docs/investments_plan.md, this file.
- Summary: Parses DEGIRO's "Estado de cuenta" (.xlsx/.csv, ES/EN): merges partial fills per order id, attaches order costs (fee, Spanish transaction tax), AutoFX EUR for foreign-currency trades, dividends grouped with their withholding and valued with the EUR actually credited by the next FX conversion (pro rata). Symbol suggestion searches ISIN + name variants, keeps listings in the trade currency and picks the one whose close matches the prices paid (venue preference only among the same ticker, or a regional German quote vs the home market). Import preview in the GUI; import is atomic and deduplicated by broker_ref; the user runs it (always into the logged-in user).
- Validation: `python -m pytest -q` → 301 passed (275 before). Rehearsal on a snapshot copy of the real DB with the user's real statement: v4→v5, 21 operations imported for PEDRO, re-import 0 new, bank transactions unchanged, integrity ok (copy deleted). The real statement was only read, never copied into the repo or tests (tests use a synthetic statement).
- Lesson: Yahoo ISIN search returns the home listing; European brokers trade US shares on German venues in EUR — verify a candidate listing by comparing its close on the trade date with the price paid, and never let venue preference switch to a different ticker (VUAA vs VUSA are different share classes).
- Follow-up: the user imports the file from the app. Other brokers (Trade Republic, MyInvestor, Scalable) need a sample statement each. F8 release (schema v5).

## 2026-09-27 — Inversiones F7b: MyInvestor account-movements import
- Type: feature
- Files touched: new app/investments/importers/{base,myinvestor}.py, app/investments/importers/{degiro,symbols,service}.py, app/gui/investment_import.py (rewritten: multi-broker, editable kind/quantity, background quantity estimation), app/gui/investments_view.py (button labels), new tests/test_investments_import_myinvestor.py, docs/investments_plan.md, this file.
- Summary: Common ImportRow/Statement model for all readers; parse_any detects DEGIRO vs MyInvestor by header. The MyInvestor CSV has only truncated concepts and amounts: kinds are guessed (sign, and a credit below 5 % of what was invested is a dividend), truncated spellings are grouped (fund abbreviations unified), and missing quantities are estimated from the chosen symbol's close/NAV (whole shares for stocks, sell-all when close to holdings). Symbol suggestion filters by asset type (funds vs stocks/ETFs), expands abbreviations, ranks funds by name similarity (EUR, unhedged) and stocks by main venue when there is no price to compare. The preview lets the user change each movement's kind and fix quantities.
- Validation: `python -m pytest -q` → 316 passed (301 before). Rehearsal on a snapshot copy of the real DB (already at v5) with the user's real MyInvestor CSV for MARIA: 14 movements imported, re-import 0 new, bank transactions unchanged, integrity ok (copy deleted). The real CSV was only read; tests use a synthetic one.
- Lesson: a truncated-name statement cannot identify a fund's share class (hedged/unhedged, currency) — propose, flag "compruébalo", and let the user confirm; ISIN is the only reliable key.
- Follow-up: user runs the import as MARIA and checks the fund classes / Rovi share count against the MyInvestor app. F8 release.

## 2026-10-04 — Acceso web en la red local (W1–W5): HTTPS, login, consulta y ediciones ligeras
- Type: feature
- Files touched: new app/web/{__init__,app,deps,render,security,tls,server,periods}.py, app/web/routers/{auth,dashboard,flows,transactions,investments,more}.py, app/web/templates/**, app/web/static/** (css, js, vendored htmx 2.0.4 + Chart.js 4.4.7, icons, manifest, sw.js); new app/analytics/{dashboard_summary,transaction_search}.py; new app/gui/{web_access_view,web_bridge}.py; app/gui/dashboard.py (uses dashboard_summary), app/gui/settings_view.py ("Acceso web" page, data-folder switch revokes web sessions), app/gui/main_window.py (reload views after web writes), app/categorization/category_manager.py (assign_user_transaction_category), app/db/database.py (busy_timeout), main.py, build_profin.spec, requirements.txt, tools/make_web_icons.py, tests/test_web.py, tests/test_web_tls.py.
- Summary: FastAPI + uvicorn server embedded in the desktop app (Configuración → Acceso web, off by default, port 8765). HTTPS only: private "FAMILYFIN Home CA" (EC P-256) + server certificate for the LAN IPs/hostname.local, keys encrypted with a DPAPI-protected passphrase in %LOCALAPPDATA%/FAMILYFIN/web_tls (never in the data folder/backups), TLS ≥ 1.2 with ECDHE+AEAD. iPhone/iPad onboarding: temporary (10 min) HTTP listener on port+1 serving only the CA .mobileconfig + steps, opened from a QR in Settings. Server-side sessions (__Host- cookie, Secure/HttpOnly/SameSite=Strict, 30 min idle / 12 h absolute, rotation on login), login backoff + lockout, CSRF token + same-origin check on every write, strict CSP (no inline script/style), HSTS, LAN-only clients, no docs/OpenAPI. Every query scoped to the session user; ids from other users → 404. Mobile-first UI (bottom tab bar, iPad sidebar, bottom sheets, light/dark, PWA). Screens: Resumen, Gastos/Ingresos (periods, drill-down), Movimientos (search/filters/infinite scroll, recategorize → category_source='manual'), Inversiones (positions, evolution vs benchmark, allocation, operations add/edit/delete via ledger, tax report + Excel), Más (categories, rules, accounts, imports, sessions). Web writes go to audit_log with the device IP and reload the desktop views (queued Qt signal). Web never downloads prices.
- Validation: `python -m pytest -q` → 427 passed (316 before). Real HTTPS server tested (CA chain, SAN, TLS 1.1 refused, onboarding serves only the CA). Screenshots on a synthetic demo DB at iPhone (390×844, light/dark) and iPad sizes via headless Chrome; desktop page rendered. PyInstaller build checked: all web modules and templates/static bundled.
- Lessons: see lessons_learned.md (Origin: null under no-referrer; Chart.js resize loop in `1fr` grids).
- Follow-up: user enables it on the PC, allows the firewall prompt on private networks only, and runs "Configurar dispositivo" once per iPhone/iPad. Future parity phases: imports by upload, rules/categories editing.

## 2026-10-04 — Release v0.5.0
- Type: release
- Version / channel / tag: 0.5.0 / R (stable, main) / `v0.5.0` (annotated, notes as message)
- Commit: `0f411fd` chore(release): v0.5.0 — on top of `36f2d08` (settings rename), `82e73b2` (MyInvestor F7b), `0f9f5c0` (LAN web access) and Inversiones F0–F7.
- Asset: `PROFIN.R.0.5.0-windows.zip` attached to https://github.com/pbermudezcc/PROFIN/releases/tag/v0.5.0
- Snapshot: `versions/FAMILYFIN.R.0.5.0.md/.prj` (ExportVersionHandler); CHANGELOG entry with the full notes.
- Validation: preflight → 430 passed; verify-notes OK; postflight OK (local) and OK `--published`.
- Note: schema v3 → v5 on first start for 0.4.0 installs (automatic copy `profin.pre-migration-v5.*.db`).

## 2026-10-04 — Session close: LAN web access shipped in v0.5.0; next goal = full web parity
- Type: session summary
- Done this session:
  1. Plan for LAN web access (FastAPI embedded, HTTPS mandatory, iOS-first UI) approved by the user; W1–W5 implemented (see the entry "Acceso web en la red local" above).
  2. Verification in a browser at iPhone/iPad sizes (headless Chrome over CDP on a synthetic demo DB); fixed: forms rejected under `Referrer-Policy: no-referrer` (Origin null), Chart.js resize loop that froze the tab, chip class clash, stale assets behind the service worker (content-hashed static URLs), login page ignoring `hx-boost="false"`.
  3. User feedback after onboarding: the "Configurar dispositivo" button looked dead while the server was off (it was disabled) → it now offers to start the server; missing LAN address gives an explicit error.
  4. User feedback from the iPad: choosing a category did not save it → the picker saves on tap, has a search box and the transaction's type first; middleware 403s are logged. Category list verified complete against the real DB (read-only): PEDRO 49, MARIA 54.
  5. Release v0.5.0 (stable, main): commits `36f2d08` (settings rename), `82e73b2` (MyInvestor F7b), `0f9f5c0` (web), `0f411fd` (release), tag `v0.5.0`, zip on GitHub, ExportVersionHandler snapshot. 430 tests.
  6. User validated: the exe from the zip opens, login as PEDRO works, the web service keeps running; iPad onboarded and working.
- Next session goal (user): turn the web app into full management with the same capabilities as the desktop app, optimised for iPad (main use), iPhone still supported. Starting brief with the gap inventory and guidelines: `docs/web_parity_plan.md`.

## 2026-10-04 — Web parity W6–W8: design refresh, categories/rules, statement import by upload
- Type: feature
- Files touched: app/web/static/{css/app.css, js/app.js, js/charts.js, icons/sprite.svg, fonts/ (Inter variable + OFL)}, app/web/templates/** (base, _ui, all pages; new importar*.html, partials/{category_form,rule_form,rule_preview}.html), app/web/{render,app,security,server,uploads}.py, app/web/routers/{admin,imports,more}.py; new app/categorization/admin_service.py, app/ingestion/import_service.py; app/gui/{categories_view,rules_view,import_view}.py; tests/test_web.py.
- Summary: W6 professional design refresh (tokens cleaned, vendored Inter, iPad icon rail / full sidebar with an Administración group, breadcrumbs, sortable data tables, master–detail Movimientos on iPad landscape, confirm dialog replacing window.confirm, toasts, admin tiles; "Más" → "Ajustes"). W7 categories & rules CRUD on the web through a new ownership-checked service shared with the desktop views (global rules read-only on the web; deleting a category resets its transactions to the rules and removes its rules; live pattern preview; priority up/down; apply to uncategorized or recalculate all, manual categories always kept). W8 CaixaBank import by upload: size/extension/signature checks, parse in a private temp folder, preview kept server-side under a token bound to user + browser session (15 min), checkboxes only on possible duplicates, result page; temp folders removed on confirm/cancel/expiry/server stop and orphans swept on start. Request body caps added to the hardening middleware (256 KB forms, 6 MB uploads). Desktop ImportView uses the shared import_service.
- Validation: full `pytest` → 453 passed before the last fixes; tests/test_web.py → 100 passed after them. Browser (Chrome, demo DB, localhost preview): every page at desktop / iPad portrait (820) / iPhone (390), light and dark; rule create with live preview, apply, priority move; category delete through the confirm dialog; detail pane save; statement upload → preview → confirm (5 imported, 3 categorized) → same file again (0 new, 1 to review, 4 skipped).
- Lessons: see lessons_learned.md (inherited hx-target, data-attribute clash with the confirm dialog, refresh swapping body wiped the toast, FastAPI without add_event_handler).
- Follow-up: user uploads a real CaixaBank statement from the iPad and compares counts with a desktop import. Later phases: W9 investments admin, W10 settings & account (see docs/web_parity_plan.md).

## 2026-10-04 — Release v0.6.0
- Type: release
- Version / channel / tag: 0.6.0 / R (stable, main) / `v0.6.0` (annotated, notes as message)
- Commit: `b7faa45` chore(release): v0.6.0 — on top of `7e43f71`, `177fbc9` (W8 import), `3d628a1` (W7 categories/rules), `2275e11` (W6 design).
- Asset: `PROFIN.R.0.6.0-windows.zip` attached to https://github.com/pbermudezcc/PROFIN/releases/tag/v0.6.0 (local copy kept in dist/)
- Snapshot: `versions/FAMILYFIN.R.0.6.0.md/.prj`; CHANGELOG entry with the full notes.
- Validation: preflight → 454 passed; verify-notes OK; postflight OK (local) and OK `--published`.
- Note: no schema migration.

## 2026-10-05 — Session close: web parity W6–W8 shipped in v0.6.0; next goal = W9 (investments admin)
- Type: session summary
- Done this session:
  1. Plan agreed with the user: professional design refresh first (W6), then categories & rules (W7) and CaixaBank import by upload (W8); investments admin and settings/account deferred to W9/W10.
  2. W6–W8 implemented, tested in Chrome on a demo DB at desktop / iPad / iPhone widths, light and dark (see the "Web parity W6–W8" entry above).
  3. Release v0.6.0 (stable, main), tag `v0.6.0`, zip on GitHub, local copy in dist/. 454 tests.
  4. New dev tool `tools/web_preview.py`: web preview on a demo DB (localhost only, demo/demo-pass) with live CSS/JS reload, device-width frame and a demo statement for /importar.
- Not verified yet: a real CaixaBank statement uploaded from the iPad (the user will try it on v0.6.0).
- Next session: W9 — investments admin from the web. Brief with the existing Qt-free functions, what must be extracted and the risks (shared price-refresh lock with the desktop, dismissed events moving from QSettings to the DB = schema migration): `docs/web_parity_plan.md` → "W9".

## 2026-10-09 — Release v0.7.0
- Type: release
- Version / channel / tag: 0.7.0 / R (stable, main) / `v0.7.0` (annotated, notes as message)
- Commit: `d7e99bb` chore(release): v0.7.0 — on top of `07e0bf3` (feat: apply category to similar movements + create rule from the picker, desktop and web) and `7e32a36` (fix: web server survives a blocked pandas; imports router loads the ingestion pipeline lazily).
- Asset: `PROFIN.R.0.7.0-windows.zip` attached to https://github.com/pbermudezcc/PROFIN/releases/tag/v0.7.0 (local copy kept in dist/)
- Snapshot: `versions/FAMILYFIN.R.0.7.0.md/.prj`; CHANGELOG entry with the full notes.
- Validation: preflight → 458 passed; verify-notes OK; postflight OK (local) and OK `--published`.
- Note: no schema migration.
- Environment: released from the laptop on a public Wi-Fi. Windows Smart App Control (enforce mode) blocked native DLLs of pandas 3.0.5 (`_libs/parsers`) and 3.0.6 (`_libs/pandas_parser`), so the test gate could not run. pandas 3.0.3 (also 2.3.3 / 2.2.3) loads: the full suite was first run against an isolated 3.0.3 (458 passed), then the global install was moved to 3.0.3; the build bundles that same 3.0.3 binary. If imports fail again with "An Application Control policy has blocked this file", check Microsoft-Windows-CodeIntegrity/Operational and try another pandas build in an isolated `pip --target` folder before touching the global install.

## 2026-10-09 — Session close: v0.7.0 (categorize similar movements + rule from picker), pandas/Smart App Control
- Type: session summary
- Done this session (laptop, public Wi-Fi, on holidays):
  1. Diagnosed the web server not starting: Smart App Control blocks pandas 3.0.5 DLLs. Web server made resilient (`7e32a36`); see lessons_learned 2026-10-08.
  2. Feature requested by the family: in the category picker (desktop dialog and web sheet), two unticked checkboxes — "Aplicar también a N movimientos iguales" (same merchant key + same type, only uncategorized, set as manual) and "Crear una regla para los próximos" (exact-match rule on the merchant, offered only when no rule matches; applied at once to uncategorized). No extra windows (`07e0bf3`).
  3. pandas 3.0.3 found to load; full suite 458 passed; release v0.7.0 published (see the release entry above). pandas pinned to 3.0.3 in requirements.txt (`bfaf7a0`).
  4. New versioning scheme from the user, written into the release skill (`b2be0fe`): X = release version (user decides), Y = main development milestone, Z = each feature/fix. Next small change → 0.7.1.
- Not verified yet: the new checkboxes in the real running app (only tests + build); the user had to restart FAMILYFIN to load v0.7.0 code.
- Still open from before: a real CaixaBank statement uploaded from the iPad.
- Next session: W9 — investments admin from the web (`docs/web_parity_plan.md` → "W9"); first agree W9a–W9e order and scope with the user.
