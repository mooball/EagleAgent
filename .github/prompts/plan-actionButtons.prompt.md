# Migrate to Chainlit Action Buttons

**Goal**: Replace custom `/` slash commands with Chainlit-native action buttons and LangGraph tools. Actions are available to both admin and non-admin staff, with role-based guards on sensitive operations.

**Prerequisite for**: `plan-serverProcessing` (server-side script execution depends on this pattern).

---

## Phase 1 — Core Migration ✅

### ~~1. Create an action registry and dispatcher (`includes/actions.py`)~~ ✅
- ~~Define a registry of available actions with metadata: `{name, label, description, icon, admin_only: bool}`.~~
- ~~Each action maps to an async handler function.~~
- ~~The dispatcher checks the user's role (admin vs staff) before executing admin-only actions.~~
- ~~Role check uses the existing `config.get_admin_emails()` pattern.~~

### ~~2. Migrate `/new` to an action button~~ ✅
- ~~Register a `new_conversation` action available to **all users** (admin_only: false).~~
- ~~Handler: generates a new `thread_id` via `uuid.uuid4()`, sets it on `cl.user_session`, sends confirmation message.~~
- ~~Identical logic to the current `/new` handler, just triggered via action callback instead.~~

### ~~3. Migrate `/deleteall` to an action button with confirmation~~ ✅
- ~~Register a `delete_all_data` action as **admin-only**.~~
- ~~Handler sends a confirmation message with two action buttons: **[Yes, delete everything]** and **[Cancel]**.~~
- ~~On confirm: calls the existing `handle_deleteall_command()` from `includes/commands.py`.~~
- ~~On cancel: sends "Deletion cancelled" message.~~
- ~~Uses `@cl.action_callback` for the confirmation step.~~

### ~~4. Remove the slash command block from `app.py`~~ ✅
- ~~Delete the `if content.startswith("/")` block and the `awaiting_delete_confirmation` logic from `main()`.~~
- ~~All command logic now lives in action callbacks and LangGraph tools.~~
- ~~This eliminates the Chainlit textarea multiline-mode issue with `/` entirely.~~

---

## Phase 2 — Discovery & Presentation ✅

### ~~5. Add a "list actions" LangGraph tool~~ ✅
- ~~Register a `list_available_actions` tool available to **all users**.~~
- ~~Returns the action registry filtered by the user's role (non-admins don't see admin-only actions).~~
- ~~The agent is trained via system prompt to recognize natural language variants: "what can you do", "what commands are available", "what actions can I perform", "show me the menu", "what jobs/tasks/scripts/functions can I access", etc.~~
- ~~Returns a formatted list with name, description, and whether it's admin-only.~~
- Also added: `is_help_request()` intercept + `send_action_buttons()` for mid-conversation discovery via keywords (help, actions, menu, etc.)

### ~~6. Expose actions as LangGraph tools~~ ✅
- ~~Register key actions as LangGraph tools so users can trigger them via natural language:~~
  - ~~`start_new_conversation` — available to all users.~~
  - ~~`delete_all_user_data` — admin-only, with confirmation via action button callback.~~
- ~~Tools call the same handlers as action buttons — single implementation, two entry points.~~
- ~~Add to role-based tool filtering in `GeneralAgent.get_tools()` / `get_tools_async()`.~~
- `list_available_actions`, `start_new_conversation`, `delete_all_user_data` in `includes/tools/action_tools.py`
- `delete_all_user_data` added to `ADMIN_ONLY_TOOLS` for role-based filtering

### ~~7. Add chat starters for common actions~~ ✅
- ~~Use `@cl.set_starters` to show suggested actions when a new thread starts.~~
- ~~Include starters like "What can you help me with?" that trigger the action listing.~~
- ~~Keeps the UI discoverable without requiring users to know specific commands.~~
- `@cl.set_starters` added to `app.py` with "What can you help me with?", "Search for a product", "Show available actions".

---

## Phase 3 — Polish ✅

### ~~8. Update system prompt for action awareness~~ ✅
- ~~Add instructions to `includes/prompts.py` so the agent knows about available actions.~~
- ~~The prompt should include: list of actions the current user can access (based on role), guidance to suggest actions when users seem to be looking for commands/tools/features.~~
- ~~Keep it dynamic — built from the action registry, not hardcoded in the prompt.~~
- `_build_action_awareness()` added to `includes/prompts.py` — dynamically lists available actions from the registry, filtered by user role.
- Integrated into `build_system_prompt()`.

### ~~9. Write tests~~ ✅
- ~~Test action registration and role filtering (admin vs staff see different actions).~~
- ~~Test action button callbacks (new conversation, delete with confirmation).~~
- ~~Test LangGraph tool wrappers call the correct handlers.~~
- ~~Test that the slash command block is fully removed and `/` messages pass through to the agent normally.~~
- 28 tests in `tests/test_actions.py` covering: registry, role filtering, help-phrase detection, dispatcher role guard, LangGraph tool wrappers.

### ~~10. Update documentation~~ ✅
- ~~Update `copilot-instructions.md` with the action button pattern and how to add new actions.~~
- ~~Update `docs/DEVELOPMENT_WORKFLOW.md` if it references slash commands.~~
- `copilot-instructions.md` updated with action button pattern, project structure, and "how to add a new action" guide.
- No other docs referenced slash commands.

---

## Recommended Execution Order

1. **#1** Action registry and dispatcher
2. **#2** Migrate `/new`
3. **#3** Migrate `/deleteall` with confirmation
4. **#4** Remove slash command block
5. **#5** List actions tool
6. **#6** LangGraph tool wrappers
7. **#7** Chat starters
8. **#8** System prompt updates
9. **#9** Tests
10. **#10** Documentation

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Entry points | Action buttons + LangGraph tools (dual) | Buttons for explicit/discoverable access; tools for natural language |
| Role model | admin_only flag per action | Simple boolean; uses existing `ADMIN_EMAILS` infrastructure |
| Confirmation UX | Action button callbacks ([Yes] / [Cancel]) | Chainlit-native; no custom JS or slash parsing needed |
| Discovery | `list_available_actions` tool + chat starters | Natural language + visual prompts cover all user types |
| Slash commands | Remove entirely | Chainlit textarea `/` multiline bug; action buttons are idiomatic |
