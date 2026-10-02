"""Manual-retry wiring for the supplier quote pipeline.

The retry is a thin delegation: it must force the run past the normal
"already processed" guard and switch on the longer patient budget. If either
flag is dropped, a retry silently behaves like the normal trigger and does
nothing for an email that already has a result — which is the whole point.
"""

from unittest.mock import patch


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
