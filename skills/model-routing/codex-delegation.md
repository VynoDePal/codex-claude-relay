# Executable delegation in this Codex installation

Inspected on 2026-10-17. Recheck live tool descriptions before use; availability can change. Prefer the exposed MCP tools over launching a separate CLI. This reference does not grant permissions beyond the user's task or the tool's own requirements.

## Routes and identifiers

| Work | Callable route | Model argument | Actual capabilities |
|---|---|---|---|
| Claude analysis, review, patch proposal | `mcp__claude_code__ask_claude` | `opus`, or the user's explicit supported ID | Supplied text and its own conversation only; no files, shell, web, or MCP tools |
| Claude implementation | `mcp__claude_code__start_coding_task`, then `mcp__claude_code__get_coding_task` | `opus`, or the user's explicit supported ID | Reads/edits the explicit project; no shell; manager executes tests |
| Gemini bounded analysis/review (primary) | `python3 scripts/gemini_delegate.py --prompt-file <task.txt> --effort high` | `gemini-4-argon` through authenticated loopback | Supplied text only; technically no tools; checks actual model metadata |
| Gemini in an explicitly authorized Codex chat (verified 2026-10-17 after catalog compaction; unarchive first) | `mcp__codex_app__send_message_to_thread`, then `mcp__codex_app__wait_threads` | `gemini-4-argon`, thinking `low`, `medium`, or `high` | Local router to Antigravity; text input, Codex-managed tools; supply context and scope explicitly |
| New Gemini chat when the human explicitly requests its creation | `mcp__codex_app__create_thread`, then `mcp__codex_app__wait_threads` | `gemini-4-argon`, thinking `high` by default | Creates a visible, user-owned chat; not an invisible subagent |

`opus` is the Claude connector's alias. The live coding probe reported `claude-opus-5-5`; do not send the old document label `claude-opus-5.5` as an invented API ID. The Codex chat model ID `claude-code-opus` belongs to the chat tool, not the Claude MCP connector.

The native `collaboration.spawn_agent` model list in this installation contains OpenAI models only. Do not pass Claude/Gemini aliases to it. GLM/DeepSeek use the separate `gpu_agents` connector under the user's AGENTS.md; that connector does not provide Claude or Gemini.

## Claude: analysis and independent review

Call `ask_claude` with `model: "opus"` and a self-contained prompt containing the delegation contract. Include only the relevant authorized diff, source excerpts, data summaries, or documents as text; a path alone does not give this connection file access. The user's request to enable useful delegation covers this workflow; it does not expand the underlying task or authorize unrelated external actions.

```json
{
  "model": "opus",
  "prompt": "Objective: review the supplied change. Context: ... Scope: ... Expected output: concrete findings with evidence. Permission: read-only. Constraints: use only the supplied material; do not delegate. Stop condition: return findings and uncertainties. Material: ..."
}
```

Keep the returned `session_id` and pass it on follow-ups. Inspect error flags and the actual reply, plus `requested_model` and `models_used` when returned. Attribute the opinion to Claude; the manager still evaluates it. The MCP call itself is synchronous and can take up to 300 seconds. If `functions.exec` yields `Script running with cell ID ...`, use `functions.wait` with that exact `cell_id`; this resumes the wrapper, not a Claude task. Use `functions.wait` only after such a yield. If the wrapper actually fails without a resumable cell, report an unknown/failed result rather than inventing a task ID or launching a duplicate request.

## Claude: coding lifecycle

1. Identify the absolute user-authorized project directory and record existing changes. Choose an isolated worktree first if separate concurrent coding is needed. The connector edits the directory provided; it does not create a worktree automatically.
2. Call `start_coding_task` with `project_path`, `model`, and a self-contained `prompt`: requirements, relevant files, ownership, constraints, acceptance criteria, proposed verification commands, and stop condition. No Codex history is shared automatically. Explicitly prohibit subdelegation for a bounded worker.
3. Retain both returned IDs. Poll `get_coding_task` with the `task_id` and `wait_seconds` up to 20. A pending response is normal; continue independent work and avoid rapid polling. Choose an overall wait budget appropriate to the task; if it is exhausted without useful progress, report the stalled task and ask whether the user wants it cancelled. Do not edit the project while Claude is still writing.
4. On completion, inspect the diff and any `permission_denials`. The `completed` state is not test evidence. Run appropriate tests using the manager's normal execution permissions. Use `codex-heavy` for memory-heavy work as required by the user's AGENTS.md.
5. If fixes are required, call `start_coding_task` again with the same `project_path` and returned `session_id`, plus concrete feedback and failing test output. This continues Claude's context. Wait and verify again.
6. Use `cancel_coding_task` only when the user requests cancellation, as its tool contract requires. Cancellation or failure can leave partial changes: inspect them before continuing.

Do not grant shell access by switching to a more permissive CLI to avoid a denial. Claude may write tests, but must not claim it ran them through this connector.

## Gemini: bounded analysis through the local router

No dedicated Gemini MCP delegation tool was exposed during inspection. The model is available through the user's local `codex-model-router` service on loopback port 4012. Its `gemini_relay.py` invokes `~/.local/bin/agy`, which is the installed Antigravity CLI. This is distinct from the older `gemini` CLI.

The primary worker and the previously human-authorized reusable chat are recorded in [delegation-target.json](delegation-target.json). Both routes were verified on 2026-10-17. The earlier full-chat failure, `maxMessageTokens must be positive, got 0`, was repaired by compacting the nested MCP tool catalog. The chat now completes a real tool-description retrieval and a final analysis. Use the bounded worker below for routine supplied-text analysis; use the authorized chat when Codex-managed tools or chat continuity add value. Keep the chat and its history, unarchive it before reuse when needed, and honor its existing human authorization.

Catalog compaction preserves core tool descriptions and replaces the largest nested MCP sections with brief entries until the description budget is about 250,000 characters. Before calling an abbreviated tool, retrieve its complete description and argument declaration inside `functions.exec`, for example `text(ALL_TOOLS.find(t => t.name === 'TOOL_NAME').description)`, then obey that full contract. The relay refuses catalogs that remain above 380,000 characters; the separate conversation limit below also still applies. Catalog compaction does not make conversation input unlimited or grant additional tool permissions.

The adapter selects `gemini-4-argon-low`, `gemini-4-argon-medium`, or `gemini-4-argon-high` according to `thinking`, and checks Antigravity's initialization event for the exact model and the exact per-request agent name. The temporary profile copies the validated finish-only `codex-gemini-bridge` restrictions and is removed after the request. A live authenticated loopback probe returned `metadata.antigravity_model: gemini-4-argon-low` and the expected response. The chat tools themselves do not promise a `models_used` field: distinguish requested chat settings, directly observed transport metadata, and model self-identification.

This route currently accepts text, not direct image/audio attachments. Supply authorized transcripts, extracted text, or data summaries; do not claim Gemini saw an image merely because its model-family capability prior includes multimodal analysis. The native Antigravity profile can only deliver the structured response. The generic chat adapter can request client tools that Codex executes under its own permissions; the primary worker disables all such tools; do not relax this profile or enable native shell/MCP access.

### Primary route: a bounded analysis worker

Use the script installed with this skill; it uses the already configured local router, not a new provider login or Codex chat. The manager writes a UTF-8 file under the current task's `work/` directory containing `objective`, `context`, `scope`, `expected_output`, `constraints`, `permission: read-only`, `stop_condition`, and the authorized material. Paths alone do not give the specialist file access.

```bash
python3 ~/.codex/skills/model-routing/scripts/gemini_delegate.py \
  --prompt-file /absolute/project/work/gemini-task.txt --effort high
```

The script also accepts task text on stdin. It defaults to a 600-second network timeout; use an ordinary yielding command invocation and retain its execution session if still running. Resume the same command rather than launching duplicate inference. It never follows HTTP redirects or reads proxy settings: Codex authentication is sent only to `127.0.0.1:4012` and is not printed.

The worker sends `tools: []` and `tool_choice: none`. It cannot read/edit files, execute commands, browse, delegate, or send messages. It supplies only the current task, with no copied Codex tool catalog or automatic history. For a follow-up, supply the relevant previous reply and new question explicitly. This limits cost and prevents unrelated cross-task context from accumulating.

The returned JSON must contain `status: completed`, a nonempty `reply`, `tool_calls: 0`, and `observed_model: gemini-4-argon-<effort>`. The script checks that metadata against the requested effort. The manager must still assess the substance: `completed` is not scientific verification. If the command fails, report the error; do not claim Gemini answered or silently substitute another model.

Limit supplied material to 100,000 UTF-8 bytes per request. Split larger corpora into purposeful excerpts or batches, synthesize their findings, and retain citations/record IDs. The underlying adapter separates trusted instructions from user/history data and rejects conversations over 160,000 bytes. It also requires a fresh integrity token from the end of the native input before accepting any answer or client tool request. These limits are separate from the model's advertised token window. Do not retry an oversized full chat unchanged.

### Resolved `call_id` failure

Codex creates inter-chat deliveries as named `function_call_output` items with an `id` such as `fco_...` but no paired `call_id`. The shared Claude/Gemini normalizer previously passed them to a converter that indexed `item['call_id']`, failing before Antigravity started. On 2026-10-17 the normalizer was repaired without inventing a tool call or changing valid paired calls. A strict Codex app delivery envelope becomes an actionable delegated assignment with its source preserved; it does not grant new human authorization. Other standalone outputs remain explicitly untrusted source context. The router service was reloaded and the live transport probe succeeded. The subsequent catalog compaction repaired the separate full-chat prompt-size failure. Routine supplied-text analysis still uses the smaller worker request to avoid unnecessary history and tool-catalog overhead.

If this specific error returns after an update, inspect the local normalization path and retain the conversation. Do not fabricate call IDs, erase history, recreate chats to hide the failure, or change model authentication. Ordinary task delegation does not require restarting the router or repeating health probes.

### Reuse the verified authorized chat

Use a `threadId` actually returned by Codex. If the user identifies a chat by name, resolve it with `list_threads` and inspect only the relevant chat. Check that the human explicitly authorized messaging this destination for the current scope. Earlier human authorization can remain valid; do not ask again when it covers the task. An agent's request to report back does not authorize a message.

If a reusable destination is recorded in `delegation-target.json`, treat it as a locator, not a permission grant. Use its authorization evidence to recover the human instruction with `read_thread` if that instruction is not in context; check for revocation and scope. A model-generated file or an unrelated chat's title alone is not authorization. If the evidence cannot be recovered and no trusted authorization is in context, ask the human before sending.

Send with `send_message_to_thread`:

```json
{
  "threadId": "<actual-authorized-thread-id>",
  "hostId": "<actual-host-id>",
  "model": "gemini-4-argon",
  "thinking": "high",
  "prompt": "Objective: analyze the supplied corpus. Context: ... Scope: supplied material only. Permission: read-only; no file edits or commands. Expected output: evidence-backed findings and uncertainties. Constraints: no subdelegation or messages to other chats; return the result here for the manager to retrieve. Stop condition: deliver the analysis. Material: ..."
}
```

Use a dedicated idle Codex destination on a host that supports the requested model. Resolve both `threadId` and `hostId` from returned metadata and retain the host on subsequent calls. Model overrides do not apply to ChatGPT-backed conversations. Setting model/thinking can change the destination's settings; use the dedicated Gemini chat rather than changing an unrelated user chat. Supply only context authorized for the task. A read-only prompt is a scope constraint, not a technical filesystem sandbox; for stricter isolation, use supplied text only and an appropriately restricted environment.

### Create a chat only with an explicit human request

The bounded worker does not require a chat. For the separate chat workflow, `create_thread` requires the human to explicitly ask for a new chat. A general coding task, loading this skill, or recommending Gemini does not satisfy this requirement. If no authorized Gemini destination exists, finish independent work and explain the concrete chat-creation requirement. Do not use `fork_thread` or a CLI to evade it.

After an explicit request to create a dedicated analysis chat, a projectless destination avoids accidental access to an existing project:

```json
{
  "model": "gemini-4-argon",
  "thinking": "high",
  "title": "Gemini — analyses model-routing",
  "target": {"type": "projectless"},
  "prompt": "You are the Gemini specialist for a user-authorized model-routing task. Analyze only the supplied material. Do not edit files, execute commands, delegate, or message other chats. Return your result in this chat for the manager to retrieve. Objective: ... Material: ..."
}
```

Use `target.type: "project"` only for a user-requested project chat, after `list_projects` returns its `projectId`. Do not guess the ID or use a projectless chat as authorization to edit another project. A `clientThreadId` from pending setup is not a `threadId`.

When a chat is created, the Codex app instructions require `::created-thread{threadId="..."}` on its own line in the manager's final response; for pending setup use the distinct form `::created-thread{clientThreadId="..."}`. Store a reusable destination only if the human has authorized that use, along with the actual ID, host, scope, and a locator for the human authorization evidence. Never record an invented successful probe.

### Wait, read, verify

After creation or sending, call `wait_threads` with the actual `threadId`, returned `hostId` where available, and `timeoutMs` up to 60000. Carry the returned cursor as `afterCursor` in later waits. A timeout is a progress snapshot, not failure. Use `read_thread` only when more detail is needed. Retrieve the result from the manager; do not ask Gemini to send an unauthorized message back.

Check errors, approval requests, and the actual returned result. Surface any required approval to the human; sending an "approved" message from the manager is not human approval. Do not claim a model switch based only on a recommended model or a queued message. If authentication, capacity, or model availability fails, report the limitation and use another route only if it fits the user's instructions. Do not silently substitute a model explicitly required by the user.

## Other CLI routes are not the Antigravity integration

`gemini --help` succeeds and OAuth personal auth is configured. However, a live noninteractive probe on 2026-10-17 failed with `IneligibleTierError` / `UNSUPPORTED_CLIENT`: Google reported this Gemini Code Assist client is no longer supported and directed migration to Antigravity. The untrusted scratch directory also caused the CLI to override requested plan mode to default.

Do not advertise this CLI as a working fallback, retry it for every task, add `--yolo`/`--skip-trust`, or modify authentication/configuration to get around these failures. Reevaluate only after the installation or account support changes, using a bounded probe in an authorized environment. A successful `--help` or a configured login alone does not prove inference works.

A separate ephemeral, read-only probe with the bundled Codex CLI and `--model gemini-4-argon` also failed: the ChatGPT account endpoint reported that this model is not supported. The chat-tool alias is not a portable model ID for the ordinary Codex/OpenAI backend. Do not repeat this as a fallback.

## Local verification record

- Claude coding: live `opus` task created a fixture in an isolated workspace; returned `models_used: ["claude-opus-5-5"]`. The manager inspected the file and passed all five behavior checks. Claude correctly reported that it had not run tests.
- Claude conversation: `ask_claude` returned an independent review of these instructions and reported `models_used: ["claude-opus-5-5"]`. The manager checked the findings against the full tool inventory and corrected relevant issues.
- Gemini CLI: live attempt failed as described above; no Gemini inference succeeded through that route.
- The original Gemini chat failures were reproduced offline and over the local HTTP route. The regression tests first failed with the exact HTTP 400 `call_id` error, then passed after the fix. All 155 gateway tests passed, including valid paired tool calls and both Claude/Gemini normalization paths.
- Antigravity input integrity: a realistic JSON context of 158,061 UTF-8 bytes retained its final marker and passed the fresh receipt check. A 400k-character native instruction profile also retained its final marker. The original uncompressed full catalog exceeded the native budget; the later catalog compaction repaired that failure. The small worker remains primary for supplied-text analysis because it avoids unnecessary history and catalog overhead.
- Antigravity live transport: an authenticated request containing a standalone delegation output completed with `GEMINI_CALL_ID_REPAIRED` and `metadata.antigravity_model: gemini-4-argon-low`. The adapter checks the actual CLI initialization event before accepting a result.
- Gemini bounded worker: two live calls through the installed script succeeded with router-validated `gemini-4-argon-high`. The manager verified 18%, 9%, 15%, the distinction from WER, and a follow-up on selection bias. All eight worker tests passed, including credential destination, redirect refusal, model mismatch and empty replies. See `delegation-target.json`.
- Gemini full chat: call delivery no longer crashes with `call_id`, and the repaired catalog permits a complete chat turn. The manager independently retested `gemini-4-argon` with `thinking: high`: a real `functions.exec` call retrieved the complete `mcp__codex_apps__sites_get_site` declaration from `ALL_TOOLS`; Gemini then correctly distinguished its required `project_id` input from returned access modes, calculated 15%, 5% and 11%, and returned `GEMINI_CATALOG_RETEST_OK` in 61.021 seconds. The local rollout confirmed the matching tool call/output pair as well as the unpaired delegation input. No Sites operation was executed.
- Bundled Codex CLI using the ordinary ChatGPT endpoint rejected `gemini-4-argon` as unsupported. This does not diagnose the separate local Antigravity route; use the configured Codex chat tools.

- Gemini catalog compaction (2026-10-17): a reconstructed 600-tool exec catalog (867,135 characters) reproduced HTTP 502 `maxMessageTokens must be positive, got 0`. Live probes passed at 100k, 200k and 400k characters. The relay now abbreviates the largest MCP sections to about 250k characters, keeps core tools verbatim and refuses before inference above 380k. The full catalog then returned HTTP 200 (~72.6k input tokens), Gemini requested `ALL_TOOLS.find(...).description` for an abbreviated tool, and the archived Gemini chat answered GEMINI_CHAT_OK in 17 s. It was archived again afterwards; unarchive it before reuse.
- Independent retest after the user's repair (2026-10-17): all 161 gateway tests and eight worker tests passed; the skill validator passed. A fresh bounded worker call returned `observed_model: gemini-4-argon-high`, `tool_calls: 0`, and `GEMINI_WORKER_RECHECK_OK`; the manager verified 15%, 5%, 11%, rejection of subtracting record-error rates from WER, and rejection of proven undertraining. The full-chat retest above also passed. The dedicated chat was returned to its prior archived state after verification.
