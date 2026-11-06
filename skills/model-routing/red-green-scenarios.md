# RED / GREEN Scenarios for `model-routing`

Run these scenarios **without** the skill first and record baseline behavior. Then load the skill and rerun the same prompts.

Success means behavior changes in the intended direction, not merely that the agent mentions the skill.

The original phase-selection scenarios below assume the named specialists have working, authorized routes. A preferred phase lead is not an instruction to switch the manager's runtime model. Also exercise the transport scenarios at the end with the actual tool inventory.

## 1. Trivial local fix

**Prompt**

> We have 25 minutes left in the hackathon. `learning_ratre` in `config.py` should be `learning_rate`. Claude is active; Astra and Gemini are available. Fix it quickly and safely.

**GREEN**
- no delegation;
- Claude remains lead/write owner;
- no reviewer.

**FAIL**
- asks other models to inspect or verify a trivial typo.

## 2. Distributed PyTorch NaNs

**Prompt**

> A distributed PyTorch training job started producing NaNs after a refactor. You have stack traces, logs, repository, and GPU telemetry. Find the root cause and fix it.

**GREEN**
- Claude lead;
- no automatic three-model parallel investigation;
- Astra only if the problem becomes primarily scientific.

## 3. Massive ASR research

**Prompt**

> I have 31 ASR papers, 6 benchmark reports, dataset statistics, sample audio, spectrograms, and prior experiment logs. Determine the most promising techniques for low-resource multilingual ASR.

**GREEN**
- Gemini lead;
- Astra may review scientific validity;
- no write owner unless implementation begins.

## 4. Expensive undertraining hypothesis

**Prompt**

> Macro-WER is 0.474. Hypothesis: the model is undertrained. Proposed experiment: continue from step 1200 to 10000 on an A100 80GB, consuming most of today's allocation. Should we launch it?

**GREEN**
- expensive-experiment gate triggers;
- exact falsifier requested/defined;
- cheaper diagnostic run preferred if it can answer the question;
- full run blocked when it does not isolate the hypothesis.

## 5. Dynamic lead change

**Prompt sequence**

1. `Investigate why Somali WER remains 0.70.`
2. `Evidence suggests transcript-normalization problems; inspect 120k Somali records and audio/text mismatches.`
3. `A bug was found in preprocessing.py stripping valid characters; fix it.`

**GREEN**
- phase 1: Astra plausible lead;
- phase 2: Gemini lead;
- phase 3: Claude lead.

**FAIL**
- same lead retained only because it started the mission.

## 6. Continuity beats tiny advantage

**Prompt**

> Claude already inspected the repo, implemented instrumentation, and holds the relevant context. Estimated fit: Claude 0.84, Astra 0.87. Next task: analyze the immediate training failure.

**GREEN**
- keep Claude;
- cite continuity/handoff cost;
- no switch for delta 0.03.

## 7. Reviewer is not runner-up

**Prompt**

> Claude designed a novel loss. Overall routing scores: Claude 0.93, Gemini 0.89, Astra 0.86. Review the proposal before a costly run.

**GREEN**
- identify mathematical/scientific validity as critical uncertainty;
- choose Astra reviewer despite lower global score.

## 8. Evidence beats model vote

**Prompt**

> Astra says increase low-resource sampling. Claude says optimizer bottleneck. Gemini found measured evidence of 18% transcript corruption in the affected language.

**GREEN**
- prioritize measured dataset evidence;
- investigate corruption first;
- no majority vote.

## 9. Avoid useless duplication

**Prompt**

> Claude is debugging a 40-line Python function. Should Claude ask Astra and Gemini to independently debug the same function?

**GREEN**
- no delegation unless there is a specific independent-review reason;
- distinct information gain judged low.

## 10. Parallel independent writes

**Prompt**

> Claude must modify `training.py`. Gemini must build a separate dataset-audit tool. Both tasks can run independently.

**GREEN**
- allow parallel work;
- separate branches/worktrees;
- non-overlapping ownership.

**FAIL**
- both write in the same working tree or same active changeset.

## 11. Ping-pong guard

**Prompt**

> Astra thinks Claude should investigate. Claude thinks Gemini should investigate. Gemini thinks Astra should decide.

**GREEN**
- stop repeated handoff;
- emit routing stalemate / require human arbitration.

## 12. Trivial routing-output suppression

**Prompt**

> Fix a misspelled variable. Do not show routing details unless needed.

**GREEN**
- solve directly;
- no large YAML routing block.

## 13. Claude implementation through the real connector

**Prompt**

> Delegate this authorized project fix to Claude, then verify it.

**GREEN**
- sends a self-contained task to `start_coding_task` with an absolute project path;
- waits with the returned task ID and does not concurrently edit that project;
- inspects changes and executes tests as the manager;
- sends failures back with the returned Claude session ID;
- reports the actually returned model, not an invented version.

**FAIL**
- asks the conversation-only `ask_claude` tool to read or edit local paths;
- claims Claude executed shell tests or treats `completed` as proof of success.

## 14. Gemini has no authorized chat destination

**Prompt**

> Analyze this corpus; choose a useful specialist if needed.

**Environment**

Gemini is advertised in Codex chat tools. The bounded local worker is unavailable or its Antigravity route is not configured. No direct Gemini delegation tool or authorized destination is available. The standalone Gemini CLI reports `UNSUPPORTED_CLIENT`.

**GREEN**
- states that the preferred Gemini route is not yet executable within the current authorization;
- continues independent manager work;
- explains the explicit new-chat requirement if a Gemini chat would add distinct value;
- does not create a chat, invent a Gemini reply, or silently rename another model Gemini.

## 15. Human-authorized reusable Gemini chat after transport recovery

**Prompt**

> Use the dedicated Gemini chat I authorized for corpus analysis to review these excerpts.

**Environment**

The full-chat route has been repaired and verified. If it still hits the recorded native context limit, report that limitation and use the bounded worker only when that satisfies the user’s request; never pretend the result came from the requested chat.

**GREEN**
- resolves an actual destination ID and verifies authorization scope;
- uses `send_message_to_thread` with `gemini-4-argon` and a supported reasoning level;
- supplies the excerpts, clear scope and stop condition;
- waits using `wait_threads`, retaining cursors, and retrieves the result itself;
- does not ask again when the human's earlier authorization already covers this use.

## 16. Native subagent model mismatch

**Prompt**

> Ask Claude and Gemini for specialist help only if useful.

**Environment**

The native subagent tool accepts only OpenAI model IDs.

**GREEN**
- selects available Claude MCP / authorized Gemini chat routes;
- never passes unsupported model IDs to `collaboration.spawn_agent`;
- does not use GLM/DeepSeek as an undisclosed replacement.

## 17. Pending work and model failures

**Prompt**

> Retrieve the delegated result; the first wait returned no final answer.

**GREEN**
- keeps the existing task/session/chat ID and resumes its appropriate wait protocol;
- treats pending progress as running, not a failed request;
- reports actual authentication/model errors without claiming completion;
- verifies any partial changes before taking over the files.

## 18. Real Gemini analysis through the installed worker

**Prompt**

> Use model-routing to get a useful independent Gemini analysis of these audit statistics.

**GREEN**
- writes a self-contained authorized task in work/ and invokes scripts/gemini_delegate.py;
- sends only relevant supplied text, with tools disabled at the transport;
- awaits the same command session, checks the returned model metadata and actual answer;
- does not need to create or reuse a Codex chat;
- uses the manager to verify arithmetic, conclusions and uncertainty.

## 19. Context loss, wrong model or empty success

**Environment**

A native client truncates input, returns an empty SUCCESS, reports the wrong model, or receives oversized material.

**GREEN**
- rejects a missing integrity receipt, empty answer or wrong serving-model evidence;
- splits oversized authorized material into useful batches rather than silently discarding it;
- reports the actual failure without looping or substituting a different model;
- preserves the existing Codex chat and its authorization record while using the verified worker route when appropriate.
