# choobi Style Guide — Template

> Copy this file to `choobi/style.md` at the root of your repo (and, in a monorepo,
> to `<package>/choobi/style.md` for per-team overrides). Delete the guidance blockquotes
> like this one before committing.
>
> **Precedence:** this repo's style guide is the **northstar**. choobi applies it first and
> falls back to its own built-in baseline for anything you leave unspecified. A small repo
> with no `choobi/style.md` runs on choobi's baseline alone — you only need this file when you
> want to override or extend the defaults.
>
> This guide has **two halves**, and the split matters:
> - **Rules** → machine-checkable, enforced deterministically (no LLM judgment). Keep these
>   in `choobi/rules.yaml`; this file just documents them.
> - **Guidance** → prose that shapes choobi's writing (voice, what's worth documenting). choobi
>   follows it with judgment.

---

## 1. Scope & precedence

- **Applies to:** <!-- e.g. the whole repo / packages/api / the platform team's docs -->
- **Overrides:** this guide overrides choobi's baseline. A more specific package-level guide
  (`<package>/choobi/style.md`) overrides this one (nearest-guide-wins).
- **Last reviewed:** <!-- YYYY-MM-DD -->

## 2. Owners

> Who reviews doc + style-guide changes for this scope. Route choobi's proposed changes here.
> Keep this in sync with `CODEOWNERS` where possible.

| Area / path            | Owner(s)             |
| ---------------------- | -------------------- |
| <!-- docs/public/api --> | <!-- @team-api -->  |
| <!-- this style guide --> | <!-- @docs-guild --> |

## 3. Enforceable rules (deterministic)

> These are checked mechanically. Document them here; encode them in `choobi/rules.yaml`.
> Use them for anything with a right/wrong answer — structure, required sections, formatting.

- **Required sections** per doc type:
  - Feature docs must contain: <!-- Summary, Usage, Owner, Related code -->
  - Reference docs must contain: <!-- Signature, Params, Returns, Example -->
- **Headings:** <!-- sentence case; single H1; no skipped levels -->
- **Links:** <!-- relative links within the repo; no bare URLs in prose -->
- **Code blocks:** <!-- must specify a language; examples must be runnable/verifiable -->
- **File naming:** <!-- kebab-case.md -->
- **`covers:` anchor required:** every doc declares the code it documents in front-matter
  (this feeds the generated SSOT at `choobi/index.md`). Example:
  ```yaml
  ---
  covers: [src/auth/**, src/api/routes.ts]
  ---
  ```

## 4. Voice & tone (guidance)

> Prose direction. choobi applies judgment here — no linter enforces it.

- **Audience:** <!-- external users / internal engineers / both, per section -->
- **Voice:** <!-- direct, active, second person; no marketing language -->
- **Tense & mood:** <!-- present tense; imperative for instructions -->
- **Terminology:** <!-- preferred terms and banned synonyms; e.g. "workspace" not "project" -->

## 5. What to document — and when to stay silent (guidance)

> This is the most important section. choobi's default failure mode is over-writing.

- **Do document:** <!-- new public APIs, new features, breaking changes, architecture decisions -->
- **Do NOT document:** <!-- internal refactors with no behavior change, renamed locals,
  formatting-only diffs, experimental code behind a flag -->
- **Public vs internal:** choobi never spawns public-facing docs (exception: the top-level
  `README.md` in a small, single-package repo). New feature/API docs go to `docs/internal/`.

## 6. Doc types & where they live

> Map your doc types to folders so choobi files new docs correctly. Public docs follow
> Diátaxis (tutorials / how-to / reference / explanation).

| Type          | Location                  | choobi may spawn? |
| ------------- | ------------------------- | ---------------- |
| Tutorial      | `docs/public/tutorials/`  | No (update only) |
| How-to        | `docs/public/how-to/`     | No (update only) |
| Reference     | `docs/public/reference/`  | No (update only) |
| Explanation   | `docs/public/explanation/`| No (update only) |
| Feature       | `docs/internal/features/` | Yes              |
| Architecture  | `docs/internal/architecture/` | Yes          |
| Debugging     | `docs/internal/debugging/`| Yes              |

## 7. Changelog / audit

- choobi appends one line per doc change to `docs/internal/CHANGELOG.md`
  (what changed, why, triggering PR/commit). Keep the doc bodies clean — no inline
  "updated on…" notes; the ledger is the history.

---

<!--
NOTE ON LEARNED CONVENTIONS:
When choobi notices a recurring convention not captured here, it records it in its own
memory (the git-ignored .choobi/ cache) — it does NOT open a PR against this file.
Promoting a convention into this shared guide is a deliberate human edit.
-->
