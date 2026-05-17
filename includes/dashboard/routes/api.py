"""API routes: latest-thread lookup and RFQ ↔ thread binding."""

import logging

from fastapi import Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from includes.dashboard.models import RFQThread
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
            rfq_thread = session.query(RFQThread).filter(
                RFQThread.rfq_number == rfq_id,
                RFQThread.user_email == user["email"],
            ).first()
            if rfq_thread:
                return JSONResponse({"thread_id": rfq_thread.thread_id})

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
    """Return the thread_id bound to this RFQ for the given user, or None."""
    session = _helpers.get_session()
    try:
        row = session.query(RFQThread).filter(
            RFQThread.rfq_number == rfq_number,
            RFQThread.user_email == user_email,
        ).first()
        if not row:
            return None
        return row.thread_id
    finally:
        session.close()


@router.get("/api/rfq-thread")
def get_rfq_thread(rfq_id: str, user: dict = Depends(require_user)):
    """Return the thread_id bound to this RFQ for the current user, or null."""
    session = _helpers.get_session()
    try:
        row = session.query(RFQThread).filter(
            RFQThread.rfq_number == rfq_id,
            RFQThread.user_email == user["email"],
        ).first()
    finally:
        session.close()
    return JSONResponse({"thread_id": row.thread_id if row else None})


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
