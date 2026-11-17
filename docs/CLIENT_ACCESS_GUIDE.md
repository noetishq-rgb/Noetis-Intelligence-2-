# Running NOETIS Brand Brain

The entire live app is Brand Brain. The owner manages access in `/owner`; clients sign in at `/login` and land in their assigned `/brain` workspace. The owner never has to complete a client’s brand questionnaire.

## Onboard a new brand

1. Open the Owner Console and sign in with your existing owner credentials.
2. Under **Create client access**, enter a brand/workspace label and the contact’s email. Add their name if helpful. No website, category, audience, objectives or brand research is required from you.
3. Leave **Temporary password** blank to generate a strong password, or enter at least 12 characters.
4. Select **Create brand login**. Copy the login details and share them privately with the client. They appear once; the app cannot retrieve a password later. No email is sent automatically.
5. The client opens `/login`, enters their email and temporary password, chooses their own password, and lands directly in their Brand Brain.
6. The client fills in their brand name, online presence, category, objectives, business description, audience, target markets, one priority objective and monthly marketing budget in INR. Partial drafts save to their assigned workspace. Submission also creates the detailed research intake with the client recorded as its respondent. The marketing budget is separate from the Claude pilot allowance.

Each managed login has a distinct email address and is dedicated to its assigned brand. Existing emails cannot be overwritten or silently assigned to a different client. If several people need access, arrange separate identities rather than circulating a shared password.

## Manage access

- **Reset password:** issues a new temporary password once and revokes all existing sessions. The client chooses a new password at their next sign-in. Resetting does not reactivate suspended access.
- **Suspend access:** prevents sign-in and invalidates current sessions. The brand’s data remains stored.
- **Restore access:** permits sign-in again. The client uses their current password, or you can issue a reset.
- **Open Brand Brain:** reviews the client’s saved draft or submitted setup. There is no need to enter those details on the client’s behalf.

Owner controls require a founder/admin session and cannot be used by clients. Do not share owner credentials with clients. Manage this app’s accounts in the Owner Console; its authentication is separate from Supabase’s platform login and the older dashboard’s Auth users.

## Where the data lives

| Service | What it stores or runs |
| --- | --- |
| Supabase PostgreSQL | Private account records, password hashes, brand workspaces, drafts, submitted inputs, version history and audit records. |
| GitHub | Application source code, migrations, tests and deployment history. No client passwords or database credentials belong here. |
| Vercel | The hosted login page, Owner Console, Brand Brain and protected backend API. Server configuration stays in environment variables. |

In Supabase’s Table Editor, the new app uses `mi` for brand data, `identity` for accounts/sessions and `mi_meta` for migration bookkeeping. Existing `public` and `private` schemas belong to the older application and remain separate. `mi.brand_onboarding_sessions` contains client drafts/submissions; `mi.client_accounts` links issued access to a brand; `identity.users` contains password **hashes**, which cannot be used as readable passwords.

Use the Owner Console for routine account changes. Avoid editing identity, membership, workflow or audit rows manually: those changes can bypass expected application checks. Use reviewed migrations for schema changes.

## Returning clients and reliability

The client uses the same `/login` URL each time. A Secure, HttpOnly browser cookie restores their session across page reloads; bearer tokens are held only in page memory. Access tokens last 10 minutes and rotate through the session. The refresh window is 7 days by default. After sign-out or expiration they enter their credentials again, and the database restores their assigned draft or submission.

Access is checked on API requests, not only when the login form is submitted. Cross-brand IDs do not grant access. Resets and suspensions revoke active sessions. Private onboarding inputs are not stored in a shared anonymous browser draft.

For production operation, keep Supabase and Vercel active, monitor `/health`, and check the backup/restore policy included with the selected Supabase plan. This app does not promise uninterrupted availability or configure an independent backup service. A paused database, provider outage or expired session needs recovery or sign-in; it does not erase the client’s saved setup.

The production login page must be reachable without a Vercel account. Production data remains behind application authentication; preview deployments remain protected separately. Share the stable production domain, not a temporary Vercel sharing link.

## Research and integrations

Creating a client or submitting their setup performs no marketing API or model call. The submitted screen explicitly says that research has not started. Model providers, source collection, worker identities and Meta/Google/email connections can be configured later without changing the client login process. Existing research approval and spending gates remain in force.

## Start public website capture

For a configured pilot brand, open its Brand Brain as an owner. Enter the official website in **Start with public website evidence**, then select **Queue website capture**. Use **Capture status** to inspect progress and sources. **Run worker now** performs one bounded pass immediately; the signed GitHub schedule also checks every five minutes and may be delayed by GitHub. Draft onboarding remains editable and unknown facts stay blank. Once the client completes and submits setup, the owner can organize the captured evidence into a review draft. This performs no model or marketing API call.

After capture, open **Capture status** and review the **Research intake review** panel. **Confirm research profile** advances the selected current client submission through the existing profile approval checks. It does not approve research findings. **Run research pilot** then invokes the configured one-generation worker and links its real job back to this session so the client sees actual research status. Clients cannot confirm intake or start paid jobs. A successful dossier still needs evidence and human quality review; a larger research programme requires more sources and a separate allowance.

An administrator may use `scripts/reset_test_brand.py` to render a reset for an explicitly requested, idle pre-research test workspace. It refuses model dispatches, research/planning jobs and approvals. Execution requires database administration privileges; it is not a web endpoint. Source snapshots, draft artifacts, sessions and test inputs are deleted transactionally for the exact brand, with FKs and audit recording active. Login accounts, memberships, service bindings, system templates and audit hashes are preserved. The next page load creates an empty onboarding session.
