## Branching Rules

* Create the working branch from `main`.
* If `main` does not exist, use the branch explicitly selected by the user.
* Example: `main` → `claude/feature-6318`.
* Create the **first PR to `staging`**.
* If the staging PR has conflicts:

  * Create `conflict/resolved/feature-6318` from `staging`.
  * Resolve the conflicts and merge it into `staging`.
  * Delete the conflict-resolution branch after merging.
* For the production PR, use the **original working branch** → `main`.

**NEVER create a working/feature branch from `staging` if the change will eventually be merged into `main`.** A branch from `staging` is allowed only for temporary conflict resolution.
