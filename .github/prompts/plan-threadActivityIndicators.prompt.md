# Plan: Thread Activity Indicators

## Phase 1 — Core Infrastructure

### 1. Thread metadata status tracking
- In `app.py` or `includes/graph.py`, update thread metadata when run state changes:
  - Run starts → `metadata: {"status": "busy", "status_label": "Working..."}`
  - Interrupt fires (user input needed) → `metadata: {"status": "interrupted", "status_label": "Input required"}`
  - Run completes → `metadata: {"status": "idle"}` or clear status
- Use our existing `update_thread(metadata={...})` in `data_layer.py`
- LangGraph already tracks `idle`/`busy`/`interrupted`/`error` via the checkpointer — we map this to thread metadata that Chainlit can read

### 2. Lightweight status API endpoint
- `GET /api/threads/status` — returns `[{thread_id, status, status_label}]` for the current user's threads
- Only returns threads with non-idle status to keep payload small
- Alternatively, poll LangGraph's thread search API directly from JavaScript

## Phase 2 — Frontend Indicator

### 3. JavaScript DOM injection in `embedded.js`
- Poll the status endpoint every 3 seconds (or use LangGraph's thread search)
- Find Chainlit's thread list DOM elements and inject colored dots:
  - 🔵 Blue = `busy` (ongoing activity)
  - 🟠 Orange = `interrupted` (user input required)
  - No dot = `idle` or `error`
- Use CSS transitions for smooth appearance/disappearance
- Handle Chainlit thread list re-renders (MutationObserver?)

### 4. CSS styling
- Position the dot to the left of the thread name
- Ensure it doesn't break Chainlit's layout
- Add a subtle pulse animation for `busy` state

## Phase 3 — Polish

### 5. Testing
- Verify dots appear/disappear correctly during:
  - Agent streaming a response (busy)
  - Agent waiting for user action button confirmation (interrupted)
  - Multiple concurrent threads
- Test across dark/light mode

### 6. Fallback behavior
- If Chainlit DOM structure changes, dots silently fail (no crashes)
- Graceful degradation: no dots is better than broken UI
