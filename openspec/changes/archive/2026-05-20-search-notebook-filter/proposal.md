## Why

The `/search` page's Ask tab already accepts a `notebook_id` via the `useAsk` hook (Wave 5A plumbing), but the search page UI provides no way to select a notebook. This means the `EvidenceBadge` tooltip — which displays evidence routing metadata (`evidence_need`, `evidence_layers_used`, `fallback_occurred`) — can only be manually tested when asking from inside a notebook's chat. Adding a lightweight notebook filter dropdown on the search page lets developers and testers exercise notebook-scoped Ask queries and verify the `EvidenceBadge` tooltip without navigating away from the search page.

## What Changes

- Add an optional notebook selector (dropdown / combobox) to the Ask tab on the search page.
- When a notebook is selected, pass its `notebook_id` to `ask.sendAsk()` so the backend graph receives notebook-scoped context.
- Display the selected notebook name as a dismissible badge/chip next to the question input for visual confirmation.
- No backend changes required — `AskRequest.notebook_id` and `stream_ask_response(notebook_id=...)` already exist.

## Capabilities

### New Capabilities
- `search-notebook-filter`: Optional notebook picker on the search page's Ask tab that passes `notebook_id` to the Ask API.

### Modified Capabilities
_(none — no existing spec-level requirements change)_

## Impact

- **Frontend only**: [`frontend/src/app/(dashboard)/search/page.tsx`](file:///d:/Project%20Web/TEST%20OPEN%20NOTEBOOK/frontend/src/app/(dashboard)/search/page.tsx) — add notebook selector UI and wire selected ID into `handleAsk`.
- **Hooks**: No changes to [`use-ask.ts`](file:///d:/Project%20Web/TEST%20OPEN%20NOTEBOOK/frontend/src/lib/hooks/use-ask.ts) — `sendAsk` already accepts optional `notebookId`.
- **Types**: No changes to [`search.ts`](file:///d:/Project%20Web/TEST%20OPEN%20NOTEBOOK/frontend/src/lib/types/search.ts) — `AskRequest.notebook_id?` already declared.
- **API**: No backend changes — [`search.py`](file:///d:/Project%20Web/TEST%20OPEN%20NOTEBOOK/api/routers/search.py) already plumbs `notebook_id` through.
- **Dependencies**: Reuses existing `useNotebooks()` hook from [`use-notebooks.ts`](file:///d:/Project%20Web/TEST%20OPEN%20NOTEBOOK/frontend/src/lib/hooks/use-notebooks.ts).
