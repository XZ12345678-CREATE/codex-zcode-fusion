---
name: codex-zcode-fusion
description: Let Codex plan and review while the user's installed ZCode desktop agent executes a bounded coding task in a staged copy. Use when the user asks for Codex to direct ZCode or explicitly invokes this Fusion workflow.
---

# Codex → ZCode Fusion

Codex is the Lead; the existing ZCode desktop agent is the Sidekick. This version uses the native ZCode UI through Computer Use and a local task bridge. It does not require a separate OpenAI API key. Headless ZCode authentication is not configured by this skill.

The plugin root is the parent of the `skills` directory (`Path(skill_file).parents[2]`). Read the root `README.md` for usage and the root `docs/WORKFLOW.md` for the exact bridge schemas (handoff package, result.json contract, locking, apply journal, resume decision table). Use the root `fusion.py` through the shell, or its `fusion_*` MCP tools if already connected. Quote absolute filesystem paths in shell commands. Set `FUSION_TASKS_DIR` to a writable directory in the user's project for every invocation, and keep that same directory throughout a task; do not write task state into an installed plugin cache.

## Plan and dispatch

Capture the user's original request, a short implementation brief, unique hard requirements, an explicit list of editable files, any context files, and useful acceptance commands as argv arrays. `prepare` creates the task copy and `brief.md`; then `handoff` returns a paste-ready prompt, the current round's token, and the fixed result path.

Use `cua_repl` to select ZCode, inspect its current state, and create a separate execution task when needed to avoid overwriting the user's unrelated chat or draft. Follow the Computer Use documentation for fresh accessibility indices. Send ZCode the handoff prompt (it points at the brief, the work directory, and result.json instructions):

> Read the task brief and execute only inside its work directory. Follow its allowed-file scope and acceptance requirements. Write the structured result atomically to the given result.json path (the only output allowed outside the work directory). Do not modify the source workspace, unlisted files, account settings, or credentials. Return a summary of changes, checks, and remaining issues to this ZCode task. Codex will independently verify and review.

If pasting fails or drops characters, save the complete generated packet unchanged to a temporary file with a short ASCII path and send a verified ASCII instruction to read it. Confirm ZCode reads the correct packet before recording dispatch; preserve the original paths and correlation fields inside the file.

A click succeeding is not a dispatch. Before recording anything, verify the paste actually took: re-read the full input box text and compare it against the intended prompt — including non-ASCII characters and paths with spaces or CJK characters, which have been lost or dropped in practice. Only after actually sending, confirm the session shows the message and ZCode has begun reading the brief; then record it with `dispatch --session-label LABEL` (or `fusion_dispatch`, which requires the current handoff and is idempotent for the same token and label). The bridge never sends UI events itself; it only records the fact you report. If the input cannot be verified, re-enter it — do not record a dispatch you have not confirmed. When Computer Use is unavailable, keep a manual handoff: give the user the paste-ready prompt and the handoff path, ask them to send it in the ZCode session, record the session label they confirm, and report exactly which automated step was unavailable. Use the existing chosen model and permission mode. Do not change authentication, model subscriptions, security controls, or global settings. A successful send is not completion; inspect the resulting task state and files.

## Recover, verify, review, and rework

After ZCode writes result.json, import it with `result` (strictly validated, stored as untrusted evidence — it never approves the task). Then run `verify` and inspect the actual diffs and independent command results. For every requirement, supply a pass/fail decision with concrete nonempty evidence. If ZCode edited existing tests, evaluate that change before explicitly acknowledging it. Passing commands alone do not justify approval.

If a session was interrupted, use `list` and the read-only `resume` to recover the task, its session labels, evidence, and the recommended next action; never re-dispatch or re-apply automatically — wait for or manually check the ZCode session instead.

For defects, use `retry` with specific feedback; it keeps the same work copy and issues a fresh handoff token. After two rounds, reassess with the user; do not run an unbounded retry loop.

Only run `review` with passing decisions when the evidence supports them. `apply` writes files one by one under a persisted journal with before-backups. If a previous apply failed or was interrupted, the journal blocks blind retries: manually inspect the workspace and backups, then continue with `apply --confirm-recovery` (no multi-file transaction, rollback, or power-loss consistency is promised); while the journal is active, retry/handoff/dispatch/result/verify/review are all refused. Apply an approved change when the user's original request authorizes those file edits — do not re-ask the user before every apply or recovery step when the existing authorization and evidence already cover it; when they do not, ask once and record the decision.

## Boundaries and reporting

The task copy, application checks, and the bridge's process lock are workflow controls, not an operating-system sandbox; the lock only serializes bridge calls and does not constrain ZCode writing files directly. ZCode retains its own existing process permissions. Never claim programmatic restriction of the Lead's tools or measured token savings.

If Computer Use or ZCode is unavailable, finish the brief and staged task, then report exactly which dispatch step is unavailable. Do not silently replace ZCode with an API client, another model, or direct Codex implementation under the name Fusion.

Report the delivered files, independent verification result, whether changes were applied, and material remaining limitations. Identify ZCode as the executor and Codex as the reviewer.
