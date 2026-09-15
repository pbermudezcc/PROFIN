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

## 2026-09-16 — Agent guardrails + release v0.2.5
- Type: refactor
- Files touched: .claude/agents/familyfin-engineer.md, .github/agents/familyfin-developer.agent.md, app/config.py (via tools/ExportVersionHandler.py), CHANGELOG.md (via tool), versions/FAMILYFIN.R.0.2.5.md, versions/FAMILYFIN.R.0.2.5.prj, this file.
- Summary: Added four guardrail rules to both agent definitions, distilled from this session's two lessons: (1) never let a passive Qt event trigger an overwriting pass, (2) append new fallback/legacy candidates at the lowest priority in a search list, never the front, (3) an identity rename that is also a storage key needs an explicit one-time migration plus a test, (4) drive interactive CLI tools non-interactively via direct function calls/monkeypatching rather than guessed stdin. Ran `tools/ExportVersionHandler.py`'s `export_version()` directly (monkeypatching only the free-text description prompt, per guardrail 4) to cut release v0.2.5: bumped `app/config.py` APP_VERSION to 0.2.5, wrote `versions/FAMILYFIN.R.0.2.5.md/.prj`, and prepended the CHANGELOG.md entry describing the FAMILYFIN rebrand and the rules-engine overwrite fix.
- Validation: full `pytest` suite green (51 passed) both before and after the version bump.
- Follow-up: commit and push v0.2.5 to GitHub with a descriptive commit message; Windows build (`build_profin.bat`) was not run as part of this release per no explicit request to package a new binary.
