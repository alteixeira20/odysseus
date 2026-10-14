# Dedicated issue draft

Title: Complete the Notes reference decomposition under the merged F1 architecture

### Prerequisites

- [x] I searched open issues and this has not already been proposed.
- [x] I searched discussions and this is not already being debated there.
- [x] This is a concrete, actionable proposal — not a vague "it would be nice if..." request.

### Area

Notes / Editor

### Problem or Motivation

The original Notes implementation (`static/js/notes.js`) is a 5,471-line monolith combining recurrence rules, HTTP transport, LocalStorage persistence, mutable application state, reminder scheduling, canvas drawing, mobile touch gestures, and window layout in a single file. This creates maintenance bottlenecks, obscures data flow, prevents isolated unit testing of business logic, and risks regressions during UI or backend enhancements. Track its complete feature-owned decomposition under the reference frontend architecture established in PR #6701.

### Proposed Solution

Decompose Notes into 41 maintained modules under `frontend/src/features/notes/` and byte-identical deployment copies under `static/js/features/notes/`:

1. Pure recurrence and completion rules in `domain/` with no ambient clock or DOM access.
2. A single authoritative application-state owner (`application/state.js`) providing read-only live proxies to views, centralized workflows, serialized draft autosave, and a page-owned reminder scheduler.
3. Injected HTTP, LocalStorage persistence, and browser notification adapters in `infrastructure/`.
4. Bounded UI factories in `ui/` with explicit resource and listener disposal on panel closure.
5. A five-line legacy compatibility facade at `static/js/notes.js` preserving all existing named exports, aliases (`openNotes`, `closeNotes`, `isNotesOpen`), `window.notesModule` identity, and synchronous selected Notes/Todos agent context acknowledgement (`odysseus:notes-context-selected` from PR #6495).

Delivered in exactly one comprehensive PR targeting official `dev`, with N0 through N6 as internal checkpoints.

### Alternatives Considered

1. In-place refactoring within `static/js/notes.js`: Rejected because it would leave monolithic coupling intact and violate F1 architectural boundaries.
2. Multiple incremental PRs: Rejected because splitting domain, application, UI, and facade across PRs would create broken intermediate states, duplicate stores, or incomplete service-worker precache entries.

### Prior Art / Related Issues

- PR #6701: Frontend source ownership and architecture gates (ADR-001).
- PR #6495: Synchronous selected Notes/Todos agent context integration.
- Roadmap and design: `docs/frontend-architecture/notes-roadmap.md` and `docs/frontend-architecture/notes-case-study.md`.

### Are you willing to implement this?

Yes — I can open a PR
