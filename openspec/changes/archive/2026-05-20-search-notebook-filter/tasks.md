## 1. Add notebook state and selector UI

- [x] 1.1 Import `useNotebooks` hook and `Select` component in `search/page.tsx`
- [x] 1.2 Add `selectedNotebookId` state (`useState<string>('')`)
- [x] 1.3 Add a `Select` dropdown below the question textarea (inside the Ask tab card) that lists notebooks from `useNotebooks()`, with an "All notebooks" / clear option
- [x] 1.4 Conditionally hide the selector when the notebook list is empty
- [x] 1.5 Disable the selector while `ask.isStreaming` is true

## 2. Wire notebook ID into Ask flow

- [x] 2.1 Update `handleAsk` to pass `selectedNotebookId` as the third argument to `ask.sendAsk(askQuestion, models, notebookId)`
- [x] 2.2 Display a dismissible `Badge` showing the selected notebook name next to the model badges (only when a notebook is selected)

## 3. Manual verification

- [ ] 3.1 Verify Ask without a notebook selected works identically to current behaviour (no `notebook_id` in request)
- [ ] 3.2 Verify Ask with a notebook selected sends `notebook_id` in the SSE request payload
- [ ] 3.3 Verify `EvidenceBadge` tooltip renders correctly when the response includes `evidence_metadata`
- [ ] 3.4 Verify the selector is disabled during streaming and re-enabled after completion
