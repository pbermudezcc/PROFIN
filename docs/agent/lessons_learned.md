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
