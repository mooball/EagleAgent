## Plan: Remove duplicate starter/action buttons to fix UI flicker

Both chat profiles (EagleAgent and System Admin) flash centered starter buttons before `on_chat_start` sends the real welcome message with action buttons. Fix by removing the redundant starters since both profiles already define their own action buttons in `on_chat_start`.

**Steps**
1. Remove the `starters=[...]` kwarg from the System Admin `ChatProfile` definition (~line 565 of app.py)
2. Replace the body of `set_starters()` to return `[]` (~line 588 of app.py), removing the four global `cl.Starter` entries

**Relevant files**
- app.py — remove `starters` from `ChatProfile` at ~line 565; gut `set_starters()` at ~line 588

**Verification**
1. `python -c "import app"` to confirm no syntax errors
2. `pytest tests/ -v` to check existing tests pass
3. Manual: log in as admin, select System Admin profile — should see welcome message with action buttons immediately, no centered starter flash
4. Manual: log in with EagleAgent profile — should see welcome message with intent buttons immediately, no centered starter flash

**Decisions**
- Keep the `@cl.set_starters` decorator with an empty return rather than removing it entirely, to prevent Chainlit from falling back to any default behavior
- No changes to `on_chat_start` — both profiles' action buttons are correct as-is
