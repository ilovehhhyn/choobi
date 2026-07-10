# roobi — Build Plan

> Status: design consolidated from the founding design conversation. This document is the
> single reference for what roobi is, why, and how it is built. Items are marked
> **[Decided]**, **[Proposed]** (a concrete suggestion not yet ratified), or **[Open]**
> (a decision still to make). Nothing here is invented beyond what was discussed; proposals
> are labelled as such.

---

## 1. Product description

**roobi** is a tool that automatically keeps engineering documentation up to date — and
writes new docs — as engineers work: writing features, changing architecture, and debugging.
It lives seamlessly alongside an engineer across every repository they touch, so that the
engineer never has to think about updating docs. The goal is that **every repo a roobi user
touches has continuously accurate docs**, and that an entire engineering team can adopt roobi
to maintain the docs of a large monorepo without manual upkeep.

roobi both **updates** existing docs and **writes** new ones:
- A new API or feature → roobi authors a new doc.
- A change or a debugging fix that alters behavior → roobi updates the affected doc.
- It maintains a clean, current public doc surface *and* an internal audit trail of how docs
  have changed over time.

## 2. Rationale — the problem and why this shape

Docs rot because keeping them current is manual, easy to forget, and disconnected from the
moment code changes. The **core hard problem** roobi must solve is:

> Given a code change, **which docs are affected, and how?**

This is a code→doc dependency-mapping problem. Every trigger and integration below is easy;
this mapping (plus knowing when to *stay quiet*) is what makes roobi good or useless. It stays
central to the whole design.

Two properties fall out of the goal and drive most decisions:
- **"Every repo I touch"** → roobi must follow the *user*, not be adopted per-repo, and must
  fire regardless of which coding tool (or none) produced the change.
- **"A whole team on a monorepo"** → shared, versioned, per-repo knowledge that all
  teammates' roobis agree on, without central write bottlenecks.

## 3. Key decisions (with reasoning)

- **[Decided] roobi is a standalone agent** — not a cron job, and not a subagent embedded in
  a coding tool.
  - *Cron rejected:* docs go stale on code changes, not on a clock; a timer is disconnected
    from the work and misses the moment that matters (right before code is shared). Only
    viable as an optional secondary sweep.
  - *Embedded subagent rejected as the final form:* an agent definition (prompt + tools +
    trigger) is tied to its harness. A Claude Code subagent cannot be invoked from Codex and
    vice-versa, so an embedded agent only fires when you used that specific tool. It is the
    right **prototype** (fastest to stand up) but not the product.
  - *Standalone chosen:* roobi is its own program, installed once, invoked by triggers that
    sit **below** the coding tool (at git), so it works no matter how the code was written.

- **[Decided] Global install, per-repo memory.** roobi is installed once and follows the user
  across every repo (lives in their terminal). Its *identity/logic* is global; its
  *knowledge* (where docs live, house style, state) is per-repo.

- **[Decided] Runtime is pluggable: Claude Code, the Codex app, or the Codex CLI.** roobi
  itself is the orchestration + git integration + memory + linkage. The actual doc-writing
  LLM work is delegated to whichever coding-agent runtime the user chooses, which carries its
  own authentication. This is *why* roobi must be standalone: it sits above these runtimes
  rather than inside any one of them.

- **[Decided] No blocking CI.** Rejected as too blocking for a team. Replaced by a
  **non-blocking** team signal (see §5).

- **[Decided] Default to proposing reviewable diffs, not silent auto-commits.** Docs are
  writing; silent commits erode trust and get roobi disabled. Auto-commit is an opt-in,
  per-doc, once trust is established.

## 4. Architecture overview

```
        ┌──────────────────────────── roobi (standalone, global) ───────────────────────────┐
        │                                                                                    │
 triggers ──▶  ENGINE:  linkage ─▶ relevance gate ─▶ surgical edit ─▶ verify ─▶ review     │
   §5          §8                                                                            │
        │        │                │                              │                           │
        │        ▼                ▼                              ▼                           │
        │   MEMORY (§9):   in-repo anchors + generated index   RUNTIME ADAPTER (§3):         │
        │   git-ignored .roobi/ cache   global ~/.roobi/ prefs   Claude Code / Codex app/CLI │
        └────────────────────────────────────────────────────────────────────────────────────┘
```

roobi orchestrates; a pluggable coding-agent runtime does the prose generation; git is the
constant trigger layer; memory is split by owner (§9).

## 5. Triggers — how roobi is woken up

All triggers invoke the same engine.

- **[Decided] Explicit / slash command** — e.g. `roobi write` or `/roobi`. Invoked on demand
  to write docs a particular way. Also the channel through which the user *teaches* roobi
  style (lessons are saved to memory, §9).
- **[Decided] Automatic: git `pre-push` hook.** Before a push, roobi checks whether the code
  changes touched anything the docs cover; if a doc is stale, it reconciles.
  - `pre-push` chosen over `pre-commit`: it batches a whole session and only fires when the
    engineer is about to *share*, so it does not nag on every commit.
  - **[Decided] UX caveat — do not silently rewrite and push.** Default flow: pre-push detects
    a stale doc → roobi drafts the update → **stops the push** with a message
    (e.g. "roobi: docs outdated — I've drafted updates to `api/README.md`; review and commit")
    → the engineer reviews, commits, and re-pushes. Fully-silent auto-commit is opt-in later.
- **[Decided] Non-blocking PR bot (team signal).** Replaces blocking CI. On a pull request it
  comments or opens a draft doc-update PR — never blocks merge. Also covers the local-hook gap
  (anyone can `git push --no-verify`).
  - **[Decided] Non-roobi authors.** When roobi sees a PR from someone not running roobi and
    judges a doc stale, it reconstructs intent from the **diff + commit messages + PR
    description**, drafts the doc update, and leaves a comment. Because commit-history intent
    is a weaker signal than a live session, for non-roobi PRs the default is to **propose** a
    draft, not auto-commit to the author's branch. Auto-fixing for them is the opt-in.

## 6. What roobi writes vs. updates, and public vs. internal

- **[Decided] roobi updates existing docs** when covered code changes.
- **[Decided] roobi writes new docs** when a new API/feature appears, and updates internal
  docs on debugging/behavior changes. roobi-spawned docs are for **internal tracking**.
- **[Decided] roobi never spawns public-facing docs** — with one exception: the top-level
  `README.md` of a **small** (single-package) repo, not the monorepo. It may always *update*
  existing public docs, but never invent them.

## 7. Output model — clean public surface + internal changelog

Every roobi write produces two things:
- **[Decided] The doc itself** — always the clean, current state. No inline "updated on…"
  cruft. Public docs read as though a human kept them pristine.
- **[Decided] A changelog ledger append** — one line per doc change (what changed, why, and
  the triggering PR/commit) in `docs/internal/CHANGELOG.md`. This is roobi's human-skimmable
  audit trail, separate from `git log`, answering "how have our docs drifted lately."

## 8. The engine — how roobi decides

1. **[Decided] Doc↔code linkage (the moat).**
   - **Source of truth = distributed `covers:` front-matter anchors** in each doc, e.g.
     `covers: [src/auth/**, src/api/routes.ts]`. Chosen over a central registry file, which
     in a monorepo becomes a merge-conflict magnet and a write bottleneck (every new doc
     touches it). Distributed anchors keep edits local, merge cleanly, and scale.
   - **SSOT = a *generated* index** (`roobi/index.md`) built by crawling the anchors,
     deterministically sorted so it merges trivially. Conceptually one place to answer "where
     do all docs live and what do they cover"; physically distributed.
   - Cheap structural hints supplement anchors (e.g. "a README owns its directory subtree").
   - **[Proposed] Semantic retrieval (embeddings)** as a *supplement* for discovery, never the
     source of truth.
2. **[Decided] Change → doc-delta reasoning (relevance gate).** After the mechanical staleness
   trigger, an LLM decides whether the change actually invalidates anything in each affected
   doc. Most diffs touch nothing doc-worthy; **staying quiet is roobi's most important skill**
   and is gated hard before any write.
3. **[Decided] Surgical edits + verification.** roobi proposes minimal patches, not rewrites,
   then runs a verify step: referenced paths/commands/signatures must still exist / compile /
   match. This is what separates a trustable roobi from a confidently-wrong doc bot.
4. **[Decided] Review surface.** Output is a reviewable diff by default (§3), routed to owners
   (§10, CODEOWNERS).

## 9. Memory model — three tiers by owner

1. **[Decided] Linkage + team conventions — in-repo, committed.** The `covers:` anchors (truth)
   and the generated index. Shared with teammates, versioned with code, travels through
   merges, and survives cache wipes.
2. **[Decided] Working cache — in-repo but git-ignored (`.roobi/`).** Embeddings, last-seen
   commit hash, "changed since last run" state, and **learned conventions** (see §11).
   Rebuildable, disposable, private to the checkout — a cache over the committed anchors, not
   the truth.
3. **[Decided] Personal preferences — global (`~/.roobi/`).** The user's house style, tone,
   verbosity. Follows the user across repos and stays out of other people's repos. Lessons
   taught via the slash command are saved here (or into the repo's `roobi/` config if the
   convention is repo-wide).

## 10. Collision / reconciliation behavior

- **[Decided]** Because the linkage map is *shared* (in-repo) and roobi runs on the **merged**
  state at pre-push, if a teammate's code changed something a doc covers, roobi sees it and
  reconciles the doc — even though the current user didn't write that code. This is the
  emergent benefit of a shared map.
- **[Decided] Honest limit:** roobi is **not** a merge-conflict resolver. It reflects both
  people's changes in the shared docs because they share one map; genuinely conflicting claims
  still surface as a normal git/doc conflict for a human to resolve.
- **[Decided]** Proposed doc and style changes are routed to area owners via `CODEOWNERS`.

## 11. Style-guide system

- **[Decided] roobi ships a built-in baseline style guide** — constant across every install,
  the "just good" defaults. Lives in this repo (`baseline/style.md`, currently minimal by
  design; `baseline/rules.yaml` holds the enforceable defaults).
- **[Decided] The repo's `roobi/style.md` is the northstar** (overrides); roobi falls back to
  its baseline for anything unspecified. A small repo with no style guide runs on the baseline
  alone.
- **[Decided] Two halves:**
  - **Enforceable rules** (`roobi/rules.yaml`) — machine-checkable, applied deterministically
    with no LLM judgment (required sections, headings, links, file naming, spawn policy,
    staleness, output behavior).
  - **Prose guidance** (`roobi/style.md`) — voice, tone, what's worth documenting, when to
    stay silent — applied by the agent with judgment.
  This split makes "respect the style guide" *guaranteed* for the mechanical half and *advised*
  for the taste half.
- **[Decided] Hierarchical scoping for monorepos.** A repo-root guide plus per-package
  overrides (`<package>/roobi/style.md` and `rules.yaml`), resolved nearest-wins, so teams get
  autonomy without forking roobi.
- **[Decided] Learned conventions → own memory, no PR.** When roobi notices a recurring
  convention not captured in the guide, it records it in its own memory (the git-ignored
  `.roobi/` cache) rather than opening a PR (too much overhead). *Accepted tradeoff:* learned
  conventions stay per-agent and do not auto-propagate to teammates; promoting one into the
  shared guide is a deliberate human edit.
- **[Decided] Template shipped** at `templates/roobi-style-guide.template.md` — the copy-in
  starting point for a repo's own guide (precedence, scope, owners, rules-vs-guidance split,
  doc-type placement, changelog).

## 12. Docs folder structure

Grounded in research into how strong, high-star repos organize docs:
- **Diátaxis** is the authoritative model for *public* docs: **tutorials / how-to / reference /
  explanation** (learning / task / facts / understanding).
- **AWS CDK** keeps its `docs/` almost entirely *internal* (design guidelines, implementation
  notes, release process); its public API reference is generated and lives elsewhere.
- **PEFT** (which houses LoRA) splits `guides/` (how-to) · `package_reference/` (reference) ·
  conceptual guides (explanation) · `developer_guides/` (internal/contributor).

This validates roobi's **public/internal split**. The layout roobi manages:

**Big monorepo:**
```
docs/
  public/                 # human-authored, Diátaxis; roobi UPDATES, never spawns
    tutorials/            #   learning
    how-to/               #   task guides
    reference/            #   API facts
    explanation/          #   concepts / architecture rationale
  internal/               # roobi-OWNED; spawns freely
    features/             #   one doc per feature/API roobi creates
    architecture/         #   design/architecture notes as systems evolve
    debugging/            #   notable fixes & behavior changes
    CHANGELOG.md          #   the continuous doc-update ledger (§7)
  roobi/                  # config + SSOT
    style.md              #   repo style guide (northstar; prose)
    rules.yaml            #   enforceable rules (deterministic)
    index.md              #   GENERATED SSOT of where docs live (from covers: anchors)
```
Per-package overrides live at `<package>/roobi/style.md` and `<package>/roobi/rules.yaml`.

**Small repo (collapsed):**
```
README.md                 # roobi may spawn/maintain (only public doc it creates)
docs/internal/{features,architecture,CHANGELOG.md}
# no roobi/ config needed -> roobi falls back to its own baseline
```

Sources:
- Diátaxis — https://diataxis.fr/
- AWS CDK docs — https://github.com/aws/aws-cdk/tree/main/docs
- PEFT docs — https://github.com/huggingface/peft/tree/main/docs/source

## 13. Repository scaffold

### 13a. roobi's own source repo (this repo) — [Proposed]

```
roobi/                              # this repo — roobi's source
  bin/
    roobi                           # standalone CLI entrypoint
  src/
    triggers/                       # pre-push hook logic, PR bot, slash command
    engine/
      linkage.*                     # covers: anchor resolution
      relevance.*                   # the "should I touch this doc" gate
      patch.*                       # surgical edit generation
      verify.*                      # paths/commands/signatures still valid
    runtimes/                       # pluggable adapters (§3)
      claude-code.*
      codex-app.*
      codex-cli.*
    index/                          # crawl anchors -> generate roobi/index.md (SSOT)
    memory/                         # repo .roobi/ cache + global ~/.roobi/ prefs
  baseline/
    style.md                        # built-in baseline prose guide  [exists, minimal]
    rules.yaml                      # built-in baseline enforceable rules  [exists]
  templates/
    roobi-style-guide.template.md   # user-facing style-guide template  [exists]
  hooks/
    pre-push                        # installable git hook
  docs/                             # roobi's own docs
  build-plan.md                     # this file
  README.md
```
(Currently present: `baseline/style.md`, `baseline/rules.yaml`,
`templates/roobi-style-guide.template.md`, `build-plan.md`.)

### 13b. What roobi installs into a target repo — [Proposed]

```
<target-repo>/
  roobi/
    style.md            # copied from template; the repo's northstar (optional)
    rules.yaml          # overrides/extends baseline (optional)
    index.md            # GENERATED SSOT
  .roobi/               # git-ignored working cache
  docs/public/...       # per §12
  docs/internal/...
  .git/hooks/pre-push   # installed hook
```

## 14. Backend implementation — git/PR access, CLI presence, and auth

This is the critical-path engineering: how roobi actually observes changes, how it lives in
the terminal, and how a user authenticates. Grounded in how the closest existing products
work — **Mintlify** (self-updating docs: a GitHub App watches repos, "automations" run on
push/schedule, the agent clones the repo as context and opens a PR; hybrid local-CLI +
cloud-LLM architecture) and **CodeRabbit** (a GitHub App for PR reviews plus a local CLI that
drives Claude Code / Codex for pre-commit review).

### 14a. Accessing git events and PRs — two surfaces, not one

**Critical insight: local events and remote events are different problems, and roobi needs
both.** A local hook is timely but unreliable; a server-side integration is reliable but not
local. Use each for what it's good at.

**Surface A — local git events (pre-push / staged changes).** Powers the "before you push"
draft flow (§5).

| Option | How | Pros | Cons |
|---|---|---|---|
| Managed `core.hooksPath` (versioned hooks dir), installed by `roobi init` | `roobi init` points git at a committed hooks dir | No runtime dependency; versioned; roobi controls install | `.git/hooks` isn't cloned → needs a per-clone install step; can conflict with an existing hook manager that sets the same key |
| Hook manager (Husky / lefthook) | Piggyback on the repo's existing manager | Reuses tooling teams already run; lefthook is a single Go binary, no runtime dep | Adds/depends on a dependency; Husky needs Node + npm `prepare` (supply-chain surface) |
| Global `core.hooksPath` (user-level) | One hook dir for all the user's repos | True "every repo I touch" with one setup; matches the global-install model | Global override can clash with per-repo managers; still bypassable |

Limitations shared by **all** local hooks: bypassable with `git push --no-verify`; fragile at
the intersection of shell env + repo config + wrapper tooling (a change in any of the three
silently breaks the chain, with no error); require a per-clone (or per-user) install step.
Conclusion: local hooks give **timeliness**, never **enforcement** — they are the fast draft,
not the guarantee.

**Surface B — remote git events (PRs, pushes to the remote).** Powers the team signal and the
non-roobi-author path (§5).

| Option | How | Pros | Cons |
|---|---|---|---|
| **GitHub App** (recommended) | Installed on org/repos; receives centralized webhooks; mints short-lived per-installation tokens | Independent bot identity; centralized webhooks across all repos; installation tokens (no per-user secret to store); scales (~15k req/hr/installation); fine-grained perms; what Mintlify & CodeRabbit use | Requires a hosted webhook receiver + token-minting service (unavoidable cloud); org admin installs once |
| OAuth App | Acts on behalf of a user | Simple user login | Acts *as* a user; webhooks configured per-repo; lower, non-scaling rate limits |
| PAT + polling | Store a PAT, poll the API | Trivial to prototype | User-tied; doesn't scale to a team; polling is laggy/wasteful; token-storage burden |
| GitHub Actions workflow | Commit a workflow that runs roobi on PR events | No server to host; runs with `GITHUB_TOKEN` | Per-repo committed workflow; consumes CI minutes; CI-ish (we rejected *blocking* CI, but a non-blocking Action is a viable low-infra fallback) |

Conclusion: the **GitHub App is the reliable backstop** that closes the `--no-verify` gap and
handles non-roobi authors. It is also the only piece that requires roobi to run a small hosted
service (webhook receiver + installation-token minting) — **be honest that this cloud
dependency is required for reliable team coverage.** A non-blocking GitHub Actions workflow is
the acceptable no-server fallback for a single team that won't host anything.

### 14b. Living seamlessly in the CLI

- **[Proposed] Distribution: a single static binary (Go/Rust) via Homebrew + a `curl | sh`
  installer.** Like lefthook, a dependency-free binary is the most reliable across machines and
  avoids the Node/npm requirement and `prepare`-script supply-chain surface that Husky/npm
  distribution carries. (npm/npx remains an option for JS-first teams.)
- **[Decided] Invocation: on-demand + hook-triggered, no daemon.** `roobi <cmd>` for explicit
  use; the git hook invokes the same binary on pre-push. A filesystem-watching background
  daemon stays **rejected** (noisy, expensive — founding decision).
- **[Decided] Thin orchestrator, not another heavy agent.** roobi owns git integration,
  linkage, memory, and the index; it **delegates prose generation to the user's existing
  coding-agent runtime** (Claude Code / Codex app / Codex CLI), as CodeRabbit's CLI drives
  Claude Code/Codex. The binary stays small and ships/manages no model itself.
- **[Decided, reliability guardrail] The hook must never hard-block on roobi.** If roobi is
  slow, offline, or errors, the pre-push hook times out and warns — it never hangs or fails the
  push. The GitHub App backstop catches anything the local path missed, so degrading gracefully
  locally costs no coverage.

### 14c. Auth process after clone / install

**Critical separation: there are two independent auths, and roobi should own as little of it
as possible.**

1. **LLM runtime auth — reuse, don't reinvent.** roobi delegates generation to Claude Code /
   Codex, which already carry their own auth (device flow or an API key the user set up). roobi
   detects an authenticated runtime and uses it; it never handles model API keys itself. A UX
   and security win.
2. **GitHub auth — split by surface:**
   - **Local CLI → OAuth device flow + system keychain (the `gh` pattern).** For local API
     calls (e.g. opening a draft PR from the terminal), roobi prints a one-time code, the user
     approves in the browser (credentials never touch the CLI), and the token is stored in the
     OS keychain (encrypted-file fallback). Where possible, **reuse the user's existing `gh`
     auth** rather than minting a new token.
   - **Team → one-time GitHub App install by an org admin.** Installation tokens are minted
     server-side per event; nothing per-user to store. One install, whole team benefits.

**Onboarding flow (recommended):**
```
1. brew install roobi                # single binary, no runtime deps
2. cd my-repo && roobi init          # installs managed git hook (core.hooksPath),
                                      # scaffolds roobi/ config + .roobi/ cache (+ .gitignore),
                                      # generates roobi/index.md from covers: anchors
3. roobi detects Claude Code / Codex runtime + its existing auth
   (prompts to pick/authenticate only if none found)
4. (team, once) an org admin installs the roobi GitHub App on the org/repos;
   local roobi reuses gh-style device-flow token (keychain) for local API calls
```

### 14d. Recommendation (UX + reliability first)

**Hybrid, mirroring Mintlify/CodeRabbit but leaning on the user's existing runtime and auth:**
- Standalone **single-binary CLI** (Go/Rust), Homebrew + curl install.
- **Local:** managed `core.hooksPath` hook installed by `roobi init`; pre-push drafts docs;
  fast, timeout-guarded, **never hard-blocks** on roobi failure.
- **Remote/team:** a **roobi GitHub App** (webhooks + installation tokens) for PR events and
  non-roobi authors, opening **draft** PRs (never blocking) — the reliable backstop that closes
  the `--no-verify` gap.
- **LLM:** delegate to the user's authenticated **Claude Code / Codex** runtime; roobi never
  handles model keys.
- **GitHub auth:** device flow + keychain for the local CLI (reuse `gh` where possible);
  one-time App install for teams.
- **Honest cost:** the GitHub App requires roobi to host a small webhook + token-minting
  service — the one unavoidable cloud dependency for reliable team coverage. A non-blocking
  GitHub Actions workflow is the no-server fallback for teams that won't host anything.

Why this serves UX + reliability: the local hook gives the instant, in-flow experience but is
best-effort (so it can never wedge a push); the GitHub App guarantees nothing slips through
regardless of tooling or `--no-verify`; and by reusing Claude Code/Codex + `gh` auth, the user
authenticates things they mostly already have, so `roobi init` is near zero-config.

Sources:
- GitHub Apps vs OAuth vs PAT — https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps
- Deciding when to build a GitHub App — https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/deciding-when-to-build-a-github-app
- Husky / lefthook (git hook distribution) — https://typicode.github.io/husky/ , https://github.com/evilmartians/lefthook
- OAuth device flow for CLIs / gh token storage — https://cli.github.com/manual/gh_auth_login
- Mintlify self-updating docs (GitHub App + automations + agent) — https://www.mintlify.com/docs/guides/automate-agent
- CodeRabbit (GitHub App + CLI with Claude Code/Codex) — https://docs.coderabbit.ai/cli

## 15. Open questions / deferred decisions

- **[Open]** Exact mechanics of the runtime adapters (how roobi hands a task to Claude Code /
  Codex app / Codex CLI and gets back a reviewable diff).
- **[Open]** Final `rules.yaml` schema and the deterministic checker that consumes it
  (`baseline/rules.yaml` is a careful first draft, §11).
- **[Open]** The heuristic for "small repo" (single-package) that unlocks README spawning.
- **[Open]** Whether/when to add embeddings-based retrieval as a linkage supplement (§8).
- **[Open]** Authoring roobi's baseline prose style guide (`baseline/style.md`), currently
  intentionally minimal.
- **[Open]** Where the GitHub App's webhook receiver + token-minting service is hosted, and
  whether a hosted service is offered vs. a self-host / GitHub Actions option for teams that
  won't run infra (§14a, §14d).
- **[Open]** Binary implementation language (Go vs Rust) and distribution channels beyond
  Homebrew/curl (§14b).
- **[Open]** Whether the local CLI reuses the user's existing `gh` auth or mints its own
  device-flow token (§14c).

## 16. Phased build plan — [Proposed]

1. **Prototype as an embedded subagent** (fastest feedback), structured to lift into the
   standalone version: tight system prompt heavy on *when to stay silent* and *verify before
   writing*; `covers:` anchors; a `pre-push` hook that blocks-with-message on stale docs; a
   verify step; global prefs + git-ignored `.roobi/` cache.
2. **Extract the engine into the standalone roobi CLI** with the runtime-adapter layer
   (Claude Code / Codex app / Codex CLI).
3. **Add the non-blocking PR bot**, including the non-roobi-author path (§5).
4. **Add the generated SSOT index and monorepo scoping** (per-package style/rules cascade).
5. **Harden trust surfaces**: per-doc opt-in auto-commit, CODEOWNERS routing, the internal
   changelog ledger.
```
