"""API routes: latest-thread lookup, RFQ ↔ thread binding, Gmail attachment proxy."""

import base64
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import Request, Depends
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from includes.dashboard.models import RFQThread, EmailTracking
from . import _helpers
from ._helpers import router, require_user

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Latest thread (for iframe resume on page reload)
# ---------------------------------------------------------------------------
@router.get("/api/latest-thread")
def latest_thread(user: dict = Depends(require_user), rfq_id: str | None = None):
    """Return the user's thread for an RFQ, or their most recent Chainlit thread."""
    session = _helpers.get_session()
    try:
        # If rfq_id is provided, look up the bound thread first
        if rfq_id:
            thread_id = _lookup_rfq_thread_id(rfq_id, user["email"])
            if thread_id:
                return JSONResponse({"thread_id": thread_id})

        # Fall back to most recent thread
        row = session.execute(
            text("""
                SELECT t."id"
                FROM threads t
                LEFT JOIN steps s ON t."id" = s."threadId"
                WHERE t."userIdentifier" = :email
                GROUP BY t."id"
                ORDER BY COALESCE(MAX(s."createdAt"), t."createdAt") DESC
                LIMIT 1
            """),
            {"email": user["email"]},
        ).fetchone()
    finally:
        session.close()
    return JSONResponse({"thread_id": row[0] if row else None})


# ---------------------------------------------------------------------------
# RFQ ↔ Thread binding
# ---------------------------------------------------------------------------

def _lookup_rfq_thread_id(rfq_number: str, user_email: str) -> str | None:
    """Return the thread_id bound to this RFQ for the given user.

    If no binding exists, creates a new Chainlit thread owned by the user,
    binds it to the RFQ, and returns the new thread_id.

    Verifies existing bindings point to a thread owned by this user.
    If not, removes the stale binding and creates a fresh thread.
    """
    session = _helpers.get_session()
    try:
        row = session.query(RFQThread).filter(
            RFQThread.rfq_number == rfq_number,
            RFQThread.user_email == user_email,
        ).first()

        if row:
            # Verify the thread is owned by this user
            owner = session.execute(
                text('SELECT "userIdentifier" FROM threads WHERE id = :tid'),
                {"tid": row.thread_id},
            ).scalar()
            if owner and owner != user_email:
                logger.warning(
                    "RFQ %s: removing stale thread binding %s (owned by %s, not %s)",
                    rfq_number, row.thread_id, owner, user_email,
                )
                session.delete(row)
                session.commit()
            else:
                return row.thread_id

        # No valid binding — create a new thread for this user+RFQ
        new_thread_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Look up the user's Chainlit userId
        user_id = session.execute(
            text('SELECT id FROM users WHERE identifier = :email'),
            {"email": user_email},
        ).scalar()

        # Insert the thread into Chainlit's threads table
        session.execute(
            text('''
                INSERT INTO threads (id, "createdAt", name, "userId", "userIdentifier")
                VALUES (:id, :created_at, :name, :user_id, :user_identifier)
            '''),
            {
                "id": new_thread_id,
                "created_at": now,
                "name": rfq_number,
                "user_id": str(user_id) if user_id else None,
                "user_identifier": user_email,
            },
        )

        # Bind the new thread to the RFQ for this user
        session.add(RFQThread(
            rfq_number=rfq_number,
            user_email=user_email,
            thread_id=new_thread_id,
        ))
        session.commit()

        logger.info("RFQ %s: created new thread %s for user %s", rfq_number, new_thread_id, user_email)
        return new_thread_id
    finally:
        session.close()


@router.get("/api/rfq-thread")
def get_rfq_thread(rfq_id: str, user: dict = Depends(require_user)):
    """Return the thread_id bound to this RFQ for the current user, or null."""
    thread_id = _lookup_rfq_thread_id(rfq_id, user["email"])
    return JSONResponse({"thread_id": thread_id})


@router.post("/api/rfq-thread")
async def bind_rfq_thread(request: Request, user: dict = Depends(require_user)):
    """Bind (or rebind) a thread to an RFQ for the current user."""
    body = await request.json()
    rfq_id = body.get("rfq_id")
    thread_id = body.get("thread_id")
    if not rfq_id or not thread_id:
        return JSONResponse({"error": "rfq_id and thread_id required"}, status_code=400)

    session = _helpers.get_session()
    try:
        # Check if this thread is already bound to a DIFFERENT RFQ for this user.
        # Never hijack a thread that belongs to another RFQ.
        thread_bound = session.query(RFQThread).filter(
            RFQThread.thread_id == thread_id,
            RFQThread.user_email == user["email"],
        ).first()
        if thread_bound and thread_bound.rfq_number != rfq_id:
            return JSONResponse(
                {"error": "thread_already_bound", "bound_to": thread_bound.rfq_number},
                status_code=409,
            )

        existing = session.query(RFQThread).filter(
            RFQThread.rfq_number == rfq_id,
            RFQThread.user_email == user["email"],
        ).first()
        if existing:
            existing.thread_id = thread_id
        else:
            session.add(RFQThread(
                rfq_number=rfq_id,
                user_email=user["email"],
                thread_id=thread_id,
            ))
        session.commit()
    finally:
        session.close()

    return JSONResponse({"ok": True, "rfq_id": rfq_id, "thread_id": thread_id})


@router.delete("/api/rfq-thread")
async def unbind_rfq_thread(rfq_id: str, user: dict = Depends(require_user)):
    """Remove the binding between this RFQ and its thread.

    The thread itself is kept (it remains a normal chat). Used by the beta
    chat UI's "Clear thread" action, which then creates and binds a fresh one.
    """
    session = _helpers.get_session()
    try:
        deleted = (
            session.query(RFQThread)
            .filter(
                RFQThread.rfq_number == rfq_id,
                RFQThread.user_email == user["email"],
            )
            .delete()
        )
        session.commit()
    finally:
        session.close()
    return JSONResponse({"ok": True, "cleared": bool(deleted)})


# ---------------------------------------------------------------------------
# Gmail attachment proxy
# ---------------------------------------------------------------------------
# Simple in-memory cache to avoid repeated Gmail API calls for the same attachment.
# Key: "{message_id}:{attachment_id}" → (expiry_timestamp, bytes)
_attach_cache: dict[str, tuple[float, bytes]] = {}
_ATTACH_CACHE_TTL = 3600  # 1 hour


@router.get("/api/gmail/attachments/{message_id}/{attachment_id}")
async def get_gmail_attachment(
    message_id: str,
    attachment_id: str,
    user: dict = Depends(require_user),
):
    """Proxy a Gmail attachment — fetches from Gmail API on demand, caches in memory."""
    cache_key = f"{message_id}:{attachment_id}"
    now = time.monotonic()

    # Check cache
    if cache_key in _attach_cache:
        expiry, data = _attach_cache[cache_key]
        if now < expiry:
            return Response(content=data, media_type="application/octet-stream")

    # Look up the email_tracking record to find the mailbox user
    session = _helpers.get_session()
    try:
        tracking = session.query(EmailTracking).filter(
            EmailTracking.gmail_message_id == message_id
        ).first()
        if not tracking:
            return Response(content=b"Not found", status_code=404, media_type="text/plain")
    finally:
        session.close()

    # Fetch from Gmail API
    try:
        from includes.gmail import get_gmail_client

        service = get_gmail_client(tracking.user_email)
        attachment = (
            service.users().messages().attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        data = base64.urlsafe_b64decode(attachment["data"])
    except Exception as e:
        logger.warning(f"Failed to fetch Gmail attachment {message_id}/{attachment_id}: {e}")
        return Response(content=b"Attachment unavailable", status_code=404, media_type="text/plain")

    # Determine MIME type from the stored attachments_json
    mime_type = "application/octet-stream"
    if tracking.attachments_json:
        for a in tracking.attachments_json:
            if a.get("gmail_attachment_id") == attachment_id:
                mime_type = a.get("mime_type", mime_type)
                break

    # Cache and return
    _attach_cache[cache_key] = (now + _ATTACH_CACHE_TTL, data)
    return Response(content=data, media_type=mime_type)
