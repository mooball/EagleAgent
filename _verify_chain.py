"""Standalone verification of the model-ladder change (no database required).

Run: uv run python _verify_chain.py
"""
import os

os.environ["LLM_TELEMETRY_ENABLED"] = "0"  # never write telemetry rows from a check

from unittest.mock import MagicMock, patch  # noqa: E402

from config.settings import Config  # noqa: E402
from includes.email_pipeline import get_pipeline_candidates, llm_call_with_retry  # noqa: E402

print("MODEL_LADDER =", Config.MODEL_LADDER)
print()

print("1) Candidates for the real .env primaries:")
for pipeline, step in [
    ("QUOTE", "extract"),
    ("QUOTE", "classify"),
    ("QUOTE", "interpret"),
    ("RFQ_CREATION", "extract"),
]:
    primary = __import__(
        "includes.email_pipeline", fromlist=["x"]
    ).get_pipeline_model(pipeline, step)
    print(f"   {pipeline}/{step:9s} primary={primary:24s} -> {get_pipeline_candidates(pipeline, step)}")

print()
print("2) Unranked primary (must fall down to the cheapest rung only):")
os.environ["QUOTE_INTERPRET_MODEL"] = "gemini-3-flash-preview"
print("   ->", get_pipeline_candidates("QUOTE", "interpret"))

print()
print("3) Empty ladder (must degrade to the primary alone, not crash):")
with patch.object(Config, "MODEL_LADDER", []):
    print("   ->", get_pipeline_candidates("QUOTE", "extract"))

print()
print("4) Attempt HTTP timeout must be bounded by the remaining budget:")
mock_client = MagicMock()
mock_client.models.generate_content.side_effect = Exception("503 UNAVAILABLE")
with patch.object(Config, "LLM_MAX_ATTEMPT_SECONDS", 5.0), patch(
    "includes.email_pipeline._http_options"
) as mock_options, patch(
    "google.genai.Client", return_value=mock_client
), patch("time.sleep"):
    try:
        llm_call_with_retry("QUOTE", "extract", ["x"])
        print("   ERROR: expected a raise")
    except Exception as exc:  # noqa: BLE001
        print(f"   raised {type(exc).__name__} as expected")
    timeouts = [call[0][0] for call in mock_options.call_args_list]
    print(f"   attempts={mock_client.models.generate_content.call_count} timeouts_ms={timeouts}")
    assert all(t <= 5000 for t in timeouts), "an attempt exceeded the 5s budget"
    print("   all attempts within budget OK")

print()
print("5) Candidate order is tried in sequence (3.8 -> 3.6 -> lite):")
mock_client2 = MagicMock()
mock_client2.models.generate_content.side_effect = [
    Exception("429 rate limited"),
    Exception("429 rate limited"),
    MagicMock(text="ok"),
]
with patch("google.genai.Client", return_value=mock_client2), patch("time.sleep"):
    result = llm_call_with_retry("QUOTE", "extract", ["x"])
    models = [c[1]["model"] for c in mock_client2.models.generate_content.call_args_list]
    print(f"   result={result.text!r} attempted={models}")
    assert models == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"], models

print()
print("ALL CHECKS PASSED")
