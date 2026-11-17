# Security and tenant isolation rules

## Trust boundaries

Organizations are agencies. Brand workspaces are the client data isolation boundary inside an organization. Human identities are global so the same person can accept invitations to different organizations. Organization membership controls agency authority; brand membership controls team, client and service access.

The API database login inherits mi_api, which inherits mi_app, has no ownership and must be neither superuser nor BYPASSRLS. Startup rejects a bypass role. Migration credentials are used only by the migration/bootstrap process. mi_guard is a NOLOGIN role with narrowly granted table privileges; its fixed-query SECURITY DEFINER functions solve membership-policy recursion. Runtime users cannot assume it. Function EXECUTE is revoked from PUBLIC. Untrusted users must never receive database credentials.

The trusted API backend establishes app.actor_id with transaction-local set_config after JWT verification. Worker database logins inherit only mi_app and are pinned to a service principal in mi.service_principals. For those logins, actor_id ignores caller context and uses the database mapping. Workers cannot invoke password lookup or session/invitation functions and do not receive the JWT signing key. HTTP actor/role/tenant headers are not accepted. This context scheme protects against missing query predicates and cross-brand identifiers; it does not protect against a fully compromised backend with stolen database credentials that can deliberately impersonate actors. Keep API/worker credentials separate, restrict database network access and audit their use.

## Permissions

| Principal | Access |
|---|---|
| Founder/admin | All brands in own organization; workspace creation, invitations, intake confirmation and audit |
| Team | Assigned brands; explicit workflow scopes; internal read scope separately assigned |
| Client | Assigned brands; own onboarding, safe questionnaire; later assigned approvals and approved client projections |
| Crawl service | Assigned brand only; workspace/profile/job/workflow reads and crawl/profile/artifact writes; no sessions or human approval |
| Research service | Assigned brand only; confirmed profile reads, research evidence/output/dispatch writes and research workflow updates; no sessions or human approval |

Clients cannot select internal research, profiles, prompts, token costs, raw sources, agent runs, audit logs or other clients' metrics. Later client deliverables live in published_deliverables; clients never select raw agent_outputs. A separate safe projection must be validated before publication is implemented.

Tenant controls use ENABLE and FORCE ROW LEVEL SECURITY on all brand-owned tables, plus per-operation scope policies. Composite foreign keys bind brand_id and organization_id, and child references bind their parent ID to the same brand. Cross-brand references fail even within one agency. Every nested API ID is queried alongside brand_id. Global authentication tables and organization control records are documented exceptions because they are not owned by a client brand.

Scope names are server-owned. Founders/admins can grant team workflow scopes. Client permissions are fixed and cannot be broadened with an invitation. Services are provisioned using the administrative CLI and have no password or interactive login. Never reuse one service principal for unrelated brands unless each assignment is explicitly authorized.

## Authentication

Passwords use Argon2id with 64 MiB memory, time cost 3 and parallelism 2. Access JWTs use a configured 32+ character secret, a fixed algorithm allowlist, issuer, audience, expiry, jti and session ID. Default expiry is 10 minutes; every request also checks that the stored session and user remain active. Refresh tokens are 48 random bytes encoded with URL-safe Base64, stored only as SHA256 digests, expire after 7 days by default and rotate on use. Replay revokes the whole token family, including the successor. Logout revokes the family.

Public founder registration is disabled by default. Bootstrap the first founder through an email-bound activation whose random token is stored only as a SHA-256 digest in server configuration, expires explicitly, and cannot create a second account after activation. The browser reads the token from a URL fragment and immediately removes it from the address/history; credentials and sessions never enter local draft storage. The hidden-password CLI remains available. Remove activation configuration after use. Invitations expire after 48 hours, are stored only as hashes and are returned once for manual delivery; no email is sent. Existing accounts must authenticate to accept their own invitation. Invalid credentials return a common response. Account and network buckets persist login failures for 15 minutes and lock after 5 failures. The API does not trust caller-supplied proxy headers.

Deployment must use TLS, controlled proxy trust, a secret manager, backup/restore controls and an edge request/body rate limit. Password recovery, email verification, MFA, signing-key rotation and membership revocation administration remain unimplemented; implement and review them before broad production onboarding. A database administrator can disable an account immediately, which invalidates active sessions on the next request.

## API credentials and files

Private account API credentials are not accepted through the application. Phase 2 provider keys are deployment-level server environment secrets, never brand inputs or browser data; see RESEARCH_OPERATIONS.md. Tenant-specific credential onboarding remains unimplemented. Integration rows are forced to disabled/readonly; credential tables have no runtime grants. Their future storage contract is AES-256-GCM envelope encryption with a fresh 12-byte nonce, wrapped per-record data key, KMS key/version and associated data binding organization, brand and integration IDs. Nonces must never repeat for a key. Encryption/decryption, rotation and provider OAuth exchanges are future implementations; ciphertext columns alone do not implement them.

Asset object keys must start with brands/{brand_id}/. Uploads are schema/contract only in Phase 1. Future upload completion must verify tenant ownership, size, actual media type, malware scan and digest before marking clean. Store assets privately and issue authorized short-lived download URLs. Never accept arbitrary object keys for download.

## Crawl and prompt safety

Only public HTTP/HTTPS, default ports and exact host/www variants are allowed. Reject userinfo, localhost/internal names, private/reserved addresses, mixed public/private DNS answers and IPv6 tunnels. DNS lookups have short timeouts. Pin the validated IP to the connection while verifying TLS against the hostname; redirects revalidate hostname, address, scheme and robots rules before the next request. HTTPS cannot downgrade to HTTP.

The crawler respects robots.txt, fails closed on inaccessible/denied robot policy and honours bounded crawl delay. It performs GET only, runs no JavaScript, submits no forms and accesses no authenticated content. Bounds: 8 HTML pages, depth 2, 100 queued URLs, 2 MB per response, 10 MB total content and a 60-second crawl deadline with bounded DNS/connect/read timeouts. The job lease is 120 seconds. Failed network work retries at most three times; permanent security failures do not retry. HTTP endpoints in tool logs omit query strings.

Captured website text is untrusted evidence. The Phase 1 extractor uses supplied inputs and page metadata only. It does not infer pricing, audiences, margins or performance. Phase 2 model prompts isolate evidence from instructions, expose only a dossier serialization tool, and validate strict JSON plus captured citation/quote constraints. These checks cannot prove semantic truth, so human research review is mandatory. No model output may select permissions, waive approval, spend money or trigger external communication.

## Audit integrity

All brand mutations, workflow transitions and stored tool calls are recorded by database triggers. Authorized API brand access is also recorded. Authentication events are in a separate append-only global log. Versioned inputs and outputs remain in immutable source tables; audit entries hold identifiers and hashes so credentials and raw private prompts are not copied into logs.

Application roles cannot update or delete audit entries. Each organization has a serialized SHA256 hash chain with strictly increasing timestamps. API request IDs correlate brand actions. Tool logs contain endpoint, method, status, latency and sanitized error; retry attempts are retained on jobs and agent-run models. Approval identity, target version and content hash are bound by constraints/triggers.

This gives application-level immutability and tamper evidence. It cannot prevent a privileged database owner from rewriting storage. Production should export audit checkpoints to independently controlled immutable storage. Define privacy retention and deletion handling separately; workspace deletion is a proposed administrative workflow, not a cascading delete that erases audit history.

## Database operations

Apply migrations with a dedicated owner, in order, through scripts/migrate.py. The runner locks concurrent migrators and records SHA256 checksums in the same transaction as each migration. Never edit an applied migration. There are no destructive down migrations. Rebuild only disposable development databases; production corrections use new migrations and verified backups.

## Phase 2 research controls

Research evidence and exact dispatch inputs are immutable and brand-bound. Model dispatches have guarded state transitions; uncertain usage retains reservations. Reviews bind current dossier/profile and hash, require founder/admin, and cannot be changed after insertion. Clients have no access to raw research, prompts, evidence or costs. Model/provider errors are sanitized. See RESEARCH_OPERATIONS.md for budgets, lease recovery, late-response handling and source limits. No provider call, source URL or recorded test can waive a human approval gate.

## Phase 3 human semantic review

New research approvals require per-claim human assessments covering all cited sources and explicit source-quality, geography, contradiction and coverage checks. Fresh deterministic revalidation binds the dossier to its captured evidence. Area gap resolutions cannot use unrelated registered sources. Migration 018 enforces a minimum semantic-review record on new approvals while preserving old append-only decisions as historical. See RESEARCH_VALIDATION.md for exact API checks and their semantic limits.

## Phase 4 isolation and authority

Planning runs use a separate bound service principal with planning:run and required brand/profile/job/workflow/artifact permissions. It can read current approved research and Phase 4 dependencies, not authenticate humans or approve outputs. Research and planning dispatch mutation policies are separated by job kind; shared budget settlement still permits late research usage after planning begins. Planning outputs/reviews are append-only under forced RLS and same-brand FKs. SQL guards bind current leases, parent snapshots, active human task owners, same-plan earlier-day dependencies and human selection/approval identities. Clients cannot read internal planning records.

Critic verdicts never authorize execution. Blocking issues cannot be waived; material issues need exact-version human risk acknowledgements. Human reviews revalidate current parents, KPI targets, owners and artifacts. The planner cannot assign arbitrary users or increase its budget. Creative copy stays draft and no publishing/account-write tool exists. Phase 5 must add separate server-side client projections and delivery permissions.

## Owner-issued client access

Migration 032 adds dedicated managed client identities and `mi.client_accounts` with forced RLS. Fixed guard-owned functions validate agency administration before creating or managing managed users; the API receives no direct UPDATE grant on password hashes. Clients cannot call owner routes, link additional brand invitations, or update arbitrary workspace columns. Temporary passwords require replacement before brand API access; password changes, resets and suspensions revoke old sessions. Passwords and temporary credentials are excluded from audit payloads.

Browser session restoration uses a Secure HttpOnly SameSite=Lax cookie scoped to `/v1/auth`, a custom same-origin request header and origin checks. Bearer tokens stay in memory. No raw authentication material enters localStorage. Authenticated onboarding drafts use server storage rather than a shared anonymous browser draft. Existing platform Authentication settings are separate from this custom application authentication.

## Hosted public capture

The scheduled route verifies GitHub JWT signatures against the fixed issuer/JWKS, RS256 only, an exact project audience, repository name and immutable ID, main ref, subject, workflow path, event type and expiry. It accepts no target URL, tenant or database credential from the scheduler. The owner route separately enforces owner/admin brand authorization. Both invoke one pass under a separate bound service login, after closing human transactions. A ContextVar scopes the worker connection to its invocation; the shared API configuration is never mutated. Existing public-address, pinned DNS/TLS, robots, page/byte/deadline and durable lease protections remain in force.
