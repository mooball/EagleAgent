"""Tests for includes/dashboard/email_uploads.py — transient upload store."""

import json
import time

import pytest

from includes.dashboard import email_uploads


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    root = tmp_path / "email_uploads"
    monkeypatch.setattr(email_uploads, "UPLOAD_ROOT", root)
    return root


class TestSaveAndLoad:
    def test_round_trip(self, tmp_store):
        info = email_uploads.save_upload(
            "staff@eagle.com", "quote.pdf", "application/pdf", b"%PDF-1.4 fake"
        )
        assert info["filename"] == "quote.pdf"
        assert info["mime_type"] == "application/pdf"
        assert info["size"] == len(b"%PDF-1.4 fake")
        assert len(info["upload_id"]) == 32

        loaded = email_uploads.load_upload("staff@eagle.com", info["upload_id"])
        assert loaded["data"] == b"%PDF-1.4 fake"
        assert loaded["filename"] == "quote.pdf"

    def test_ownership_enforced(self, tmp_store):
        info = email_uploads.save_upload("staff@eagle.com", "q.pdf", "application/pdf", b"x")
        with pytest.raises(ValueError):
            email_uploads.load_upload("other@eagle.com", info["upload_id"])

    def test_invalid_upload_id_rejected(self, tmp_store):
        for bad in ["", "abc", "../etc", "0" * 31, "g" * 32, "0" * 33, "0" * 32 + ".."]:
            with pytest.raises(ValueError):
                email_uploads.load_upload("staff@eagle.com", bad)
            with pytest.raises(ValueError):
                email_uploads.delete_upload("staff@eagle.com", bad)

    def test_missing_upload(self, tmp_store):
        with pytest.raises(ValueError):
            email_uploads.load_upload("staff@eagle.com", "a" * 32)

    def test_oversized_rejected(self, tmp_store, monkeypatch):
        monkeypatch.setattr(email_uploads, "MAX_BYTES", 1024)
        with pytest.raises(ValueError):
            email_uploads.save_upload(
                "staff@eagle.com", "big.bin", "application/octet-stream", b"x" * 1025
            )

    def test_empty_rejected(self, tmp_store):
        with pytest.raises(ValueError):
            email_uploads.save_upload("staff@eagle.com", "e.bin", "", b"")

    def test_filename_sanitized(self, tmp_store):
        info = email_uploads.save_upload(
            "staff@eagle.com", "../../evil\r\nname.pdf", "application/pdf", b"x"
        )
        assert info["filename"] == "evilname.pdf"
        assert ".." not in info["filename"]
        assert "\r" not in info["filename"]
        assert "\n" not in info["filename"]


class TestDelete:
    def test_delete_removes_dir(self, tmp_store):
        info = email_uploads.save_upload("staff@eagle.com", "q.pdf", "application/pdf", b"x")
        assert email_uploads.delete_upload("staff@eagle.com", info["upload_id"]) is True
        with pytest.raises(ValueError):
            email_uploads.load_upload("staff@eagle.com", info["upload_id"])

    def test_delete_foreign_id_returns_false(self, tmp_store):
        info = email_uploads.save_upload("staff@eagle.com", "q.pdf", "application/pdf", b"x")
        assert email_uploads.delete_upload("other@eagle.com", info["upload_id"]) is False
        # original still intact for its owner
        assert email_uploads.load_upload("staff@eagle.com", info["upload_id"])["data"] == b"x"


class TestSweep:
    def test_sweep_expired_only(self, tmp_store):
        old = email_uploads.save_upload("staff@eagle.com", "old.pdf", "application/pdf", b"o")
        fresh = email_uploads.save_upload("staff@eagle.com", "new.pdf", "application/pdf", b"n")

        meta_path = (
            tmp_store / email_uploads._owner_key("staff@eagle.com") / old["upload_id"] / "meta.json"
        )
        meta = json.loads(meta_path.read_text())
        meta["created"] = time.time() - 7200  # 2 hours old
        meta_path.write_text(json.dumps(meta))

        removed = email_uploads.sweep_expired(1)  # 1-hour TTL
        assert removed == 1

        assert email_uploads.load_upload("staff@eagle.com", fresh["upload_id"])["data"] == b"n"
        with pytest.raises(ValueError):
            email_uploads.load_upload("staff@eagle.com", old["upload_id"])

    def test_sweep_missing_root_returns_zero(self, tmp_store):
        assert email_uploads.sweep_expired(1) == 0


class TestResolveAttachments:
    """Send-side resolution in includes/dashboard/routes/rfqs.py."""

    def test_total_size_cap(self, tmp_store, monkeypatch):
        from includes.dashboard.routes.rfqs import _resolve_attachments

        monkeypatch.setattr(email_uploads, "TOTAL_MAX_BYTES", 1024)
        a = email_uploads.save_upload("staff@eagle.com", "a.pdf", "application/pdf", b"x" * 700)
        b = email_uploads.save_upload("staff@eagle.com", "b.pdf", "application/pdf", b"y" * 700)
        with pytest.raises(ValueError, match="total limit"):
            _resolve_attachments(
                "staff@eagle.com",
                [{"upload_id": a["upload_id"]}, {"upload_id": b["upload_id"]}],
            )

    def test_foreign_upload_rejected(self, tmp_store):
        from includes.dashboard.routes.rfqs import _resolve_attachments

        info = email_uploads.save_upload("staff@eagle.com", "a.pdf", "application/pdf", b"x")
        with pytest.raises(ValueError, match="not found|expired"):
            _resolve_attachments("other@eagle.com", [{"upload_id": info["upload_id"]}])

    def test_bad_payload_shapes(self, tmp_store):
        from includes.dashboard.routes.rfqs import _resolve_attachments

        with pytest.raises(ValueError):
            _resolve_attachments("staff@eagle.com", "not-a-list")
        with pytest.raises(ValueError):
            _resolve_attachments("staff@eagle.com", [{"nope": 1}])
        with pytest.raises(ValueError):
            _resolve_attachments("staff@eagle.com", [{"upload_id": "not-hex"}])

    def test_empty_returns_empty(self, tmp_store):
        from includes.dashboard.routes.rfqs import _resolve_attachments

        assert _resolve_attachments("staff@eagle.com", None) == []
        assert _resolve_attachments("staff@eagle.com", []) == []
