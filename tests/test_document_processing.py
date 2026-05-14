"""Tests for includes/chat/document_processing.py — file parsing and multimodal content."""

import io
import json

import pytest

from includes.chat.document_processing import (
    process_image,
    extract_pdf_text,
    extract_text_from_file,
    extract_spreadsheet_text,
    process_audio,
    process_file,
    create_multimodal_content,
    SPREADSHEET_MIME_TYPES,
    SPREADSHEET_EXTENSIONS,
)


# ── Image processing ─────────────────────────────────────────────


class TestProcessImage:
    def _make_png_bytes(self, width=10, height=10):
        from PIL import Image
        img = Image.new("RGB", (width, height), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _make_rgba_png_bytes(self):
        from PIL import Image
        img = Image.new("RGBA", (8, 8), color=(0, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_basic_png(self):
        data = self._make_png_bytes(20, 30)
        result = process_image(data, "image/png")
        assert result["type"] == "image"
        assert result["size"] == (20, 30)
        assert result["format"] == "PNG"
        assert len(result["base64"]) > 0

    def test_rgba_converts(self):
        """RGBA images should be converted to RGB without error."""
        data = self._make_rgba_png_bytes()
        result = process_image(data, "image/png")
        assert result["type"] == "image"

    def test_invalid_bytes_raises(self):
        with pytest.raises(Exception):
            process_image(b"not an image", "image/png")


# ── PDF extraction ───────────────────────────────────────────────


class TestExtractPdfText:
    def _make_pdf_bytes(self, text="Hello World"):
        """Create a minimal PDF with reportlab if available, else fpdf2."""
        try:
            from fpdf import FPDF
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Helvetica", size=12)
            pdf.cell(text=text)
            return pdf.output()
        except ImportError:
            pytest.skip("fpdf2 not installed")

    def test_extract_text(self):
        pdf_bytes = self._make_pdf_bytes("Test PDF Content")
        result = extract_pdf_text(pdf_bytes)
        assert "Test PDF Content" in result

    def test_invalid_pdf_returns_error_string(self):
        result = extract_pdf_text(b"not a pdf")
        assert result.startswith("[Error") or result.startswith("[PDF")


# ── Text extraction ──────────────────────────────────────────────


class TestExtractTextFromFile:
    def test_plain_text_utf8(self):
        content = "Hello, world! Ünïcödé"
        result = extract_text_from_file(content.encode("utf-8"), "text/plain")
        assert "Hello" in result
        assert "Ünïcödé" in result

    def test_plain_text_latin1(self):
        content = "Café résumé"
        result = extract_text_from_file(content.encode("latin-1"), "text/plain")
        assert "Café" in result

    def test_csv_by_extension(self):
        content = "a,b,c\n1,2,3"
        result = extract_text_from_file(content.encode(), "application/octet-stream", "data.csv")
        assert "a,b,c" in result

    def test_unsupported_mime(self):
        result = extract_text_from_file(b"\x00\x01", "application/x-binary")
        assert "not supported" in result


# ── Spreadsheet extraction ───────────────────────────────────────


class TestExtractSpreadsheetText:
    def _make_xlsx_bytes(self):
        import pandas as pd
        df = pd.DataFrame({"Name": ["Alice", "Bob"], "Score": ["90", "85"]})
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl")
        return buf.getvalue()

    def test_extract_xlsx(self):
        data = self._make_xlsx_bytes()
        result = extract_spreadsheet_text(data, "test.xlsx")
        assert "Alice" in result
        assert "Bob" in result
        assert "Sheet" in result  # should have sheet header

    def test_invalid_spreadsheet(self):
        result = extract_spreadsheet_text(b"not excel", "bad.xlsx")
        assert "Error" in result


# ── Audio processing ─────────────────────────────────────────────


class TestProcessAudio:
    def test_returns_placeholder(self):
        result = process_audio(b"\x00" * 100, "audio/mp3")
        assert result["type"] == "audio"
        assert result["size_bytes"] == 100
        assert "not yet implemented" in result["transcription"]


# ── process_file dispatcher ──────────────────────────────────────


class TestProcessFile:
    def test_image_dispatch(self):
        from PIL import Image
        img = Image.new("RGB", (5, 5))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()

        result = process_file(data, "image/jpeg", "photo.jpg")
        assert result["processed_type"] == "image"
        assert result["filename"] == "photo.jpg"

    def test_text_dispatch(self):
        result = process_file(b"Hello!", "text/plain", "note.txt")
        assert result["processed_type"] == "text"
        assert "Hello!" in result["content"]

    def test_audio_dispatch(self):
        result = process_file(b"\x00", "audio/wav", "clip.wav")
        assert result["processed_type"] == "audio"

    def test_unsupported_type(self):
        result = process_file(b"\x00", "application/x-unknown", "mystery.bin")
        assert result["processed_type"] == "unsupported"

    def test_spreadsheet_by_mime(self):
        import pandas as pd
        df = pd.DataFrame({"X": ["1"]})
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl")
        data = buf.getvalue()

        result = process_file(
            data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "report.xlsx",
        )
        assert result["processed_type"] == "spreadsheet"


# ── Multimodal content builder ───────────────────────────────────


class TestCreateMultimodalContent:
    def test_text_only(self):
        parts = create_multimodal_content("Hello", [])
        assert len(parts) == 1
        assert parts[0]["type"] == "text"
        assert parts[0]["text"] == "Hello"

    def test_empty_text_and_files(self):
        parts = create_multimodal_content("", [])
        assert len(parts) == 1
        assert parts[0]["type"] == "text"

    def test_image_adds_image_url(self):
        files = [{
            "processed_type": "image",
            "filename": "pic.jpg",
            "content": {"mime_type": "image/jpeg", "base64": "abc123"},
        }]
        parts = create_multimodal_content("Look at this", files)
        assert any(p["type"] == "image_url" for p in parts)
        image_part = next(p for p in parts if p["type"] == "image_url")
        assert "data:image/jpeg;base64,abc123" in image_part["image_url"]

    def test_pdf_appends_to_text(self):
        files = [{
            "processed_type": "pdf",
            "filename": "doc.pdf",
            "content": "Extracted PDF text here",
        }]
        parts = create_multimodal_content("Please review", files)
        text_part = next(p for p in parts if p["type"] == "text")
        assert "doc.pdf" in text_part["text"]
        assert "Extracted PDF text here" in text_part["text"]

    def test_spreadsheet_appends_to_text(self):
        files = [{
            "processed_type": "spreadsheet",
            "filename": "data.xlsx",
            "content": "A,B\n1,2",
        }]
        parts = create_multimodal_content("", files)
        text_part = next(p for p in parts if p["type"] == "text")
        assert "data.xlsx" in text_part["text"]

    def test_audio_mention(self):
        files = [{
            "processed_type": "audio",
            "filename": "voice.mp3",
            "content": {"transcription": "placeholder"},
        }]
        parts = create_multimodal_content("", files)
        text_part = next(p for p in parts if p["type"] == "text")
        assert "voice.mp3" in text_part["text"]
