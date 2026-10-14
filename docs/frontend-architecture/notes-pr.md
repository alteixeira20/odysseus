# PR description draft

Title: Decompose Notes into the F1 reference feature architecture

<!-- Publication is withheld. File the dedicated issue using docs/frontend-architecture/notes-issue.md first,
     then replace #6712 below with that issue number before publishing this PR. -->

## Summary

Decompose the 5,471-line Notes monolith into the F1 reference frontend architecture established in PR #6701, maintaining 41 modular source files under `frontend/src/features/notes/` and byte-identical deployment copies under `static/js/features/notes/`.

The refactor introduces a single authoritative application-state owner (`application/state.js`) providing read-only live proxies to views, pure recurrence and date rules (`domain/recurrence.js`, `noteRules.js`), injected HTTP, LocalStorage persistence, and notification adapters (`infrastructure/`), bounded UI factories (`ui/`), and a page-owned background reminder scheduler (`application/reminders.js`) that survives panel closure.

All public API contracts are preserved: the five-line legacy compatibility facade at `static/js/notes.js` retains all named exports, aliases (`openNotes`, `closeNotes`, `isNotesOpen`), `window.notesModule` identity, and synchronous selected Notes/Todos agent context acknowledgement (`odysseus:notes-context-selected` from PR #6495).

## Target branch

- [x] This PR targets the correct integration branch: **`lab`** in the private maintainer-preview repository, or **`dev`** in the public repository. `main` remains release-curated.

Target for this PR: official `odysseus-dev/odysseus`, `dev`.

## Linked Issue

Fixes #6712

<!-- Note for publisher: Create the dedicated feature issue from docs/frontend-architecture/notes-issue.md
     and replace #6712 above with the resulting issue number (e.g. Part of #6706). -->

## Type of Change

- [ ] Bug fix (non-breaking — fixes a confirmed issue)
- [ ] New feature (non-breaking — adds new behaviour)
- [ ] Breaking change (changes or removes existing behaviour)
- [x] Refactor / cleanup (behaviour unchanged)
- [ ] Documentation only
- [x] CI / tooling / configuration

The extraction preserves business behavior. The two intentional lifecycle corrections are page-owned background reminders surviving panel closure, and explicit disposal of Notes-owned UI observers and DOM listeners upon panel teardown.

## Checklist

- [x] I searched [open issues](https://github.com/odysseus-dev/odysseus/issues) and [open PRs](https://github.com/odysseus-dev/odysseus/pulls) — this is not a duplicate.
- [x] This PR targets the correct integration branch (`lab` in maintainer-preview; `dev` in the public repository)
- [x] My changes are limited to the scope described above — no unrelated refactors or whitespace changes mixed in.
- [x] I actually ran the app (`docker compose up` or `uvicorn app:app`) and verified the change works end-to-end. Type-checks and unit tests are not enough.
- [ ] I did not run the app/runtime validation and stated that gap in **How to Test**. Leave this unchecked when the app-run box above is checked.

## How to Test

1. **Frontend build and architecture checks:**
   ```sh
   python3 scripts/build_frontend.py --check
   node scripts/check_frontend.mjs
   node scripts/check_frontend.mjs --inventory
   git diff --check
   ```
2. **Node unit, oracle, recurrence, and contract tests (46 tests):**
   ```sh
   ENDPOINT_URL=http://127.0.0.1:1 node --test \
     tests/frontendArchitecture.test.mjs tests/noteTestOracle.test.mjs \
     tests/notesDraftAutosave.test.mjs tests/selectedNotesContext.test.mjs \
     tests/notesCharacterization.test.mjs tests/notesRecurrence.test.mjs \
     tests/notesInfrastructure.test.mjs tests/notesApplication.test.mjs \
     tests/notesReminders.test.mjs tests/notesWorkflows.test.mjs
   ```
3. **Focused Notes Python and offline module tests (172 passed, 1 smoke skipped):**
   ```sh
   CHROMADB_HOST=127.0.0.1 CHROMADB_PORT=1 \
     python3 -m pytest -q -rs \
     $(rg --files tests -g 'test_*note*.py') \
     tests/test_frontend_build.py tests/test_frontend_foundation_offline_js.py \
     tests/test_frontend_module_graph.py tests/test_frontend_module_version_parity.py \
     tests/test_panel_loader_js.py
   ```
4. **Isolated runtime distribution and browser tests:**
   ```sh
   python3 scripts/verify_frontend_runtime.py
   python3 scripts/verify_notes_browser_runtime.py
   ```
5. **Docker serving probe (verifies static confinement without frontend source or Node):**
   ```sh
   docker run --rm --network none \
     --mount type=bind,src="$PWD",dst=/candidate,readonly \
     --workdir /candidate --entrypoint python \
     odysseus-maintainer-preview-odysseus:latest scripts/verify_frontend_runtime.py
   ```

## Visual / UI changes — REQUIRED if you touched anything that renders

- [x] **Screenshot or short clip** of the change in the running app, attached below. Mobile screenshot too if the change affects mobile.
- [x] **Style match**: the change uses Odysseus's existing visual language. Specifically:
  - Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--card`, `--border`, etc.) — do not introduce new color values, font sizes, or spacing units.
  - Reuse existing button/input/card/border classes. Don't invent parallel styling.
  - **No Unicode emoji in UI or code.** Use inline SVG (matching the monochrome icon style already in `static/index.html`) or plain text.
  - Monospaced font (`Fira Code`) for primary UI text. Don't override.
  - Dark theme is the default; any light-mode work must be wired through the existing theme system, not hard-coded.
- [x] **No new component patterns.** If a similar widget already exists in the app, extend it instead of writing a parallel one.
- [ ] **I am not an LLM agent submitting a bulk PR.** If you are, please open an issue describing the problem first — bulk auto-generated PRs that don't match the project's visual style are closed on sight, even when the underlying fix is correct.

### Screenshots / clips

<!-- REQUIRED PUBLICATION STEP: Drag and drop the two verified screenshot files from the repository into this text area when opening the PR on GitHub.
     GitHub will automatically host them under https://github.com/user-attachments/assets/... and generate markdown embeds:
     ![Desktop screenshot](https://github.com/user-attachments/assets/...)
     ![Mobile screenshot](https://github.com/user-attachments/assets/...)
     Replace this comment block with the generated image embeds before submitting. -->

Verified local evidence produced by `scripts/verify_notes_browser_runtime.py` against the real running authenticated application (Chromium):
- Desktop view (1440x1000): `docs/frontend-architecture/evidence/notes/native-desktop.png`
- Mobile touch view (390x844): `docs/frontend-architecture/evidence/notes/native-mobile.png`

Screenshots demonstrate:
- Rendered Notes panel, search reset, tag chips, quick-add input, and note cards.
- End-to-end CRUD persistence and draft autosave recovery.
- Selected Notes/Todos agent context transfer and chat indicator reset.
- Both captures were inspected and confirmed free of credentials, secrets, tokens, or personal user data (test user: `notes-reference`).

Detailed module inventory, responsibility mapping, checkpoint receipts, rollback procedure, and accepted limits are documented in `docs/frontend-architecture/notes-case-study.md`.
