# Easy-2424

This repository uses **[OpenSpec](./openspec/AGENTS.md)** for spec-driven
development. Before proposing non-trivial changes, read:

- [`openspec/project.md`](./openspec/project.md) — project context and
  conventions.
- [`openspec/AGENTS.md`](./openspec/AGENTS.md) — how specs and change
  proposals work in this repo.

## Layout

```
openspec/
├── project.md    # Project context
├── AGENTS.md     # Instructions for AI assistants
├── specs/        # Current, agreed behavior (source of truth)
└── changes/      # Proposed changes (active work + archive/)
```

## Workflow

1. **Propose** — create `openspec/changes/<change-id>/` with `proposal.md`,
   `tasks.md`, and spec deltas.
2. **Implement** — work through `tasks.md`, keeping code aligned with the
   proposal.
3. **Archive** — fold deltas into `openspec/specs/` and move the change
   folder to `openspec/changes/archive/`.
