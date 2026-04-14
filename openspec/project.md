# Project Context

> This file is the shared context for humans and AI assistants working on this
> repository. Keep it short, current, and factual. If something here becomes
> stale, update it in the same change that makes it stale.

## Overview

<!--
One paragraph: what this project is, who it serves, what problem it solves.
Example:
"Easy-2424 is a <type of system> that <core value proposition>. It is used
by <users> to <primary workflow>."
-->

TBD — fill in once the project's purpose is defined.

## Tech Stack

<!--
List the key pieces of the stack and their versions. Only include what is
actually used; avoid aspirational tooling.
-->

- Language(s): TBD
- Runtime / framework: TBD
- Package manager: TBD
- Test runner: TBD
- Lint / format: TBD
- CI: TBD

## Repository Layout

<!--
A short map of the top-level directories and what lives in each. Update when
structure changes.
-->

- `openspec/` — specs, change proposals, and AI assistant instructions.
- TBD — add source directories as they are created.

## Conventions

<!--
Project-specific rules that are not obvious from the code. Examples:
- Commit message style (Conventional Commits, etc.)
- Branch naming
- Error-handling patterns
- Logging / telemetry expectations
- Public API stability guarantees
-->

- **Spec-driven development.** Non-trivial behavior changes go through
  `openspec/changes/` before implementation. See `openspec/AGENTS.md`.
- **Commits.** Prefer small, focused commits. Reference the OpenSpec change
  id when applicable (e.g. `feat(x): … — change/<id>`).
- **Branches.** Feature branches off the default branch; one change id per
  branch when practical.

## Constraints & Non-Goals

<!--
Things that are intentionally out of scope or off-limits. Helps assistants
avoid proposing work that will be rejected.
-->

- TBD

## Glossary

<!--
Domain terms that have a specific meaning here. Keep definitions tight.
-->

- **Capability** — a cohesive slice of behavior that has its own spec folder.
- **Change** — a proposed delta to one or more capabilities, living under
  `openspec/changes/<change-id>/` until archived.
- **Delta** — the `ADDED / MODIFIED / REMOVED / RENAMED` description of how
  a change alters existing requirements.
