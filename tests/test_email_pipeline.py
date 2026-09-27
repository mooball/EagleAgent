"""Tests for includes/email_pipeline.py — shared email pipeline infrastructure."""

import io
import logging
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# get_pipeline_model — env var resolution order
# ---------------------------------------------------------------------------

class TestGetPipelineModel:
    def test_step_specific_env_var(self, monkeypatch):
        monkeypatch.setenv("QUOTE_CLASSIFY_MODEL", "gemini-custom")
        from includes.email_pipeline import get_pipeline_model
        assert get_pipeline_model("QUOTE", "classify") == "gemini-custom"

    def test_pipeline_level_env_var(self, monkeypatch):
        monkeypatch.delenv("QUOTE_CLASSIFY_MODEL", raising=False)
        monkeypatch.setenv("QUOTE_PIPELINE_MODEL", "gemini-pipeline")
        from includes.email_pipeline import get_pipeline_model
        assert get_pipeline_model("QUOTE", "classify") == "gemini-pipeline"

    def test_falls_back_to_default_model(self, monkeypatch):
        monkeypatch.delenv("QUOTE_CLASSIFY_MODEL", raising=False)
        monkeypatch.delenv("QUOTE_PIPELINE_MODEL", raising=False)
        from includes.email_pipeline import get_pipeline_model
        from config.settings import Config
        assert get_pipeline_model("QUOTE", "classify") == Config.DEFAULT_MODEL

    def test_step_overrides_pipeline(self, monkeypatch):
        monkeypatch.setenv("QUOTE_CLASSIFY_MODEL", "step-model")
        monkeypatch.setenv("QUOTE_PIPELINE_MODEL", "pipeline-model")
        from includes.email_pipeline import get_pipeline_model
        assert get_pipeline_model("QUOTE", "classify") == "step-model"

    def test_different_pipelines_independent(self, monkeypatch):
        monkeypatch.setenv("QUOTE_PIPELINE_MODEL", "quote-model")
        monkeypatch.delenv("CUSTOMER_REQUEST_PIPELINE_MODEL", raising=False)
        monkeypatch.delenv("CUSTOMER_REQUEST_EXTRACT_MODEL", raising=False)
        monkeypatch.delenv("QUOTE_EXTRACT_MODEL", raising=False)
        from includes.email_pipeline import get_pipeline_model
        from config.settings import Config
        assert get_pipeline_model("QUOTE", "extract") == "quote-model"
        assert get_pipeline_model("CUSTOMER_REQUEST", "extract") == Config.DEFAULT_MODEL


# ---------------------------------------------------------------------------
# extract_spreadsheet_content — CSV and Excel parsing
# ---------------------------------------------------------------------------

class TestExtractSpreadsheetContent:
    def test_csv_basic(self):
        from includes.email_pipeline import extract_spreadsheet_content
        csv_data = b"Name,Price,Qty\nWidget,10.50,100\nGadget,25.00,50"
        result = extract_spreadsheet_content(csv_data, "prices.csv", "text/csv")
        assert result.ok
        assert "```csv" in result.text
        assert "Widget" in result.text
        assert "10.50" in result.text

    def test_csv_truncation(self):
        from includes.email_pipeline import extract_spreadsheet_content
        long_csv = b"x" * 6000
        result = extract_spreadsheet_content(long_csv, "big.csv", "text/csv")
        assert len(result.text) <= 5100  # 5000 + markdown fencing

    def test_xlsx_basic(self):
        from includes.email_pipeline import extract_spreadsheet_content
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Pricing"
        ws.append(["Part", "Price", "Lead Time"])
        ws.append(["ABC-123", 42.50, "4 weeks"])
        ws.append(["DEF-456", 18.00, "2 weeks"])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()

        result = extract_spreadsheet_content(xlsx_bytes, "quote.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert result.ok
        assert "## Sheet: Pricing" in result.text
        assert "ABC-123" in result.text
        assert "42.5" in result.text
        assert "4 weeks" in result.text

    def test_xlsx_empty_sheet(self):
        from includes.email_pipeline import AttachmentFailure, extract_spreadsheet_content
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Empty"
        buf = io.BytesIO()
        wb.save(buf)
        result = extract_spreadsheet_content(buf.getvalue(), "empty.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert result.failure == AttachmentFailure.EMPTY
        assert result.text == "*[No content extracted]*"

    def test_xlsx_none_cells(self):
        from includes.email_pipeline import extract_spreadsheet_content
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["A", None, "C"])
        ws.append([None, "B2", None])
        buf = io.BytesIO()
        wb.save(buf)
        result = extract_spreadsheet_content(buf.getvalue(), "sparse.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert "B2" in result.text

    def test_xlsx_multi_sheet(self):
        from includes.email_pipeline import extract_spreadsheet_content
        from openpyxl import Workbook
        wb = Workbook()
        ws1 = wb.active
        ws1.title = "Sheet1"
        ws1.append(["Col1"])
        ws1.append(["Val1"])
        ws2 = wb.create_sheet("Sheet2")
        ws2.append(["Col2"])
        ws2.append(["Val2"])
        buf = io.BytesIO()
        wb.save(buf)
        result = extract_spreadsheet_content(buf.getvalue(), "multi.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert "Sheet1" in result.text
        assert "Sheet2" in result.text
        assert "Val1" in result.text
        assert "Val2" in result.text

    def test_xlsx_truncation(self):
        from includes.email_pipeline import extract_spreadsheet_content
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Data"])
        for i in range(300):
            ws.append([f"Row {i} with lots of text to make it larger" * 5])
        buf = io.BytesIO()
        wb.save(buf)
        result = extract_spreadsheet_content(buf.getvalue(), "big.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert len(result.text) <= 8000

    def test_corrupt_file(self):
        from includes.email_pipeline import AttachmentFailure, extract_spreadsheet_content
        result = extract_spreadsheet_content(b"not a real xlsx", "bad.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert result.failure == AttachmentFailure.PARSE_ERROR
        assert result.text == "*[Spreadsheet could not be parsed]*"
        assert result.detail  # upstream message kept for humans/logs


# ---------------------------------------------------------------------------
# llm_call_with_retry — retry and fallback logic
# ---------------------------------------------------------------------------

class TestLlmCallWithRetry:
    def test_success_first_attempt(self):
        from includes.email_pipeline import llm_call_with_retry

        mock_response = MagicMock()
        mock_response.text = "result"
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("includes.email_pipeline.get_pipeline_model", return_value="test-model"), \
             patch("google.genai.Client", return_value=mock_client):
            result = llm_call_with_retry("QUOTE", "classify", ["test"])
            assert result.text == "result"
            assert mock_client.models.generate_content.call_count == 1

    def test_retries_on_transient_error(self):
        from includes.email_pipeline import llm_call_with_retry

        mock_response = MagicMock()
        mock_response.text = "ok"
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            Exception("504 Deadline Exceeded"),
            mock_response,
        ]

        with patch("includes.email_pipeline.get_pipeline_model", return_value="test-model"), \
             patch("google.genai.Client", return_value=mock_client), \
             patch("time.sleep"):
            result = llm_call_with_retry("QUOTE", "classify", ["test"])
            assert result.text == "ok"
            assert mock_client.models.generate_content.call_count == 2

    def test_falls_back_through_distinct_models(self):
        """Every attempt must use a different model than the one that failed.

        The old implementation was ``[primary, primary, FALLBACK_MODEL]``, which
        spent both retries on the model that was already overloaded.
        """
        from includes.email_pipeline import llm_call_with_retry

        mock_response = MagicMock()
        mock_response.text = "fallback ok"
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            Exception("503 Service Unavailable"),
            Exception("504 Gateway Timeout"),
            mock_response,
        ]

        with patch(
            "includes.email_pipeline.get_pipeline_model",
            return_value="gemini-3.8-flash",
        ), patch("google.genai.Client", return_value=mock_client), patch("time.sleep"):
            result = llm_call_with_retry("QUOTE", "classify", ["test"])
            assert result.text == "fallback ok"
            models = [
                call[1]["model"]
                for call in mock_client.models.generate_content.call_args_list
            ]
            assert models[0] == "gemini-3.8-flash"
            assert len(models) == len(set(models)), f"a model was retried: {models}"

    def test_candidates_are_the_ladder_below_the_primary(self):
        """Failover steps down the ladder from wherever the primary sits.

        The old design appended a *flat* FALLBACK_CHAIN, so a Pro model fell
        straight to flash-lite and a flash-lite primary fell *up* to a slower,
        more expensive model.
        """
        from config.settings import Config
        from includes.email_pipeline import get_pipeline_candidates

        ladder = [
            "gemini-3.1-pro-preview",
            "gemini-3.8-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
        ]
        with patch.object(Config, "MODEL_LADDER", ladder):
            for primary, expected in {
                "gemini-3.1-pro-preview": ladder,
                "gemini-3.8-flash": ladder[1:],
                "gemini-3.6-flash": ladder[2:],
                "gemini-3.5-flash-lite": ladder[3:],
            }.items():
                with patch(
                    "includes.email_pipeline.get_pipeline_model", return_value=primary
                ):
                    candidates = get_pipeline_candidates("QUOTE", "classify")
                assert candidates == expected, primary
                assert candidates[0] == primary
                assert len(candidates) == len(set(candidates))

    def test_bottom_of_ladder_has_no_fallback(self):
        """flash-lite is the floor, so a 429 there is terminal by design.

        There is nothing cheaper to step down to, and retrying the same
        overloaded model is the anti-pattern this whole function exists to
        avoid. Accepted for now; service-tier Priority is the mitigation.
        """
        from config.settings import Config
        from includes.email_pipeline import get_pipeline_candidates

        ladder = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
        with patch.object(Config, "MODEL_LADDER", ladder), patch(
            "includes.email_pipeline.get_pipeline_model",
            return_value="gemini-3.5-flash-lite",
        ):
            assert get_pipeline_candidates("QUOTE", "classify") == [
                "gemini-3.5-flash-lite"
            ]

    def test_unranked_primary_falls_back_only_to_the_cheapest_rung(self, caplog):
        """An unranked model must never fail over to something costlier.

        We cannot know where an unranked model belongs on the ladder, so the
        only safe direction is down.
        """
        from config.settings import Config
        from includes.email_pipeline import get_pipeline_candidates

        ladder = [
            "gemini-3.1-pro-preview",
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
        ]
        with patch.object(Config, "MODEL_LADDER", ladder), patch(
            "includes.email_pipeline.get_pipeline_model",
            return_value="gemini-3-flash-preview",
        ), caplog.at_level(logging.WARNING, logger="includes.email_pipeline"):
            candidates = get_pipeline_candidates("QUOTE", "classify")

        assert candidates == ["gemini-3-flash-preview", "gemini-3.5-flash-lite"]
        assert "not on MODEL_LADDER" in caplog.text

    def test_not_found_is_not_retried(self):
        """A 404 must fail immediately.

        This is the dead-fallback bug: FALLBACK_MODEL pointed at a model that
        returns 404, so the "safety net" was itself the failure.
        """
        from includes.email_pipeline import llm_call_with_retry

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception(
            "404 NOT_FOUND. Publisher model gemini-2.0-flash was not found"
        )

        with patch("includes.email_pipeline.get_pipeline_model", return_value="test-model"), \
             patch("google.genai.Client", return_value=mock_client), \
             patch("time.sleep"):
            with pytest.raises(Exception, match="404"):
                llm_call_with_retry("QUOTE", "classify", ["test"])
            assert mock_client.models.generate_content.call_count == 1

    def test_permanent_error_not_retried(self):
        from includes.email_pipeline import llm_call_with_retry

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = ValueError("Invalid input")

        with patch("includes.email_pipeline.get_pipeline_model", return_value="test-model"), \
             patch("google.genai.Client", return_value=mock_client):
            with pytest.raises(ValueError, match="Invalid input"):
                llm_call_with_retry("QUOTE", "classify", ["test"])
            assert mock_client.models.generate_content.call_count == 1

    def test_all_retries_exhausted(self):
        """All three rungs below the primary are tried, then it raises."""
        from includes.email_pipeline import llm_call_with_retry

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("503 UNAVAILABLE")

        with patch(
            "includes.email_pipeline.get_pipeline_model",
            return_value="gemini-3.8-flash",
        ), patch("google.genai.Client", return_value=mock_client), patch("time.sleep"):
            with pytest.raises(Exception, match="503"):
                llm_call_with_retry("QUOTE", "classify", ["test"])
            # 3.8-flash -> 3.6-flash -> 3.5-flash-lite
            assert mock_client.models.generate_content.call_count == 3

    def test_attempt_timeout_is_bounded_by_remaining_budget(self):
        """A single attempt must not outlive the whole call budget.

        The budget used to be checked only *between* attempts, so one call could
        run to the full per-request timeout — prod logged a 91.4s attempt
        against a nominal 45s budget.
        """
        from includes.email_pipeline import llm_call_with_retry

        mock_response = MagicMock()
        mock_response.text = "ok"
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch(
            "includes.email_pipeline.get_pipeline_model",
            return_value="gemini-3.8-flash",
        ), patch("includes.email_pipeline.Config.LLM_MAX_ATTEMPT_SECONDS", 5.0), patch(
            "includes.email_pipeline._http_options"
        ) as mock_http_options, patch(
            "google.genai.Client", return_value=mock_client
        ):
            llm_call_with_retry("QUOTE", "classify", ["test"])

        timeout_ms = mock_http_options.call_args[0][0]
        assert timeout_ms <= 5000, f"attempt timeout {timeout_ms}ms exceeds the 5s budget"

    def test_gives_up_when_budget_is_exhausted(self):
        """Once the budget is gone, no further attempt is started."""
        from includes.email_pipeline import llm_call_with_retry

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("503 UNAVAILABLE")

        # deadline calc -> 0.0, first attempt -> 0.0, then the clock jumps past
        # the 5s budget so no second attempt may start.
        clock = iter([0.0, 0.0, 10.0, 10.0, 10.0])
        with patch(
            "includes.email_pipeline.get_pipeline_model",
            return_value="gemini-3.8-flash",
        ), patch("includes.email_pipeline.Config.LLM_MAX_ATTEMPT_SECONDS", 5.0), patch(
            "includes.email_pipeline.time.monotonic",
            side_effect=lambda: next(clock),
        ), patch("google.genai.Client", return_value=mock_client), patch("time.sleep"):
            with pytest.raises(Exception, match="503"):
                llm_call_with_retry("QUOTE", "classify", ["test"])

        assert mock_client.models.generate_content.call_count == 1


# ---------------------------------------------------------------------------
# Image signature caching
# ---------------------------------------------------------------------------

class TestImageSignature:
    def test_check_known_signature(self):
        from includes.email_pipeline import check_image_signature

        mock_record = MagicMock()
        mock_record.classification = "signature"
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_record

        with patch("includes.email_pipeline._get_session", return_value=mock_session):
            result = check_image_signature(b"test image bytes")
            assert result == "signature"

    def test_check_unknown_signature(self):
        from includes.email_pipeline import check_image_signature

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None

        with patch("includes.email_pipeline._get_session", return_value=mock_session):
            result = check_image_signature(b"new image bytes")
            assert result is None

    def test_store_signature(self):
        from includes.email_pipeline import store_image_signature
        import hashlib

        mock_session = MagicMock()
        # Return None for the "existing" query so the function proceeds to add
        mock_session.query.return_value.filter.return_value.first.return_value = None
        image_bytes = b"test image"
        expected_sha = hashlib.sha256(image_bytes).hexdigest()

        with patch("includes.email_pipeline._get_session", return_value=mock_session):
            store_image_signature(image_bytes, "signature", "logo.png", 42)
            mock_session.add.assert_called_once()
            added = mock_session.add.call_args[0][0]
            assert added.sha256 == expected_sha
            assert added.classification == "signature"
            assert added.sample_filename == "logo.png"
            mock_session.commit.assert_called_once()

    def test_store_signature_skips_duplicate(self):
        from includes.email_pipeline import store_image_signature

        mock_existing = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_existing

        with patch("includes.email_pipeline._get_session", return_value=mock_session):
            store_image_signature(b"test image", "signature")
            mock_session.add.assert_not_called()
