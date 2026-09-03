---
name: ai-work-task-init
description: Initialize a significant AI-Work development task from the current repository state before implementation. Use for coding, architecture, refactoring, security, migration, release, or other repository-changing work.
---

# AI-Work Task Initialization

Initialize from the repository, not from chat memory. `PROJECT_STATE.md` is the current-state snapshot, `AGENTS.md` is the repository contract, and Git plus the working tree are live facts. This skill stores no project state and does not replace either document.

## Workflow

1. Resolve the current AI-Work repository. Prefer the active local checkout; otherwise use an authorized repository connector, Raw content, public repository content, or user-provided files in that reliability order.
2. Read the current working-tree `PROJECT_STATE.md` first. Treat it as the context snapshot, not as evidence of live Git status. If the local checkout is unavailable, read the repository's current documented state through the best available repository access path.
3. Read the current `AGENTS.md` next. Apply its repository facts, compatibility, code organization, safety, mailbox, verification, and generated-file rules to the requested task.
4. Check Git in real time before deciding how to proceed. Use read-only or low-side-effect operations such as `git status --short --branch`, current branch and HEAD checks, comparison with `origin/main`, and, when needed, `git fetch origin main`. Never use fetch as an opportunity to checkout, merge, rebase, reset, or clean.
5. Identify existing staged, unstaged, and untracked changes that overlap the requested work. Treat them as user or prior-task work. Preserve them unless the user explicitly asks for a change to those exact edits.
6. Extract only the constraints that affect this task: preserved public interfaces, safety boundaries, identity and confirmation rules, dependencies, test requirements, and known blockers. If the request conflicts with the repository contract, stop and identify the required explicit approval or scope change.
7. Choose execution proportional to risk. A simple, local, low-risk change may need only its target, key constraints, and verification. A multi-file, architectural, data, security, release, permission, or hard-to-reverse change needs target scope, steps, dependencies, risks, acceptance checks, and a rollback approach before edits.
8. Report a concise readiness result before implementation:
   - `READY`: current facts are verified and implementation can proceed.
   - `READY WITH CONSTRAINTS`: proceed only within the stated repository or user boundaries.
   - `BLOCKED`: current state cannot be verified, required approval is missing, or the request conflicts with a safety or compatibility rule.

Do not invent current implementation status, MCP tool counts, branch names, HEAD hashes, dirty-state facts, or completion progress. Read them from the current documents and Git when they matter.
