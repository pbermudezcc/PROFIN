---
name: release
description: Cut a professional GitHub release for FAMILYFIN/PROFIN — determines semantic version bump, asks whether this is a stable main release or a branch/pre-release, generates release notes from commit history, updates CHANGELOG.md and app/config.py via the existing ExportVersionHandler, tags, and publishes with `gh release create`. Use when the user asks to "release", "publish a version", "cut a release", "create a GitHub release", or "tag a new version".
---

# Release skill — FAMILYFIN / PROFIN

Produces a standard, auditable software release: semantic version bump →
changelog → annotated git tag → pushed branch/tag → published GitHub
Release. Follows the same conventions used by any professional GitHub-based
project (Semantic Versioning, Keep a Changelog style notes, annotated tags,
`gh release create`), adapted to this repo's existing tooling instead of
inventing a parallel system.

This repo already owns two pieces of the release surface — reuse them, don't
duplicate them:
- `app/config.py` — `APP_NAME` / `APP_VERSION` (and `APP_RELEASE_DATE` if
  present) are the source of truth for the app's own version string.
- `tools/ExportVersionHandler.py` — `export_version()` already knows how to
  write `APP_VERSION` back to `app/config.py`, prepend a dated entry to
  `CHANGELOG.md`, and produce the `FAMILYFIN.B.X.X.md` / `FAMILYFIN.R.X.X.md`
  snapshot + optional `.prj` archive. This skill drives that function
  directly (see Step 4) rather than re-implementing version/changelog
  writing.

This skill only handles the **git/GitHub side**: version-bump decision,
commit, tag, push, and the GitHub Release itself.

## Preconditions

1. Run `git status`. If there are uncommitted changes unrelated to the
   release, stop and ask the user whether to commit, stash, or include them
   — never silently commit unrelated work into a release commit.
2. Confirm `gh` is available: `gh --version`. If missing, tell the user the
   GitHub CLI (`gh`) needs to be installed and authenticated
   (`gh auth login`) before this skill can publish a release — do not fall
   back to guessing API calls or tokens.
3. Confirm `gh auth status` succeeds (authenticated to the right account/org
   for `pbermudezcc/PROFIN`, or whatever `git remote get-url origin` shows).
4. `git fetch --tags` so version/tag comparisons use up-to-date remote state.

## Step 1 — Main release or branch release?

Ask the user (AskUserQuestion) which this is, since it changes tagging,
prerelease flag, and target branch:

- **Main / stable release** — work has already been merged into `main` (or
  will be as part of this release), tag is a plain `vX.Y.Z`, GitHub Release
  is **not** marked prerelease, target branch is `main`.
- **Branch / pre-release** — cut from the current feature/working branch
  without merging to `main` yet (e.g. an internal beta for testing). Tag is
  `vX.Y.Z-beta.N` (increment N if that base version was already
  pre-released), GitHub Release **is** marked `--prerelease`, target is the
  current branch (`gh release create ... --target <branch>`).

If the user picked "main" but the current branch isn't `main` and isn't
merged, ask explicitly whether to merge/PR first or release the current
commit directly onto `main` — never merge or push to `main` without that
confirmation.

## Step 2 — Determine the version bump

1. Read `APP_VERSION` from `app/config.py` (current baseline).
2. Run `git log <last_tag>..HEAD --oneline` (last_tag = latest matching
   `v*` tag reachable from history, or the whole history if none) to see
   what's actually shipping.
3. Classify by Conventional Commits style prefixes where present
   (`feat:`, `fix:`, `BREAKING CHANGE`/`!:`, `chore:`, `refactor:`, etc.);
   where commits don't follow that convention, read the messages directly.
4. Propose a bump per Semantic Versioning:
   - **MAJOR** — breaking changes (data migration required, removed
     feature, incompatible config/schema change).
   - **MINOR** — new features, backward compatible.
   - **PATCH** — bug fixes, polish, no new capability.
5. State the proposed next version and a one-line reason, then ask the user
   to confirm or override it — do not silently pick a bump for a household
   finance app where an unexpected major bump (e.g. forced re-auth or DB
   migration) has real user impact.

## Step 3 — Draft the release notes

1. Group the commits from Step 2 into sections: `Features`, `Fixes`,
   `Improvements`, `Chores/Internal` (skip empty sections).
2. Cross-check against `docs/agent/agent_log.md` and, for any bug fixed,
   `docs/agent/lessons_learned.md` — these often have a clearer one-line
   description of *why* a change matters than the raw commit message.
3. Write a short human-readable summary paragraph (2-4 sentences) plus the
   grouped bullet list. This becomes both the `CHANGELOG.md` description
   (Step 4) and the GitHub Release body (Step 6) — one description, two
   destinations, so they never drift apart.
4. Show the drafted notes to the user before proceeding — this is the one
   piece of free text a human should sanity-check.

## Step 4 — Update version + CHANGELOG via the existing tool

Call `export_version()` from `tools/ExportVersionHandler.py` directly with
explicit keyword arguments (same non-interactive pattern already used in
this repo for this tool — never pipe guessed answers into its stdin
prompts):

```python
from tools.ExportVersionHandler import export_version
export_version(
    version="X.Y.Z",              # digits only, no leading "v" — matches existing FAMILYFIN.R.0.2.5 convention
    ver_type="R",                  # "R" for a main/stable release, "B" for a branch/pre-release
    also_prj=True,                 # keep parity with existing release snapshots in versions/
)
```

This call itself prompts once for a free-text description
(`_prompt_revision_description`) with no bypass parameter — monkeypatch
that single prompt function to return the Step 3 summary paragraph, same
approach already established for driving this tool non-interactively.

This updates `app/config.py` (`APP_VERSION`, `APP_RELEASE_DATE`),
prepends the dated entry to `CHANGELOG.md`, and writes the
`versions/FAMILYFIN.{B,R}.X.Y.Z.{md,prj}` snapshot.

## Step 5 — Commit and tag

1. `git add app/config.py CHANGELOG.md versions/FAMILYFIN.*` (name the files
   explicitly — never a bare `git add -A`/`.`).
2. Commit: `chore(release): vX.Y.Z` with the Step 3 summary as the body,
   ending with the attribution lines this session's system reminder
   specifies for commits.
3. Create an **annotated** tag so the release has a real message, not a
   lightweight pointer:
   `git tag -a vX.Y.Z -m "<Step 3 summary + bullet list>"`
   (append `-beta.N` per Step 1 for a branch/pre-release).

## Step 6 — Push and publish (confirm first)

Pushing a tag/branch and creating a public GitHub Release are both visible,
hard-to-reverse actions — confirm with the user before this step even if
they approved the plan earlier, unless they've already explicitly said to
proceed through publish.

1. `git push origin <branch>` then `git push origin vX.Y.Z`.
2. `gh release create vX.Y.Z --title "FAMILYFIN vX.Y.Z" --notes "<Step 3 notes>" [--prerelease] [--target <branch-if-not-default>]`.
3. If a Windows build is expected for this release (see
   `build_profin.spec` / `tools/build_windows.py`), ask whether to attach
   the built artifact to the release (`gh release upload vX.Y.Z <path>`) —
   do not build or upload it silently.

## Step 7 — Log it

Append one entry to `docs/agent/agent_log.md` (type: `release`) recording:
version, release type (main/pre-release), tag, files touched, and a link
note that the GitHub Release + CHANGELOG.md entry are the durable record —
this mirrors the existing agent_log.md convention already used for every
other work session in this repo.

## Guardrails

- Never force-push a tag or branch as part of a release.
- Never skip the Step 3 human review of release notes.
- Never release from a dirty working tree (uncommitted, unrelated changes).
- Never invent a version number without showing the reasoning in Step 2.
- If any step fails (e.g. `gh release create` errors), stop and report
  exactly what succeeded so far (commit? tag? push?) so the user isn't left
  guessing what state the repo/remote is in.
