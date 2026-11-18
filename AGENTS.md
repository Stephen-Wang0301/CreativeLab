# CreativeLab project rules

- Work in this project directory. Do not move or rename it for ordinary updates.
- Local edits, server deployments and Git milestones are separate operations.
- Do not create commits, tags, GitHub releases, or push changes without the user's explicit instruction to save or publish that stage. Show the concrete change summary first. Do not auto-commit each edit or each chat.
- Server deployments require the user's instruction to update the test site; deployment does not imply permission to create a Git commit. Use `python deployment/manage.py deploy --working-tree` for uncommitted tests, or `deploy --ref <commit-or-tag>` for saved versions.
- The live server `data` is authoritative. Code deployment must not upload local `data` or overwrite server records, drafts, keys or tunnel configuration. Browser protocol edits are database changes, separate from code versions.
- Root `README.md` is a private operator guide and may contain credentials. The public GitHub README is `.github/README.md`.
- Keep data, backups, logs, `.runtime`, `private`, credentials, machine settings, generated inventories and binary runtimes out of Git, including its history. Run `python deployment/manage.py check` before any commit or push. Hooks are a guard, not a substitute for reviewing changes.
- Use the independent Git repository rooted here; do not stage files from the parent `PhDWorking` repository.
- Use `deployment/manage.py` for controlled deployments: verify the server code baseline, back up data, deploy only allowed code paths, restart only the experiment service, and verify health. Do not deploy over unexplained server edits. Data restore requires a separate explicit decision.

## Agreed conversation commands

These are conversation instructions, not the removed BAT menu numbers. A request to discuss a command is not authorization to execute it. Use the current task's scope and explicit choices; do not infer a Git save from a code edit.

1. Keep the server protocol draft; do not publish it.
2. Publish the server protocol draft as the next experiment version. Pause new enrollment, publish, then restore the prior enrollment state. Existing participants retain their original version.
3. Edit and preview code on the user's computer; no Git commit, push or server deployment.
4. Save the approved code stage as a Git commit and push to GitHub; no deployment.
5. Deploy the current code for testing; do not create a Git commit or push.
6. Save and push the approved code stage, then deploy that same commit.
7. Restore a specified Git code version; require the version and target (computer, server, or both). Preserve unsaved work and later Git history. Do not restore experiment data implicitly.
8. Download and SHA-256 verify a complete private server backup, including every experiment version, quick-demo data, unpublished drafts and credentials. Never put it in Git.
9. Fetch server code into a private comparison directory, review and merge as requested; do not overwrite computer source automatically.
10. Roll back server code by deployment ID, including uncommitted test deployments; leave data in place and check compatibility.
11. Restore a specifically identified protocol or dataset snapshot. State the object and time; do not silently overwrite newer participant records.

Commands may be combined, such as 2+6. Check protocol/data compatibility and required dependency/configuration migrations before deployment or rollback. Use “server” and “your computer” explicitly, avoiding ambiguous “local” for server writes.

## Experiment data catalog

- Display versions have three integer components: stage.content.feature (current 0.1.1). Stage 0 is pilot and 1 is formal. Publishing changed materials/process increments content; feature-only releases increment the feature component in study/catalog.py without publishing drafts or allocating a new content batch. Git commits remain separate.
- Keep stable two-component content batch paths (e.g. batches/pilot/v0.1), revision IDs and aliases across feature updates. Immutable protocol/identity files must not be rewritten just to change a display label. Add feature manifests under releases/ and retain enrolled/last-used release provenance in responses and exports. Filters group patches of one content version together.
- Existing opaque participant IDs, cookies, recovery hashes, answers and frozen materials must survive migration. Use readable aliases S0-V0001-P000001 (Q- prefix for the isolated quick-demo dataset); never renumber existing records or move them between versions.
- Keep the authoritative SQLite index, immutable batch/protocol snapshots, and separate per-version participant directories. Keep legacy directories as migration evidence. Code deployment must not merge versions or replace the user's computer data over the server data.
- Stage/version filters apply consistently to overview, participants, raw exports, ratings and analysis. Enrollment caps are per published version.

## Participant access and recovery

- Display only the participant alias, without a redundant short invitation number. Preserve legacy labels and signed links. One invitation binds one attempt; distinct invitations allow repeated participation. Reservations do not count as enrollment; hide registered invitations from the pending list and paginate unused ones ten at a time. Continuation links in participant details must not perform recovery or change timing.
- Common invitations admit first-time browser sessions to the current published content version. Existing sessions keep their old attempt. For shared browsers or repeated participation use separate personal links.
- Pause saves draft and remaining time atomically on the server, masks/inerts the task and survives reload. Continue preserves phase, assignments and remaining time; archive-and-exit remains paused, not withdrawn. A pilot restart requires a server-enforced ten-second challenge, archives the old attempt and reserves a new invitation using the same transactional allocator as all other admissions. Never delete answers or recycle participant numbers. Formal self-restart is disabled pending research approval.
- Retain pause durations/effective timing and restart lineage in events/exports. Reject code rollbacks that cannot read live paused/restarted states; never solve code incompatibility by silently restoring or deleting data.
- Scope participant cookies and tab storage to the invitation. Preserve legacy shared links and old recovery codes. Never let an existing cookie silently redirect a new personal invitation into another attempt.
- Recovery requires an explicit researcher action, a current serial and a reason. Retain a complete pre-recovery state snapshot, original timeouts, work, condition assignment and version. Record intervention flags and timer changes. Keep completed and intentionally withdrawn attempts sealed; issue a new invitation for another test.
- Adding recovery/invitation functionality does not authorize automatically reopening real participants or generating production test records. Use isolated data for interaction tests.
- Use one compact dashboard read per tab switch; do not drop or optimistically fake participant saves for perceived speed. Keep protocol editors fresh rather than editing cached drafts. Reject late responses from older navigation requests.
