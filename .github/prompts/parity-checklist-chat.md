# Feature-Parity Checklist — Chat Migration

> Phase 0 Deliverable A. Companion to
> [plan-chatMigration-phase0.prompt.md](plan-chatMigration-phase0.prompt.md)
> and [plan-chatMigration.prompt.md](plan-chatMigration.prompt.md).
>
> Status: **DRAFT for review** (2026-09-05). Each row's "Proposed" column is a
> starting suggestion — review, change as needed, and tick the decision.
> The signed-off version of this file is the acceptance record for the whole
> migration: anything NOT on the signed-off list is out of scope.
>
> **Beta progress (2026-09-06):** rows with `[x]` or ✅ notes are working in the
> beta panel today; unticked rows remain for the parity phase. The beta POC
> itself is VALIDATED — see
> [plan-chatMigration-beta.prompt.md](plan-chatMigration-beta.prompt.md).

---

## How to use

- Review each section against the **current Chainlit UI** (live app).
- Adjust the **Proposed** decision where you disagree.
- Sign off at the bottom. Sections may be reviewed independently.

Legend: **K** = keep (must work in the new UI) · **D** = drop ·
**C** = change (keep the capability, new form) · **?** = verify before deciding.

---

## A. Platform settings (from `.chainlit/config.toml`)

| # | Setting | Current | Proposed | Notes |
|---|---|---|---|---|
| A1 | `features.unsafe_allow_html` | true | **C** | Only exists for the token-footer `<div>`. Becomes structured metadata in the new UI. |
| A2 | `features.latex` | false | **D** | Already off. |
| A3 | `features.show_readme_button` | false | **D** | |
| A4 | `features.user_message_autoscroll` | true | **K** | Ours to implement. |
| A5 | `features.assistant_message_autoscroll` | true | **K** | Ours to implement. |
| A6 | `features.auto_tag_thread` | true | **C** | Profile moves from thread tag to `threads.metadata.agent`. |
| A7 | `features.edit_message` | true | **D** | Decided 2026-08-16. |
| A8 | `features.allow_thread_sharing` | false | **D** | |
| A9 | `features.favorites` | false | **D** | |
| A10 | `features.audio.enabled` | false | **D** | |
| A11 | `spontaneous_file_upload.accept` | images, PDF, text, audio, xlsx/xls | **K** | ✅ Server-side via `process_file` (50MB cap); beta UI uploads done. |
| A12 | `spontaneous_file_upload.max_files` | 20 | **K** | No hard server-side count yet — beta allows multi-select without a cap. |
| A13 | `spontaneous_file_upload.max_size_mb` | 50 | **K** | Reconciled 2026-08-16 (`MAX_FILE_SIZE_MB` 100 → 50). |
| A14 | `project.session_timeout` | 7200 | **N/A** | Stateless after migration. |
| A15 | `project.user_session_timeout` | 1296000 | **K** | Must stay matched to `SessionMiddleware max_age` in `main.py` (currently 15 days ✓). |
| A16 | `project.transports` | `["websocket"]` | **N/A** | ✅ Replaced by SSE — verified through the Railway proxy in beta. |
| A17 | `project.allow_origins` | `["*"]` | **C** | Tighten to same-origin after migration. |
| A18 | `UI.confirm_new_chat` | true | **K** | Our own "new chat" confirmation (prevents accidental thread reset). |
| A19 | `UI.cot` | hidden | **C** | Decide how reasoning is displayed in the new UI (currently hidden entirely). |
| A20 | `UI.custom_css` / `UI.custom_js` | `/public/stylesheet.css`, `/public/embedded.js` | **C** | Review what these do (theme tweaks, embed widget?); reimplement as own frontend assets, not verbatim. |
| A21 | `UI.alert_style` | classic | **D** | |
| A22 | `UI.default_theme` + dark toggle | light + user toggle | **K** | ✅ Dark mode works in the beta panel. |

---

## B. Core chat capabilities (behavioural)

All are **Keep** unless you decide otherwise.

- [x] B1 Thread list: create / rename / delete / resume
      *(beta: rename wired on the standalone page; pending in the panel —
      `PATCH /chat-ui/threads/{id}` exists)*
- [ ] B1a **Resume backfill reconciliation** — `on_chat_resume` backfills missing
      `assistant_message` steps from the checkpoint (`plan_resume_backfill`).
      This is the load-bearing part of "resume", not the list itself.
- [ ] B2 Thread auto-naming (`"{RFQ} — {customer}"`)
- [x] B3 Agent choice — removed from the UI; routing is command-driven
      (one-shot intents map to the owning agent server-side)
- [x] B4 Composer command buttons per profile (`INTENTS` / `RESEARCH_INTENTS`)
      *(Tools dropdown with prefills; a chip shows the active command)*
- [ ] B5 Welcome message variants (first-visit × known-name × 3 profiles)
- [x] B6 Transient "⏳ Using {tool}… (xN)" progress line
- [ ] B7 Token/cost footer (becomes structured metadata — not raw HTML)
- [x] B8 Stop button + cooperative cancellation
- [x] B9 File upload → vision/text extraction (PDF pages, xlsx, images)
- [x] B9a **Attachment serving/rendering** — existing `elements` rows render via
      `/files/{objectKey}`. Note `ATTACHMENT_RETENTION_DAYS` means some old
      links are already dead (not a regression).
- [ ] B10 Browser screenshot inline image in chat
- [x] B11 Dark mode; sidebar collapse state *(beta: dark mode ✓)*
- [x] B12 Markdown + code block rendering
- [x] B12a **Sanitised rendering of historical messages** — legacy `steps.output`
      contains baked-in HTML (token footer) from `unsafe_allow_html = true`.
      Never render as raw HTML. Correctness **and** XSS concern.
- [ ] B13 New-chat confirmation (A18)
- [ ] B14 Copy-to-clipboard on messages / code blocks (free in Chainlit today)
- [ ] B15 Message timestamps
- [ ] B16 Avatars — `/avatar` endpoint, `data/avatar_cache/`, `/public/avatars/`
- [x] B17 `RunInProgress` handling — per-thread lock already exists; surface a
      friendly "run already active" state rather than an error
- [ ] B18 Feedback (thumbs up/down) — **DROP** (decided 2026-09-05). Chainlit's
      `feedbacks` table exists in the schema but is unused; not carried forward.

Notes / proposals:

- **B7** — currently rendered via `unsafe_allow_html`; new UI should render token
  usage as data, not HTML.
- **B10** — verify the exact rendering path (screenshot produced by
  `browser_tools`; confirm it is attached as an inline image element).
- **B12a** — required even for the beta POC, since viewing historical threads is
  in scope there. Implemented (sanitise-on-read) but no automated test yet.
- **C-A** — still orphaned to Chainlit during the beta (Phase 5); documented in
  the beta plan. Beta users keep `/chat` available for those flows.
- **Beta extras shipped beyond parity:** RFQ hard-binding (lock/Clear/🔗
  badges), thread-keyed dashboard context (multi-tab isolation), compact
  Preline-style composer.

---

## C. Action buttons

"Action buttons work" is not a parity criterion — each row is a distinct
user-facing capability and must be individually verified.

### C-A. Dashboard-initiated (9 call sites via `/api/agent-bridge`)

| # | Action | Trigger in UI | Proposed |
|---|---|---|---|
| C-A1 | `rfq_update_supplier` | Supplier status change | **K** |
| C-A2 | `rfq_identify_items` (all) | "Classify & validate all items" | **K** |
| C-A3 | `rfq_identify_items` (single line) | Same action, single-line variant | **K** — confirm payload shapes differ |
| C-A4 | `rfq_find_suppliers` | "Find suppliers" for one line | **K** |
| C-A5 | `rfq_group_items` | "Group items" | **K** |
| C-A6 | `rfq_find_all_suppliers` | "Find suppliers for all items" | **K** |
| C-A7 | `rfq_find_previous_suppliers` | "Search our records" | **K** |
| C-A8 | `rfq_find_brand_suppliers` | "Find brand-linked suppliers" | **K** |
| C-A9 | `rfq_find_new_suppliers` | "Search the web for new suppliers" | **K** |

### C-B. Chat-emitted (13 actions, rendered as buttons on agent messages)

| # | Action | Purpose | Proposed |
|---|---|---|---|
| C-B1 | `rfq_refresh` | Refresh dashboard view | **K for now** — re-evaluate once unified frontend lands |
| C-B2 | `rfq_find_web_suppliers_for_line` | Web search, single line | **K** |
| C-B3 | `rfq_dismiss` | Dismiss a pipeline prompt | **K for now** — re-evaluate once unified frontend lands |
| C-B4 | `rfq_pipeline_fix_part` | Correct a mis-parsed part number | **K** |
| C-B5 | `rfq_pipeline_skip_validation` | Skip validation, reset fix counter | **K** |
| C-B6 | `rfq_pipeline_retry_validation` | Retry validation | **K** |
| C-B7 | `rfq_pipeline_web_search` | Heaviest — 6 messages, 3 dashboard notifies | **K** |
| C-B8–C-B12 | `rfq_pipeline_{previous,brand,new_domestic,new_international}_suppliers` + `supplier_search_done` | Supplier-search menu (verify as ONE menu) | **K** |
| C-B13 | `rfq_add_brand_supplier` | Add a brand-linked supplier | **K** |

### C-C. System actions (all in app.py)

| # | Action | Proposed |
|---|---|---|
| C-C1 | `new_conversation` | **K** |
| C-C2 | `cancel_run_script` | **K** |
| C-C3 | `cancel_job` | **K** |
| C-C4 | `stop_agent` (also `/api/stop-agent` — note the lock bypass) | **K** — decide whether the HTTP endpoint survives |

### C-D. Dropped (decided 2026-08-16)

`delete_all_data`, `confirm_delete_all`, `cancel_delete_all` — **not carried**.
Side-effect: the agent can no longer invoke a destructive delete via
`action_tools`. `ADMIN_ONLY_TOOLS` remains as the registration point.

---

## D. Items to verify before porting (not a decision yet)

- [ ] D1 `public/embedded.js` + `public/stylesheet.css` — rated **M** coupling in
      the parent plan (postMessage, DOM-scraping profile detection, sidebar
      cookie hacks). This is known iframe glue **to be removed**, not merely
      reviewed. Informs A20.
- [ ] D2 `rfq_identify_items` payload shapes (C-A2 vs C-A3) — confirm both covered.
- [ ] D3 Supplier-search menu (C-B8–C-B12) — treat as one component, not five buttons.
- [ ] D4 B10 screenshot rendering path.
- [ ] D5 SSE on Railway proxy — replicate/verify the WebSocket-only reason (A16).
- [ ] D6 **Thread id invariant** — `threads.id` == LangGraph `thread_id` ==
      `rfq_threads.thread_id` == `rfqs.thread_id` must hold for threads created
      by the new UI.
- [ ] D7 Stale artefacts to clean up en route (parent §12): `chainlit_datalayer.db`,
      `chainlit.md`, 23 stock locale files.

---

## E. Sign-off

| Section | Decision | Reviewer | Date |
|---|---|---|---|
| A. Platform settings | pending | | |
| B. Core capabilities | pending | | |
| C. Action buttons | pending | | |
| D. Verification items | pending | | |
| Whole checklist | pending | | |
