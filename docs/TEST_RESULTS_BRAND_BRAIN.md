# Brand Brain validation — 17 November 2026

The complete backend suite passed **119 tests**, including all 111 previous checks and eight Brand Brain integration checks. No tests were skipped; one existing upstream Starlette/httpx deprecation warning remains. The final full run after the confidence/status adjustments passed in 218.89 seconds.

Verified with disposable PostgreSQL 17 and bound API/crawl service roles:

- Saved partial drafts resume; stale versions fail; incomplete/unsafe URLs cannot be submitted. Social-only workspaces retain a null website and a real supplied-input citation.
- Existing durable capture jobs are used. Before execution, stages are pending and source count is zero. After controlled capture, source-backed chapter drafts appear and unsupported audience/market remains needs_review/unknown. Repeated synthesis is idempotent unless refresh is explicit.
- Finding edits retain original_text and attributed history; stale review sequences fail. Unknown findings cannot be accepted as verified.
- Comments, replies, optimistic resolution and final confirmation persist. Unresolved comments block the selected chapters. Partial/full confirmation records contain actor/time/version/hash and do not overwrite old versions.
- Restoring a version creates a new draft and preserves the current confirmed snapshot. Canonical promotion reuses the original critical-input/human approval gates; missing budget/audience inputs cannot be bypassed by confirming a Brain.
- Clients assigned to the brand can read/collaborate but cannot accept as founder-confirmed, make the final confirmation or access other brands/nested IDs. Unauthenticated calls fail. Existing service identity/tenant isolation tests remain intact.
- All 30 migrations apply to a fresh database; a second pass performs no changes and validates checksums. All seven new tables have enabled and forced RLS.
- Optional same-origin UI mount serves the packaged HTML/JS/CSS, omits configuration files, and leaves application API routes authenticated.

Frontend: **31 Node tests passed**, including the prior 23 plus setup/link validation and a credential-free backend availability probe, safe platform detection, recoverable draft persistence, combined review filters, honest progress/empty state, bearer refresh flow and unavailable-API error recovery. Build validates module and standalone-script syntax. Existing editable PowerPoint and client export checks still pass.

Research/model outputs in tests are controlled fixtures; these checks do not establish live source access, provider quality, factual truth or a successful SuperYou pilot. No paid call was made. The hosted Sites preview remains disconnected from the application API. Browser rendering, mobile/keyboard/accessibility QA and the prior 10-second client comprehension goal remain unverified because the managed browser-control tool is unavailable. Existing later-stage workflow lifecycle gates may reject a new research cycle; the UI reports the conflict and retains confirmed data rather than resetting work or budgets.
