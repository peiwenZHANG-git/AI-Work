---
name: ai-work-delivery-check
description: Validate a significant AI-Work change against repository rules, verification evidence, and project-state synchronization before delivery or completion.
---

# AI-Work Delivery Check

Use this workflow after implementation and before saying a significant development task is complete, before proposing its final report, and before committing when the user requested a commit. Re-check the finished work; do not rely only on the task-initialization snapshot.

## Workflow

1. Inspect the live change with Git: status, branch and remote relationship, staged and unstaged diffs, and untracked files. Review the actual patch for scope, accidental edits, debug output, generated files, secrets, credentials, URLs containing session material, and unintended behavior.
2. Re-read the current `AGENTS.md` and confirm the implementation respects its compatibility, code organization, safety, mailbox, dependency, generated-file, and verification rules. For anything not covered by the implementation, state the remaining risk instead of claiming an unchecked property.
3. Verify behavior at the level of risk:
   - Python code and executable configuration: run the syntax and full side-effect-free test checks required by `AGENTS.md`, plus any interface-registration or repository-specific check it requires.
   - Documentation, process, or Skill-only changes: validate structure, internal links, reachable file paths, and whitespace. Full Python tests are required only when Python behavior or test execution could be affected; if omitted, say why.
   - Targeted changes: run focused tests in addition to required baseline checks when they materially increase confidence.
4. Run a diff hygiene check such as `git diff --check`. Do not use checkout, merge, rebase, reset, clean, or another destructive operation to make problems disappear.
5. Decide whether any fact recorded by `PROJECT_STATE.md` changed: implementation, architecture, completed features, public interfaces or MCP tool surface, dependencies or runtime environment, safety boundaries, known issues or blockers, verification baseline, current focus, or next steps. If yes, update only those facts from the actual diff and latest verification evidence, and refresh its last-updated date. If no recorded fact changed, do not edit it merely for formality.
6. Confirm that dynamic Git facts remain out of `PROJECT_STATE.md` and the final report's durable claims. Report live status separately from the state snapshot.
7. Return exactly one result:
   - `PASS`: required checks passed and project-state synchronization was correctly applied or correctly judged unnecessary.
   - `PASS WITH MANUAL CHECK`: automated checks passed, but a clearly named real-desktop, real-mailbox, real-browser, external-service, or platform-specific check remains and requires explicit authorization.
   - `BLOCKED`: required evidence cannot be obtained without authorization or an external dependency.
   - `FAIL`: a required check failed, the patch violates a repository rule, or a recorded project fact is out of sync.

Never describe a skipped prerequisite, timeout, mocked prerequisite that should be real, or unknown result as a pass. Preserve any `MANUAL CHECK` instruction in the delivery report.
