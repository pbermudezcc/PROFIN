---
name: release
description: Cut a complete, verified FAMILYFIN/PROFIN release — runs the full test suite as a gate, commits pending work, decides the semantic version, drafts release notes from every commit/changed area/schema migration since the last tag, writes them into CHANGELOG.md and app/config.py via ExportVersionHandler, builds and zips the Windows app, tags, pushes, publishes the GitHub Release with the same notes and the zip attached, and verifies everything afterwards. Use when the user asks to "release", "publish a version", "cut a release", "create a GitHub release", "hacer un release", "publicar versión" or "tag a new version".
---

# Release skill — FAMILYFIN / PROFIN

One release = one set of notes in three places that must agree:
`CHANGELOG.md` entry, annotated git tag message, GitHub Release body —
plus the Windows build attached as a zip. Every mechanical check lives in
`tools/release_tools.py` (tested in `tests/test_release_tooling.py`); this
skill decides, writes the prose, asks the user, and runs the commands.

Paths used below (`dist/` is gitignored, so drafts never get committed):
- notes file: `dist/release-notes-vX.Y.Z.md`
- build folder: `dist/PROFIN.<R|B>.X.Y.Z/` (exe + `_internal/`)
- release asset: `dist/PROFIN.<R|B>.X.Y.Z-windows.zip`

Channel letter: `R` = stable release from `main`; `B` = pre-release
(`X.Y.Z-beta.N`) from the current branch.

Run every command from the repo root. Stop at the first failure and report
exactly what already happened (commit? tag? push? release?) — see
"Recovery" at the end.

---

## Step 0 — Bring the working tree to a releasable state

1. `git status`. Pending work is normal (features are often left
   uncommitted until release). For each group of related changes, propose
   a Conventional Commit (`feat(scope): …`, `fix(scope): …`,
   `refactor/perf/test/docs/chore(scope): …`) with an explicit file list,
   show the plan, and commit only after the user agrees. Good commit
   subjects matter: they become the release notes' raw material.
   - Never stage with `git add -A` / `git add .`.
   - Never commit `.env`, `*.db`, `*.zip`, `data/`, `logs/`, `build/`,
     `dist/` (the `.gitignore` covers them; `preflight` double-checks).
   - Files the user doesn't want in this release: ask whether to stash them.
2. Commit messages end with the attribution lines required by this
   session's system reminder.

## Step 1 — Main release or branch pre-release?

Ask (AskUserQuestion):
- **Main / stable** — tag `vX.Y.Z`, not prerelease, from `main` in sync with
  `origin/main`. If the work is on another branch, ask whether to merge/PR
  first — never merge or push to `main` without that confirmation.
- **Branch / pre-release** — tag `vX.Y.Z-beta.N` (next free N), GitHub
  `--prerelease`, `--target <branch>`.

## Step 2 — Gate: tests and preflight

```
python tools/release_tools.py preflight --version <X.Y.Z> --channel <R|B>
```
(Use a provisional version if Step 3 hasn't fixed it yet, and rerun once
it is final.) It runs the **whole pytest suite offscreen** and checks:
semver and greater than `APP_VERSION`; `CHANGELOG.md` top entry matches
`APP_VERSION`; clean working tree; no forbidden tracked files; tag free
locally and on GitHub; branch is `main` for R and not behind origin; `gh`
authenticated. **Any problem stops the release** — fix it (or ask), never
skip it. Do not pass `--skip-tests` for a real release.

## Step 3 — Decide the version

```
python tools/release_tools.py draft --version <provisional> --out dist/release-notes-v<provisional>.md
```
Read the draft's facts (commits since the last tag, changed areas, schema
`vA → vB`, agent-log titles) and propose per SemVer:
- **MAJOR** — removes a feature, requires manual action, or data could not
  migrate automatically.
- **MINOR** — new features, **or any schema migration** (the family's DB is
  upgraded on first start; they must know).
- **PATCH** — fixes/polish only, no migration.

State the version and a one-line reason; the user confirms or overrides.
If it changed, regenerate the draft with the final version.

## Step 4 — Write the notes (Spanish, for the family)

Edit `dist/release-notes-vX.Y.Z.md`:
1. `### Resumen`: 2–4 sentences, plain Spanish, what changes for someone
   who installs this version. This paragraph becomes the CHANGELOG
   "Description".
2. Rewrite every `TODO:` bullet as a user-facing sentence; keep the
   `` (`sha`) `` reference at the end of each. Merge or split bullets as
   needed, but **every commit sha must remain somewhere** in the notes.
   Use `docs/agent/agent_log.md` / `lessons_learned.md` (titles listed in
   the draft's "Fuentes" comment) for the *why* of each change.
3. Keep `### Antes de actualizar` (schema migration warning) verbatim when
   present, and keep `### Áreas afectadas` and the `<details>` commit list.
4. Delete both HTML comments. No bare `---` lines.

Then:
```
python tools/release_tools.py verify-notes dist/release-notes-vX.Y.Z.md --version X.Y.Z
```
must print `Notas OK.` (it rejects TODO/BORRADOR/comments, a missing
commit, a missing changed area, a missing migration warning, a too-short
summary). Finally **show the notes to the user and wait for approval** —
this is the one piece of prose a human must sanity-check.

## Step 5 — Version, CHANGELOG and snapshot

```
python - <<'EOF'
from pathlib import Path
import tools.release_tools as rt
from tools.ExportVersionHandler import export_version
summary, body = rt.split_notes(Path("dist/release-notes-vX.Y.Z.md").read_text(encoding="utf-8"))
export_version(version="X.Y.Z", ver_type="R", also_prj=True, description=summary, notes=body)
EOF
```
(`ver_type="B"` for a pre-release.) No prompts are involved. This writes
`APP_VERSION` + `APP_RELEASE_DATE` in `app/config.py`, prepends the entry
(summary + full notes body) to `CHANGELOG.md`, and creates
`versions/FAMILYFIN.<R|B>.X.Y.Z.md/.prj`. Run
`python -m pytest -q tests/test_release_tooling.py` afterwards (it checks
`APP_VERSION` against the new CHANGELOG entry).

## Step 6 — Build and package the Windows app

```
python tools/build_windows.py --channel release     # or: --channel beta
python tools/release_tools.py package --version X.Y.Z --channel R
```
The build is a one-dir PyInstaller app: the `.exe` does **not** run
without `_internal/`, so only the zip is ever published. `package` refuses
an incomplete build. Do not launch the built exe against the family's data
to "test" it — it would migrate their real database.

## Step 7 — Commit, tag, verify locally

1. `git add app/config.py CHANGELOG.md versions/FAMILYFIN.<R|B>.X.Y.Z.md versions/FAMILYFIN.<R|B>.X.Y.Z.prj`
2. `git commit -m "chore(release): vX.Y.Z" -m "<Resumen paragraph>" -m "<attribution lines>"`
3. `git tag -a vX.Y.Z --cleanup=verbatim -F dist/release-notes-vX.Y.Z.md`
   (annotated; `verbatim` keeps the `### ` headings, which git's default
   `strip` cleanup would delete as comments)
4. ```
   python tools/release_tools.py postflight --version X.Y.Z --channel R --notes dist/release-notes-vX.Y.Z.md
   ```
   must print `Postflight OK.` (config version/date, snapshot files,
   CHANGELOG top entry contains the summary **and** the full notes body,
   annotated tag on HEAD whose message is the notes, zip present).

## Step 8 — Push and publish (confirm first)

Pushing and publishing are public and hard to reverse: summarise what will
happen (branch, tag, release title, prerelease flag, asset) and get an
explicit yes, even if the plan was approved earlier.

```
git push origin <branch>
git push origin vX.Y.Z
gh release create vX.Y.Z --verify-tag --title "FAMILYFIN vX.Y.Z" --notes-file dist/release-notes-vX.Y.Z.md [--prerelease --target <branch>] dist/PROFIN.<R|B>.X.Y.Z-windows.zip
python tools/release_tools.py postflight --version X.Y.Z --channel R --notes dist/release-notes-vX.Y.Z.md --published
```
The last command must print `Postflight OK.`: the GitHub Release exists,
is not a draft, has the right prerelease flag, its body equals the notes
file, and the zip is attached. Give the user the release URL
(`gh release view vX.Y.Z --json url --jq .url`).

## Step 9 — Log it

Append a `release` entry to `docs/agent/agent_log.md`: version, channel,
tag, commit, asset name, test count from preflight, postflight result.
Commit it as `docs(agent_log): record vX.Y.Z release` and push it (ask
first, same as Step 8).

---

## Recovery (nothing is ever force-pushed)

| Failed at | State | Fix (confirm with the user first) |
|---|---|---|
| Step 2–4 | nothing changed | fix the problem, rerun |
| Step 5–6 | files modified, no commit | `git restore app/config.py CHANGELOG.md` + delete the new `versions/` files, rerun |
| Step 7 postflight | local commit + tag | `git tag -d vX.Y.Z`, `git reset --soft HEAD~1`, fix, redo Step 7 |
| Step 8 push/publish | branch/tag pushed | never delete a pushed tag; fix forward: `gh release edit vX.Y.Z --notes-file …` / `gh release upload vX.Y.Z <zip> --clobber`, rerun postflight `--published` |

## Guardrails

- Never skip the test gate, `verify-notes`, the user's review of the notes,
  or the confirmation before Step 8.
- Never force-push, never move or delete a pushed tag.
- Never publish the bare `.exe`; always the zip from `package`.
- One notes file feeds CHANGELOG, tag and GitHub — never retype them.
- Never invent a version without the Step 3 reasoning.
