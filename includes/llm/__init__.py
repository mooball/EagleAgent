"""LLM provider plumbing: telemetry, model/tier selection, failover.

Currently:

- ``telemetry`` — best-effort per-call recording (model, tier, latency, tokens,
  error class) into ``llm_call_log``.

Planned (see .github/prompts/plan-llmObservabilityAndFailover.prompt.md):
a ``llm_targets`` registry so a single resolver decides model + location + tier
per scope, with capability gating and ordered failover candidates.
"""

from includes.llm.telemetry import (  # noqa: F401
    classify_error,
    flush,
    instrument_call,
    record_llm_call,
    stats,
)
