---
name: model-routing
description: Choose and execute useful model delegation in Codex, especially Claude Code implementation or review and Gemini corpus analysis or research. Use for explicit model-routing requests or substantial tasks where a bounded specialist adds value; keep trivial work local.
---

# Model Routing

## Core Principle

Route by the **current phase**, never by a permanent model hierarchy.

**MODEL ROLE IS TEMPORARY. Human override always wins.**

Use [routing-reference.md](routing-reference.md) for capability priors, scoring, reviewer mapping, and output contracts. Before an actual delegation, read [codex-delegation.md](codex-delegation.md) for the verified tools, identifiers, authorization boundaries, and task lifecycle.

The current Codex assistant remains the manager: it plans, dispatches bounded work, checks results, and answers the user. A temporary phase lead is a specialist, not a change to the current chat's model. Never claim that choosing a name in a routing block changed the runtime model.

## Decision Flow

1. Decide whether routing adds distinct value. For trivial/local work or when the current model already fits well, do not delegate.
2. Profile the phase across relevant dimensions: scientific reasoning, experiment design, model architecture, ML engineering, debugging, repo engineering, deep research, dataset forensics, multimodal analysis, long-context synthesis, result interpretation, and tool use.
3. Check callable tools and model availability, then select the temporary phase lead using task fit plus context continuity and handoff cost. A model preference is not evidence that its transport works.
4. Identify the `critical_uncertainty`. Select a reviewer for that uncertainty, not merely the second-highest-scoring model.
5. Add specialists only for bounded tasks with distinct information gain.
6. Assign one write owner for each active changeset/experiment. Dispatch through a verified, authorized route; do not stop at recommending delegation when it is useful and executable.
7. Retrieve the actual result, inspect changed files and test evidence, then integrate the result. Reassess only after a material phase change.

Do not switch for a marginal score advantage.

## Delegation

Every delegation must specify:
`objective`, `context`, `scope`, `expected_output`, `constraints`, `permission`, and `stop_condition`.

Permissions are `read-only`, `patch-proposal`, or `write`.

Specialists are `read-only` by default.

Use the Claude MCP connection for actual Claude work: `ask_claude` for analysis of supplied context, `start_coding_task` for project edits, and `get_coding_task` to await results. Claude's coding connection cannot execute shell commands; the manager runs tests and sends failures back using the returned `session_id`.

For Gemini analysis/review, use the installed tool-free worker `scripts/gemini_delegate.py` through the authenticated local Codex router. Supply a bounded task and the relevant text via `--prompt-file`; the script validates the actual Antigravity model and completed reply. This is the primary verified delegation route. It does not create a Codex chat, load its full history/tool catalog, or execute specialist tools. See `codex-delegation.md` for the command, scope and limits.

The previously authorized reusable Gemini chat is retained in [delegation-target.json](delegation-target.json). Its `call_id` bug and catalog-size failure are repaired; a real chat turn, tool-description retrieval through `functions.exec`, and the final answer were verified on 2026-10-17. Prefer the bounded worker for supplied-text analysis; use the authorized chat when the task benefits from Codex-managed tools or chat continuity. Unarchive that destination before reuse when needed. Large MCP descriptions are abbreviated; retrieve their full descriptions and argument declarations from `ALL_TOOLS` before calling them. Native context limits still apply; split or summarize oversized material instead of retrying an unchanged failure. Creating or messaging chats still follows the tool's explicit authorization requirements. The separate legacy `gemini` CLI and ordinary OpenAI endpoint are not this integration. Never pass Claude/Gemini aliases to the inspected OpenAI-only native subagent API.

Honor existing authorization without asking again. If a required route is unavailable, report the specific blocker and continue independent manager work. Do not silently substitute another model for one explicitly required by the user, bypass a denial, or invent a successful response.

**NO DELEGATION WITHOUT DISTINCT VALUE.**

Maximum nested delegation depth: `2`. If another specialist is needed beyond that, return a recommendation to the lead.

## Write Ownership

**ONE ACTIVE CHANGESET = ONE WRITE OWNER.**

For ML work, treat the experiment ID as the ownership unit.

Parallel writes are allowed only for independent, non-overlapping work in isolated branches/worktrees. Never let multiple agents modify the same active experiment in one working tree.

## Review Gate

Independent review is required for high-impact decisions: model-architecture changes, scientific-hypothesis changes, expensive GPU runs, irreversible dataset transforms, evaluation-protocol changes, conflicting evidence, final/high-stakes submissions, or low routing confidence.

Before an expensive experiment, verify:
1. exact hypothesis;
2. falsifying observation;
3. whether a cheaper probe can answer the question first.

Block the full run when it does not isolate the hypothesis or a cheaper diagnostic can provide the needed information.

## Evidence and Reassessment

When models disagree:

`measured experiment > dataset/repository evidence > scientific literature > reasoned inference > model intuition`

Never decide by model vote when stronger evidence exists.

Reassess routing after root-cause discovery, experiment design/completion, implementation completion, unexpected results, evidence conflict, scope/category changes, or specialist blockers.

Stop agent ping-pong after two same-task handoffs or one repeated model-pair round-trip; return `routing_stalemate: true` for human arbitration.

## Output

Do not emit a large routing block for trivial tasks.

For normal work, a concise, accurate decision is enough:
`Manager: current Codex model. Coding: Claude via start_coding_task. Tests: manager.`

Distinguish `recommended`, `started`, `running`, `completed`, `verified`, and `blocked`. Report requested and actually reported model IDs separately; when the transport does not report the serving model, say so. A model's self-identification is not transport evidence. Completion means the specialist returned; verification requires evidence from the manager.

When explicit routing details are requested, use the full contract in `routing-reference.md`.

## Red Flags

Re-evaluate if agents duplicate the same task, switch for tiny score differences, review their own high-impact decisions, share write ownership on one experiment, launch costly runs without falsifiable hypotheses, bounce work between models, or treat consensus as evidence.
