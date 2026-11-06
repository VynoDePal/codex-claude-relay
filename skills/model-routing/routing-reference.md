# Routing Reference

## Capability Priors

These are routing priors, not absolute model rankings or measured benchmarks. Calibrate them over time from observed performance. Filter candidates by a working, authorized transport first; names in this table are not API identifiers. See [codex-delegation.md](codex-delegation.md) for tool-specific identifiers.

| Capability | GPT-6 Astra | Claude Opus 5.5 | Gemini 4 Argon |
|---|---:|---:|---:|
| scientific_reasoning | 5.0 | 4.7 | 4.8 |
| experiment_design | 5.0 | 4.8 | 4.6 |
| model_architecture | 4.7 | 5.0 | 4.6 |
| ml_engineering | 4.5 | 5.0 | 4.7 |
| debugging | 4.6 | 5.0 | 4.4 |
| repo_engineering | 4.7 | 5.0 | 4.7 |
| deep_research | 4.7 | 4.5 | 5.0 |
| dataset_forensics | 4.5 | 4.7 | 5.0 |
| multimodal_analysis | 4.7 | 4.4 | 5.0 |
| long_context_synthesis | 4.7 | 4.4 | 5.0 |
| result_interpretation | 5.0 | 4.8 | 4.8 |
| computer_tool_use | 5.0 | 4.9 | 4.8 |

## Scoring

For model `m`:

`raw_score(m) = Σ(task_weight_i × capability_m_i) / Σ(task_weight_i)`

Then reason qualitatively about:
- specialization bonus;
- current-context bonus;
- continuity bonus;
- handoff penalty.

Do not fake precision. Round decisions to `low`, `medium`, or `high` confidence.

### Default switch thresholds

- `< 0.08` advantage: keep current lead.
- `0.08–0.15`: ambiguous; usually keep current lead.
- `≥ 0.15`: recommend switching if the phase truly changed.

## Preferred Leads by Dominant Phase

| Dominant phase | Preferred prior |
|---|---|
| scientific reasoning / experiment design | Astra |
| ML architecture / implementation / debugging | Claude |
| deep research / dataset forensics / multimodal / very long context | Gemini |

These are defaults only. Mixed tasks must be scored.

## Critical Uncertainty → Reviewer

| Critical uncertainty | Preferred reviewer |
|---|---|
| scientific validity, causal reasoning, maths | Astra |
| implementation feasibility, code correctness, debugging | Claude |
| dataset evidence, corpus analysis, multimodal or long-context evidence | Gemini |

## Full Routing Contract

```yaml
MODEL_ROUTING:

  phase: <current phase>

  task_profile:
    <dimension>: <0-5>

  lead:
    model: <current-model-or-verified-specialist-id>
    confidence: <low|medium|high>

  execution:
    manager: <actual-current-Codex-model>
    transport: <local|claude-conversation|claude-coding|authorized-codex-chat|unavailable>
    status: <recommended|started|running|completed|verified|blocked>
    task_id: <returned-id|none>
    session_id: <returned-id|none>
    thread_id: <returned-id|none>
    requested_model: <tool-specific-id|none>
    reported_models: []

  critical_uncertainty:
    type: <type|none>

  reviewer:
    model: <model|none>
    required: <true|false>
    focus: <focus|none>
    permission: <read-only|patch-proposal>

  specialists:
    - model: <model>
      task: <bounded task>
      permission: <read-only|patch-proposal|write>

  write_owner:
    model: <model|none>

  switch:
    recommended: <true|false>
    reason: <short reason>

  reassess_when:
    - <event>

  human_override: true
```

## Delegation Contract

```yaml
delegation:
  to: <model>
  transport: <verified-callable-route>
  objective: <single bounded objective>
  context: <only context needed>
  scope:
    - <allowed work>
  expected_output:
    - <deliverable>
  constraints:
    - <explicit limits>
  permission: <read-only|patch-proposal|write>
  authorized_project: <absolute-path|none>
  verification: <manager checks and relevant test commands>
  stop_condition: <when to return>
```

## Review Gate for Expensive Experiments

A reviewer must answer:

```yaml
experiment_gate:
  hypothesis: <exact hypothesis>
  falsifier: <observation that would disprove it>
  cheaper_probe_available: <true|false>
  cheaper_probe: <probe|none>
  full_run: <allow|block>
  reason: <short reason>
```
