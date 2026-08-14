"""Transient, owner-scoped storage for email attachments.

Files live under DATA_DIR/email_uploads/<sha256(user_email)[:16]>/<upload_id>/:

    file        # raw bytes
    meta.json   # {filename, mime_type, size, created}

Nothing here is persisted long-term: cleanup is driven by sweep_expired()
(called from the maintenance loop) plus explicit deletes when the user
removes an attachment chip. Gmail is the store of record once the email
is sent or drafted.
"""

import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from config.settings import Config

UPLOAD_ROOT = Path(Config.DATA_DIR) / "email_uploads"
MAX_BYTES = Config.EMAIL_ATTACHMENT_MAX_MB * 1024 * 1024
TOTAL_MAX_BYTES = Config.EMAIL_ATTACHMENT_TOTAL_MB * 1024 * 1024

# Per-user staging quota — generous, only guards against runaway uploads
USER_QUOTA_BYTES = 100 * 1024 * 1024

_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")


def _owner_key(user_email: str) -> str:
    return hashlib.sha256((user_email or "").strip().lower().encode()).hexdigest()[:16]


def _safe_filename(name: str) -> str:
    name = Path(name or "attachment").name.replace("\x00", "")
    name = re.sub(r"[\r\n]", "", name)[:200]
    return name or "attachment"


def _validate_upload_id(upload_id: str) -> None:
    if not _UPLOAD_ID_RE.fullmatch(upload_id or ""):
        raise ValueError("Invalid upload id")


def _owner_dir_size(owner_dir: Path) -> int:
    """Total bytes already staged for this owner (best effort)."""
    total = 0
    if owner_dir.is_dir():
        for f in owner_dir.glob("*/file"):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def save_upload(user_email: str, filename: str, mime_type: str, data: bytes) -> dict:
    """Store an uploaded file and return its metadata (no data bytes)."""
    if not data:
        raise ValueError("Empty file")
    if len(data) > MAX_BYTES:
        raise ValueError(f"File exceeds {Config.EMAIL_ATTACHMENT_MAX_MB}MB limit")

    owner_dir = UPLOAD_ROOT / _owner_key(user_email)
    if _owner_dir_size(owner_dir) + len(data) > USER_QUOTA_BYTES:
        raise ValueError("Upload quota exceeded")

    upload_id = uuid.uuid4().hex
    dest = owner_dir / upload_id
    dest.mkdir(parents=True, exist_ok=True)  # dir is absent in the Docker image
    (dest / "file").write_bytes(data)
    meta = {
        "filename": _safe_filename(filename),
        "mime_type": mime_type or "application/octet-stream",
        "size": len(data),
        "created": time.time(),
    }
    (dest / "meta.json").write_text(json.dumps(meta))
    return {"upload_id": upload_id, **meta}


def load_upload(user_email: str, upload_id: str) -> dict:
    """Load an upload owned by user_email, including its raw data bytes."""
    _validate_upload_id(upload_id)
    d = UPLOAD_ROOT / _owner_key(user_email) / upload_id
    meta_path = d / "meta.json"
    if not meta_path.exists():
        raise ValueError("Upload not found or expired")
    meta = json.loads(meta_path.read_text())
    return {**meta, "data": (d / "file").read_bytes()}


def delete_upload(user_email: str, upload_id: str) -> bool:
    """Delete an owner's upload. Raises ValueError on malformed ids."""
    _validate_upload_id(upload_id)
    d = UPLOAD_ROOT / _owner_key(user_email) / upload_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def sweep_expired(ttl_hours: float) -> int:
    """Remove uploads older than ttl_hours; prune empty owner dirs.

    Called from the maintenance loop. Returns number of upload dirs removed.
    """
    removed = 0
    if not UPLOAD_ROOT.is_dir():
        return 0
    now = time.time()
    cutoff = now - float(ttl_hours) * 3600
    for owner_dir in UPLOAD_ROOT.iterdir():
        if not owner_dir.is_dir():
            continue
        for up_dir in owner_dir.iterdir():
            if not up_dir.is_dir():
                continue
            try:
                meta = json.loads((up_dir / "meta.json").read_text())
                created = float(meta.get("created", now))
            except (OSError, ValueError, json.JSONDecodeError):
                try:  # partially written dir — fall back to mtime
                    created = up_dir.stat().st_mtime
                except OSError:
                    created = now
            if created < cutoff:
                shutil.rmtree(up_dir, ignore_errors=True)
                removed += 1
        try:  # remove now-empty owner dirs
            owner_dir.rmdir()
        except OSError:
            pass
    return removed
