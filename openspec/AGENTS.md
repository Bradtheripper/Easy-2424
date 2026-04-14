# OpenSpec Instructions

These instructions apply to any AI coding assistant working in this repository (Claude, Cursor, Copilot, Windsurf, etc.). They describe **how to use OpenSpec** — a lightweight, spec-driven workflow for aligning humans and AI on *what* to build before *how* to build it.

---

## 1. What OpenSpec Is

OpenSpec keeps three things in sync:

1. **Specs** — the current, agreed behavior of each capability (`openspec/specs/`).
2. **Changes** — proposed modifications to that behavior (`openspec/changes/`).
3. **Code** — the implementation in the rest of the repository.

Specs are the source of truth. Changes are the mechanism for evolving specs. Code must conform to whichever spec is currently active.

---

## 2. Directory Layout

```
openspec/
├── project.md          # Project context: stack, conventions, constraints
├── AGENTS.md           # This file — instructions for AI assistants
├── specs/              # CURRENT behavior (merged, active capabilities)
│   └── <capability>/
│       └── spec.md
└── changes/            # PROPOSED behavior (active, not yet merged)
    └── <change-id>/
        ├── proposal.md     # Why + what + impact
        ├── tasks.md        # Ordered checklist of work
        ├── design.md       # (optional) Technical decisions, trade-offs
        └── specs/
            └── <capability>/
                └── spec.md # Delta: ADDED / MODIFIED / REMOVED / RENAMED
```

---

## 3. The Three-Stage Workflow

### Stage 1 — Propose

Before writing non-trivial code, create a change folder under `openspec/changes/<change-id>/` containing:

- **`proposal.md`** — problem statement, proposed solution, affected capabilities, out-of-scope notes.
- **`tasks.md`** — ordered, checkable task list (`- [ ] …`). Each task should be small enough to review.
- **`specs/<capability>/spec.md`** — the *delta* against the current spec. Use these headers:
  - `## ADDED Requirements`
  - `## MODIFIED Requirements`
  - `## REMOVED Requirements`
  - `## RENAMED Requirements`
- **`design.md`** (optional) — only when architectural decisions warrant discussion.

Wait for human approval of the proposal before starting implementation.

### Stage 2 — Implement

1. Work through `tasks.md` top-to-bottom.
2. Check items off (`- [x]`) as they complete.
3. Keep code changes scoped to the proposal. If scope grows, update the proposal first.
4. Reference the change id in commit messages (e.g. `feat(auth): add magic-link login — change/add-magic-link`).

### Stage 3 — Archive

When the change is merged and deployed:

1. Apply the deltas to `openspec/specs/<capability>/spec.md` so the spec reflects the new reality.
2. Move the change folder to `openspec/changes/archive/YYYY-MM-DD-<change-id>/`.
3. Commit with a message like `chore(openspec): archive <change-id>`.

After archiving, `openspec/specs/` alone describes current behavior.

---

## 4. Writing Good Specs

- **Requirements are testable**. Each should map to at least one scenario or test.
- Use `MUST`, `SHOULD`, `MAY` (RFC 2119) to signal obligation level.
- Prefer short, numbered requirements over long prose. Group them under clear headings.
- Reference code paths with `path/to/file.ext:line` when helpful.
- Avoid duplicating information — link between specs instead.

Example requirement:

> **R-AUTH-003** The login endpoint MUST reject requests whose `Content-Type` is not `application/json` with HTTP 415.

---

## 5. Guardrails for AI Assistants

- **Read before you write.** Always consult `project.md` and any existing specs before proposing changes.
- **One change per folder.** Don't bundle unrelated edits; it makes review and archiving harder.
- **Deltas, not rewrites.** In `changes/**/specs/`, describe only what's different, not the whole spec.
- **Ask when unsure.** If a requirement is ambiguous or conflicts with existing behavior, surface it in the proposal rather than guessing.
- **No silent drift.** If you discover code that already violates a spec, either fix it in the current change or file a follow-up proposal — don't update the spec to match reality without approval.
- **Keep scope honest.** Out-of-scope items belong in a separate proposal, not as hidden extras.

---

## 6. Change IDs

Use `<verb>-<subject>` in kebab-case. Keep them short and searchable.

Good: `add-magic-link-login`, `rename-user-to-account`, `remove-legacy-export`
Avoid: `fix-stuff`, `misc-updates`, `change-1`

---

## 7. Minimal Templates

### `proposal.md`

```markdown
# <Change Title>

## Why
<1–3 sentences describing the problem or motivation.>

## What
<Concrete description of the proposed change.>

## Affected capabilities
- <capability-a>
- <capability-b>

## Out of scope
- <explicit non-goals>
```

### `tasks.md`

```markdown
# Tasks

- [ ] Update spec delta under `specs/<capability>/spec.md`
- [ ] Implement <module>
- [ ] Add tests for <behavior>
- [ ] Update documentation
```

### `specs/<capability>/spec.md` (delta)

```markdown
# <capability> — delta

## ADDED Requirements
- **R-…** …

## MODIFIED Requirements
- **R-…** (was: …) now: …

## REMOVED Requirements
- **R-…** …

## RENAMED Requirements
- **R-OLD-ID** → **R-NEW-ID**
```

---

## 8. TL;DR for Assistants

1. Read `openspec/project.md`.
2. Read the relevant `openspec/specs/<capability>/spec.md`.
3. Create a change folder with `proposal.md` + `tasks.md` + spec deltas.
4. Get approval.
5. Implement, checking off tasks.
6. Archive on merge.

When in doubt: **spec first, code second**.
