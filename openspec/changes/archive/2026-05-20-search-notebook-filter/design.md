## Context

The search page (`/search`) has an Ask tab that runs questions through the `ask_graph` LangGraph pipeline. Wave 5A already added `notebook_id` plumbing end-to-end:

- **Frontend type**: `AskRequest.notebook_id?` in `search.ts`
- **Hook**: `useAsk().sendAsk(question, models, notebookId?)` accepts an optional third argument
- **API router**: `search.py` passes `notebook_id` into `stream_ask_response()` and the graph input
- **Graph**: `ask_graph` uses `notebook_id` to scope evidence retrieval

However, the search page UI never passes a `notebookId` — only the notebook chat (`useNotebookChat`) does. The `EvidenceBadge` component (which shows evidence tier, layers used, and fallback status in a tooltip) therefore cannot be manually tested in notebook-scoped mode from the search page.

## Goals / Non-Goals

**Goals:**
- Let users optionally scope an Ask query to a specific notebook from the search page
- Enable manual testing of `EvidenceBadge` tooltip with notebook-scoped evidence routing
- Keep the change minimal — frontend-only, no new API endpoints or model changes

**Non-Goals:**
- Adding notebook filtering to the Search tab (text/vector search) — different plumbing needed
- Persisting the selected notebook across page reloads (URL param support can come later)
- Building a full notebook-aware search experience (this is a testing/power-user affordance)

## Decisions

### 1. Use a simple `<Select>` dropdown, not a combobox

**Rationale**: The notebook list is typically small (< 50 items). A combobox with search adds complexity for minimal gain. The existing shadcn `Select` component is already used elsewhere in the app and keeps the UI consistent.

**Alternative considered**: Combobox with typeahead — rejected as over-engineering for this use case.

### 2. Place the selector below the question textarea, inline with model badges

**Rationale**: Keeps it visually grouped with other "query context" controls (model selection). A dismissible `Badge` showing the selected notebook name provides clear visual feedback without cluttering the input area.

**Alternative considered**: Placing it above the textarea as a filter bar — rejected because it would imply it filters the Search tab too, which it does not.

### 3. Reuse `useNotebooks()` hook directly

**Rationale**: The hook already exists, returns the notebook list, and handles loading/error states. No new data fetching logic needed.

### 4. Pass `notebookId` as the third argument to `ask.sendAsk()`

**Rationale**: The `sendAsk` function already accepts `notebookId?` as its third parameter. Zero hook changes required — just wire the selected state value through `handleAsk`.

## Risks / Trade-offs

- **User confusion**: Users might not understand what "scope to notebook" means → Mitigate with a short helper text beneath the dropdown ("Limit search to sources within this notebook").
- **Empty notebook list**: If no notebooks exist, the dropdown should be hidden or disabled → Mitigate by conditionally rendering only when notebooks are available.
- **Stale notebook list**: If a notebook is deleted while the page is open, the selected ID could be invalid → Low risk; the backend handles unknown `notebook_id` gracefully (treats as unscoped).
