"""Manual-retry wiring for the supplier quote pipeline.

The retry is a thin delegation: it must force the run past the normal
"already processed" guard and switch on the longer patient budget. If either
flag is dropped, a retry silently behaves like the normal trigger and does
nothing for an email that already has a result — which is the whole point.

The claim is also the concurrency guard: a forced retry may reclaim a terminal
result, but must never start a second worker while one is actively processing.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


def test_retry_forces_a_patient_rerun():
    from includes.tools import supplier_quote_pipeline as sqp

    with patch.object(sqp, "trigger_supplier_quote_pipeline") as trigger:
        sqp.retry_supplier_quote_pipeline(89676, user_id="angie@eagle-example.com")

    trigger.assert_called_once_with(
        89676,
        user_id="angie@eagle-example.com",
        force=True,
        patient_budget=True,
    )


# ---------------------------------------------------------------------------
# _claim_pipeline_run — the atomic claim
# ---------------------------------------------------------------------------

def _session_returning(result):
    """A session whose FOR UPDATE select returns a row with this result."""
    session = MagicMock()
    session.execute.return_value.mappings.return_value.first.return_value = {"r": result}
    return session


def test_forced_claim_refuses_an_active_run():
    """Two Retry clicks must not both start workers for the same email."""
    from includes.tools import supplier_quote_pipeline as sqp

    now = datetime.now(timezone.utc).isoformat()
    session = _session_returning({"status": "processing", "started_at": now})

    claimed, reason = sqp._claim_pipeline_run(session, 123, force=True)

    assert claimed is False
    assert reason == "already_running"
    session.commit.assert_not_called()


def test_forced_claim_reclaims_a_stale_run():
    """A marker left by a crashed server must not block a retry forever."""
    from includes.tools import supplier_quote_pipeline as sqp

    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    session = _session_returning({"status": "processing", "started_at": old})

    claimed, reason = sqp._claim_pipeline_run(session, 123, force=True)

    assert claimed is True
    assert reason == "claimed"
    session.commit.assert_called_once()


def test_forced_claim_overwrites_a_terminal_result():
    from includes.tools import supplier_quote_pipeline as sqp

    session = _session_returning({"classification": "quote_response", "error": "boom"})

    claimed, reason = sqp._claim_pipeline_run(session, 123, force=True)

    assert claimed is True
    assert reason == "claimed"


def test_normal_claim_skips_an_existing_result():
    from includes.tools import supplier_quote_pipeline as sqp

    session = _session_returning({"classification": "quote_response"})

    claimed, reason = sqp._claim_pipeline_run(session, 123, force=False)

    assert claimed is False
    assert reason == "already_processed"
    session.commit.assert_not_called()


def test_normal_claim_takes_an_unprocessed_email():
    from includes.tools import supplier_quote_pipeline as sqp

    session = _session_returning(None)

    claimed, reason = sqp._claim_pipeline_run(session, 123, force=False)

    assert claimed is True
    assert reason == "claimed"


def test_stale_processing_treats_malformed_markers_as_stale():
    from includes.tools import supplier_quote_pipeline as sqp

    assert sqp._is_stale_processing({"status": "processing", "started_at": "nope"}) is True
    assert sqp._is_stale_processing({"status": "processing"}) is True
    # Not a processing marker at all -> not "stale processing".
    assert sqp._is_stale_processing({"classification": "quote_response"}) is False
    assert sqp._is_stale_processing(None) is False
