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
