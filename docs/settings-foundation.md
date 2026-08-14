# Settings foundation

This document defines the architecture and migration contract for the Settings refactor on `refactor/settings-foundation`.

The goal is not to move every environment variable into the browser. The goal is to make Settings the canonical UI for configuration that is safe, useful, and expected to change at runtime, while preserving domain ownership and deployment boundaries.

## Why this refactor exists

Settings currently spans several ownership models at once:

- `static/js/settings.js` owns navigation, modal lifecycle, appearance behavior, AI defaults, search, account behavior, reminders, and other panel-specific logic.
- `static/index.html` hardcodes the Settings navigation and panel containers.
- admin-managed panels are delegated from `settings.js` to `window.adminModule`.
- `routes/prefs_routes.py` provides a generic per-user JSON key/value store without a schema for known Settings keys.
- some browser-only preferences are persisted directly in `localStorage` for early startup behavior.
- integrations are split between `settings.js`, domain APIs, and transitional `integrationCategoryActions.js` / `integrationHeaderActions.js` modules.

The result works, but adding a setting often requires knowing several unrelated implementation details and editing a large shared file. That makes the Settings surface harder to audit and easier to regress.

## Non-goals

This refactor must not:

- create one backend "settings god object" that owns email, MCP, model endpoints, tokens, users, or other domain resources;
- expose deployment/bootstrap secrets merely because they are configurable through environment variables;
- replace `.env`, Docker Compose, or infrastructure configuration;
- change authentication or authorization semantics as part of frontend modularization;
- require a frontend framework migration;
- change existing API contracts solely to fit the Settings UI.

## Configuration classes

Every value considered for Settings must be classified before it is exposed.

### 1. User preference

Per-user, runtime-safe preferences such as UI behavior, defaults, presentation, shortcuts, and similar choices.

Preferred persistence: the per-user preferences API, with a typed registry for known keys.

### 2. Domain-managed runtime setting

Configuration that is safe to change in the UI but belongs to a specific subsystem. Examples include integration accounts, agent credentials, model endpoints, and similar resources.

Preferred persistence: the subsystem's existing API. Settings is only the presentation layer.

### 3. Admin runtime setting

Deployment-wide behavior that can safely change while Odysseus is running, but should be restricted to administrators.

Preferred persistence: the owning admin/domain API with authorization enforced server-side.

### 4. Deployment-only setting

Infrastructure or bootstrap configuration such as database connection details, bind addresses, container topology, initial credentials, or secret-storage internals.

Preferred persistence: environment/deployment configuration. These values do not become ordinary Settings fields.

## Current Settings information architecture

The sidebar currently exposes these panels:

| Tab id | Label | Current owner |
| --- | --- | --- |
| `services` | Add Models | delegated to admin module |
| `added-models` | Added Models | delegated to admin module |
| `ai` | AI Defaults | `settings.js` |
| `search` | Search | `settings.js` |
| `integrations` | Integrations | delegated/admin integration renderer plus Settings glue |
| `email` | Email | `settings.js` and email APIs |
| `reminders` | Reminders | `settings.js` |
| `appearance` | Appearance | `settings.js` plus browser startup persistence |
| `shortcuts` | Shortcuts | `settings.js` |
| `account` | Account | `settings.js` and auth APIs |
| `tools` | Agent Tools | delegated admin module |
| `users` | Users | delegated admin module |
| `system` | System | delegated admin module |

The first architectural milestone is to preserve this behavior while moving ownership into explicit modules. Information-architecture changes happen only after the shell is modular and covered by regression tests.

## Persistence contract

### Per-user preferences

`/api/prefs` remains the compatibility surface for ordinary user preferences during the refactor. The endpoint already provides per-user storage and legacy-format compatibility.

A later slice will add a registry for known Settings keys so the server can validate type/default/scope for registered preferences while continuing to tolerate existing unknown keys where backward compatibility requires it.

### Domain resources

Settings must continue to use domain APIs for resources with their own lifecycle. Examples include:

- API/agent tokens;
- email accounts;
- calendar/contact integrations;
- MCP servers;
- model endpoints;
- users and privileged system operations.

The Settings registry describes how to present and find these panels; it does not absorb their persistence or authorization rules.

### Browser startup preferences

Some appearance/sidebar values are applied from `localStorage` before the main app boots to prevent visual flashes. Those startup-sensitive values need an explicit compatibility plan before being moved. The modularization phase should wrap existing persistence first, not silently change load order.

## Target frontend boundaries

Files are created only when real code moves into them. Empty placeholder modules are not part of the plan.

```text
static/js/settings/
├── index.js
├── registry.js
├── navigation.js
├── lifecycle.js
├── persistence.js
├── components/
│   ├── dialog.js
│   ├── field.js
│   ├── section.js
│   └── search.js
├── panels/
│   ├── account.js
│   ├── appearance.js
│   ├── models.js
│   ├── search.js
│   ├── reminders.js
│   ├── shortcuts.js
│   └── system.js
└── integrations/
    ├── index.js
    ├── agents.js
    ├── mcp.js
    ├── email.js
    ├── calendar.js
    ├── contacts.js
    └── api.js
```

`static/js/settings.js` remains a compatibility entry point while behavior is extracted. It should shrink slice by slice instead of being replaced in one large rewrite.

## Settings registry contract

The registry will describe navigation/search/presentation metadata, not own domain persistence.

A registered setting or panel may expose metadata such as:

```js
{
  id: 'appearance',
  group: 'general',
  label: 'Appearance',
  description: 'Theme, density, and display preferences',
  keywords: ['theme', 'font', 'density', 'color'],
  scope: 'user',
  adminOnly: false,
  runtimeEditable: true,
  restartRequired: false,
}
```

For scalar user preferences, the registry may additionally describe `type`, `default`, `choices`, and validation constraints. Secret values must never be returned to the browser through registry metadata.

## Migration rules

Each refactor slice must satisfy all of the following:

1. preserve existing routes and persisted values unless the slice explicitly documents a migration;
2. move one coherent responsibility at a time;
3. add or strengthen regression coverage before deleting compatibility code;
4. avoid duplicate sources of truth between old and new modules;
5. keep authorization in backend/domain code;
6. keep commits small enough to review independently;
7. run syntax checks and focused tests for every changed module;
8. perform a final UI pass only after the architectural slices are complete.

## Ordered implementation slices

### Slice 0 — baseline and architecture contract

- replace synthetic integration assertions with a harness that executes the production integration module;
- document ownership, persistence, scope boundaries, and target module layout.

### Slice 1 — Settings shell

Extract tab navigation, modal lifecycle, shared element helpers, and panel activation from `settings.js` with no intended visual change.

### Slice 2 — registry and navigation metadata

Introduce the Settings registry and make navigation/search metadata declarative while preserving the existing panel order initially.

### Slice 3 — modular panels

Extract existing panels one at a time. Prefer mechanical moves plus focused tests over behavior changes.

### Slice 4 — integrations consolidation

Move integration-specific code under `settings/integrations/` and retire the transitional category/header patch modules once their behavior is owned by the new integration module.

### Slice 5 — preference validation

Add typed validation/default metadata for known per-user preferences while preserving backward compatibility for existing stored values.

### Slice 6 — missing runtime-safe settings

Audit hardcoded and environment-backed configuration. Expose only values that fit the user/admin runtime classes above.

### Slice 7 — Settings UX

Add Settings search and apply consistent headers, actions, descriptions, empty states, reset/default behavior, responsive layout, and restart/admin indicators.

### Slice 8 — final hardening

Run the full focused/backend test matrix, keyboard/accessibility review, responsive UI review, security review, dead-code cleanup, and final branch diff audit before opening the upstream issue/PR pair.

## Review strategy

The branch should remain bisectable throughout the work. Refactors and behavior changes should not be mixed unless the behavior change is necessary to complete the extraction safely. Transitional compatibility code should be removed in a later explicit commit rather than hidden inside an unrelated move.
