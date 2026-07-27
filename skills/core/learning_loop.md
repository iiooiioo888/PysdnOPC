---
name: learning-loop
description: "Minimal learning loop for high-frequency repeated scenarios (Post-Edit Validation, CI smoke test). Captures execution patterns and reuses them on subsequent triggers."
domain:
  - validation
  - testing
  - learning
trigger: "When post-edit validation or CI smoke test is triggered repeatedly"
always_on: false
---

# Learning Loop Skill

A minimal learning loop that captures execution patterns from high-frequency
repeated scenarios and reuses them to optimize subsequent runs.

## Trigger Conditions

The learning loop activates when ANY of the following occurs:

1. **Post-Edit Validation**: A file under `opc/`, `tests/`, or `scripts/` is
   edited (Write/SearchReplace/Edit tool call completes).
2. **CI Smoke Test**: `python -m pytest tests/ -q --tb=short -x` is invoked
   via pre-commit hook or manual CI run.
3. **Repeated Scenario**: The same file prefix + test target combination has
   been seen before (pattern store hit).

## Execution Steps

1. **Detect** — Identify the trigger scenario (file path → test target mapping).
2. **Lookup** — Query the pattern store (`.opc/memory/learned_patterns.json`)
   for a cached strategy matching the current file prefix.
3. **Execute** — Run the validation using the learned strategy:
   - If pattern exists with confidence ≥ 0.8: use the narrowed test target.
   - Otherwise: fall back to the default full-suite strategy.
4. **Capture** — Record the execution outcome (pass/fail, duration, target)
   into the pattern store and telemetry log.
5. **Adapt** — Update confidence scores based on outcome:
   - Pass with narrowed target → increase confidence (+0.1, cap 1.0).
   - Fail with narrowed target → decrease confidence (-0.3, floor 0.0).
   - Fail triggers automatic fallback to full-suite on next run.

## Validator

After each learning-loop execution, validate:

- [ ] Telemetry event appended to `.opc/logs/post_edit_validation.jsonl`
- [ ] Pattern store updated at `.opc/memory/learned_patterns.json`
- [ ] Skill activation logged with `"skill": "learning-loop"` field
- [ ] Second trigger of same scenario shows `"pattern_reused": true`

## Stop Rules

The learning loop STOPS (falls back to default behavior) when:

1. **Confidence collapse**: A narrowed target fails → immediately revert to
   full-suite for that file prefix (confidence reset to 0.0).
2. **Staleness**: Pattern not exercised for 50 consecutive runs → evict entry.
3. **Timeout**: Validation exceeds 180s → abort and log timeout event.
4. **Consecutive failures**: 3 consecutive failures on any target → escalate
   to full-suite and mark prefix as "unstable" for 10 runs.

## Pattern Store Schema

```json
{
  "version": 1,
  "patterns": {
    "<file_prefix>": {
      "test_target": "tests/test_x.py",
      "confidence": 0.9,
      "runs": 12,
      "passes": 11,
      "failures": 1,
      "last_run": "2025-01-01T00:00:00Z",
      "last_outcome": "pass",
      "avg_duration_ms": 3200,
      "consecutive_failures": 0,
      "unstable_until": 0
    }
  }
}
```

## Integration Points

| Component | Role |
|---|---|
| `scripts/hooks/post_edit_validate.py` | Trigger + executor + pattern capture |
| `.opc/logs/post_edit_validation.jsonl` | Telemetry / audit trail |
| `.opc/memory/learned_patterns.json` | Pattern persistence |
| `skills/core/learning_loop.md` | This skill definition |
| `.pre-commit-config.yaml` (pytest-smoke) | CI smoke test trigger |
