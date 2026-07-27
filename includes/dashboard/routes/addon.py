"""
Gmail Add-on API endpoints.

Authenticated via Google OIDC identity tokens from ScriptApp.getIdentityToken().
Domain-restricted to eagle-exports.com.au users.

Token verification:
  - Signature and expiry validated by google-auth library
  - Issuer must be accounts.google.com (implicit in verify_token)
  - hd (hosted domain) claim must equal eagle-exports.com.au

No audience (aud) check — the token is verified as a valid Google-issued
OIDC token, and domain membership is the access control gate.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel

from config.settings import Config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/addon", tags=["addon"])

# Cache a single transport instance for token verification
_google_request = google_requests.Request()

ALLOWED_DOMAIN = "eagle-exports.com"


# ── Auth dependency ────────────────────────────────────────────────────────

def verify_addon_token(request: Request) -> dict:
    """Verify OIDC identity token from Apps Script.

    Returns the decoded payload: sub, email, hd, name, iat, exp, etc.
    Raises 401 on invalid/missing token, 403 on wrong domain.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token = auth_header[7:]

    try:
        # verify_token validates: signature, expiry, issuer (accounts.google.com)
        # No audience check — we rely on domain validation instead.
        payload = id_token.verify_token(raw_token, request=_google_request)
    except ValueError as exc:
        logger.warning("Addon token verification failed: %s", exc)
        raise HTTPException(
            status_code=401,
            detail="Invalid identity token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Domain restriction — the hd claim is populated for Google Workspace users
    domain = payload.get("hd", "")
    if domain != ALLOWED_DOMAIN:
        logger.warning(
            "Addon access denied: domain=%r email=%r",
            domain,
            payload.get("email"),
        )
        raise HTTPException(
            status_code=403,
            detail=f"Domain {domain} not authorized",
        )

    return payload


AddonUser = Annotated[dict, Depends(verify_addon_token)]


# ── Request / Response models ──────────────────────────────────────────────

class ContextRequest(BaseModel):
    gmail_message_id: str
    gmail_thread_id: str
    subject: str | None = None
    sender: str | None = None


class ContextResponse(BaseModel):
    customer: dict | None = None
    supplier: dict | None = None
    rfq: dict | None = None
    opportunity: dict | None = None
    email_tracked: bool = False


class LinkEmailRequest(BaseModel):
    gmail_message_id: str
    gmail_thread_id: str
    link_type: str  # "customer" | "supplier" | "rfq"
    entity_id: str | None = None  # UUID for customer/supplier
    rfq_token: str | None = None  # RFQ number for rfq type
    sender: str | None = None  # optional — sender email for domain save
    save_domain: bool = True  # auto-save domain to entity for future matching


class MatchRequest(BaseModel):
    """Request to look up a sender email against known entities."""
    sender: str


class MatchResponse(BaseModel):
    matched: bool
    entity: dict | None = None  # {type, id, name, match_type} or None


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/match", response_model=MatchResponse)
def match_sender(body: MatchRequest, user: AddonUser):
    """Look up a sender email against known contacts, customers, and domains.

    Uses the same exact-match → domain-fallback logic as the automated
    Gmail sync pipeline.
    """
    if not body.sender:
        return MatchResponse(matched=False)

    from includes.dashboard.database import get_session
    from includes.gmail.matching import find_sender_match

    session = get_session()
    try:
        result = find_sender_match(session, body.sender)
        if result:
            return MatchResponse(matched=True, entity=result)
        return MatchResponse(matched=False)
    finally:
        session.close()


@router.post("/context", response_model=ContextResponse)
def get_email_context(body: ContextRequest, user: AddonUser):
    """Return linked entities (customer, supplier, RFQ, opportunity) for a Gmail message.

    Looks up EmailTracking by gmail_message_id, falling back to the latest
    tracked message in the same thread.
    """
    from includes.dashboard.database import get_session
    from includes.dashboard.models import (
        Customer,
        EmailTracking,
        Opportunity,
        RFQ,
        Supplier,
    )
    from uuid import UUID

    session = get_session()
    try:
        # Try exact message match first
        tracking = (
            session.query(EmailTracking)
            .filter(EmailTracking.gmail_message_id == body.gmail_message_id)
            .first()
        )
        if not tracking:
            # Fall back to latest message in the same thread
            tracking = (
                session.query(EmailTracking)
                .filter(EmailTracking.gmail_thread_id == body.gmail_thread_id)
                .order_by(EmailTracking.id.desc())
                .first()
            )

        if not tracking:
            return ContextResponse(email_tracked=False)

        result = ContextResponse(email_tracked=True)

        # Customer
        if tracking.customer_id:
            try:
                customer = session.query(Customer).get(tracking.customer_id)
                if customer and customer.companyname:
                    result.customer = {
                        "id": str(customer.id),
                        "name": customer.companyname,
                    }
            except Exception:
                pass

        # Supplier
        if tracking.supplier_id:
            try:
                supplier = session.query(Supplier).get(tracking.supplier_id)
                if supplier:
                    result.supplier = {
                        "id": str(supplier.id),
                        "name": supplier.name,
                    }
            except Exception:
                pass

        # RFQ — linked via rfq_token
        if tracking.rfq_token:
            try:
                rfq = (
                    session.query(RFQ)
                    .filter(RFQ.rfq_number == tracking.rfq_token)
                    .first()
                )
                if rfq:
                    result.rfq = {
                        "id": str(rfq.id),
                        "rfq_number": rfq.rfq_number,
                        "status": rfq.status or "draft",
                    }
            except Exception:
                pass

        # Opportunity — linked via opportunity_id (netsuite_id string)
        if tracking.opportunity_id:
            try:
                opp = (
                    session.query(Opportunity)
                    .filter(Opportunity.netsuite_id == tracking.opportunity_id)
                    .first()
                )
                if opp:
                    result.opportunity = {
                        "id": str(opp.id),
                        "title": opp.title or f"OP{opp.netsuite_id}",
                    }
            except Exception:
                pass

        return result
    finally:
        session.close()


# ── Search endpoint ────────────────────────────────────────────────────────

@router.get("/search")
def search_entities(type: str, q: str, user: AddonUser):
    """Search customers or suppliers by name for the email linking UI.

    Args:
        type: "customer" or "supplier"
        q: Search query (minimum 2 characters)
    """
    from fastapi.responses import JSONResponse
    from includes.dashboard.database import get_session
    from sqlalchemy import text

    if not q or len(q) < 2:
        return JSONResponse({"results": []})

    session = get_session()
    try:
        if type == "supplier":
            rows = session.execute(
                text(
                    "SELECT id, name FROM suppliers "
                    "WHERE LOWER(name) LIKE :q ORDER BY name LIMIT 10"
                ),
                {"q": f"%{q.lower()}%"},
            ).mappings().all()
            return JSONResponse({
                "results": [{"id": str(r["id"]), "name": r["name"]} for r in rows],
            })
        elif type == "customer":
            rows = session.execute(
                text(
                    "SELECT id, companyname FROM customers "
                    "WHERE LOWER(companyname) LIKE :q AND isinactive = false "
                    "ORDER BY companyname LIMIT 10"
                ),
                {"q": f"%{q.lower()}%"},
            ).mappings().all()
            return JSONResponse({
                "results": [
                    {"id": str(r["id"]), "name": r["companyname"]}
                    for r in rows
                ],
            })
        elif type == "rfq":
            rows = session.execute(
                text(
                    "SELECT r.id, r.rfq_number, r.status, r.customer, "
                    "r.created_date, r.opportunity_id "
                    "FROM rfqs r "
                    "LEFT JOIN customers c ON r.customer_id = c.id "
                    "WHERE "
                    "  LOWER(r.rfq_number) LIKE :q "
                    "  OR LOWER(r.customer) LIKE :q "
                    "  OR (r.opportunity_id IS NOT NULL AND r.opportunity_id::text LIKE :q) "
                    "ORDER BY r.created_date DESC "
                    "LIMIT 10"
                ),
                {"q": f"%{q.lower()}%"},
            ).mappings().all()
            return JSONResponse({
                "results": [
                    {
                        "id": str(r["id"]),
                        "rfq_number": r["rfq_number"],
                        "status": r["status"] or "draft",
                        "customer": r["customer"],
                        "opportunity_id": str(r["opportunity_id"]) if r["opportunity_id"] else None,
                    }
                    for r in rows
                ],
            })
        else:
            return JSONResponse(
                {"results": [], "error": "type must be 'customer', 'supplier', or 'rfq'"},
                status_code=400,
            )
    finally:
        session.close()


# ── Link-email endpoint ────────────────────────────────────────────────────

@router.post("/link-email")
def link_email(body: LinkEmailRequest, user: AddonUser):
    """Link a Gmail message (and its thread) to a customer or supplier.

    Looks up the EmailTracking record by gmail_message_id (fallback to
    thread), then updates the link for all messages in the same thread.
    """
    from fastapi.responses import JSONResponse
    from includes.dashboard.database import get_session
    from includes.dashboard.models import Customer, EmailTracking, Supplier
    from sqlalchemy import text
    from uuid import UUID

    session = get_session()
    try:
        # Find the tracking record
        tracking = (
            session.query(EmailTracking)
            .filter(EmailTracking.gmail_message_id == body.gmail_message_id)
            .first()
        )
        if not tracking:
            tracking = (
                session.query(EmailTracking)
                .filter(EmailTracking.gmail_thread_id == body.gmail_thread_id)
                .order_by(EmailTracking.id.desc())
                .first()
            )

        if not tracking:
            return JSONResponse(
                {"status": "error", "message": "Email not found in tracking"},
                status_code=404,
            )

        tid = tracking.gmail_thread_id or ""

        if body.link_type == "customer":
            try:
                customer_id = UUID(body.entity_id)
            except ValueError:
                return JSONResponse(
                    {"status": "error", "message": "Invalid entity ID"},
                    status_code=400,
                )

            customer = session.query(Customer).get(customer_id)
            if not customer:
                return JSONResponse(
                    {"status": "error", "message": "Customer not found"},
                    status_code=404,
                )

            # Update all messages in the thread
            session.execute(
                text(
                    "UPDATE email_tracking "
                    "SET customer_id = :cid, match_type = 'manual' "
                    "WHERE gmail_thread_id = :tid OR id = :eid"
                ),
                {"cid": str(customer_id), "tid": tid, "eid": tracking.id},
            )

            # Auto-save sender domain if requested
            if body.save_domain and body.sender:
                from includes.gmail.matching import save_sender_domain
                save_sender_domain(session, body.sender, "customer", customer_id)

            session.commit()
            return JSONResponse({
                "status": "ok",
                "message": f"Linked to {customer.companyname}",
                "entity_name": customer.companyname,
            })

        elif body.link_type == "supplier":
            try:
                supplier_id = UUID(body.entity_id)
            except ValueError:
                return JSONResponse(
                    {"status": "error", "message": "Invalid entity ID"},
                    status_code=400,
                )

            supplier = session.query(Supplier).get(supplier_id)
            if not supplier:
                return JSONResponse(
                    {"status": "error", "message": "Supplier not found"},
                    status_code=404,
                )

            # Update all messages in the thread
            session.execute(
                text(
                    "UPDATE email_tracking "
                    "SET supplier_id = :sid, match_type = 'manual' "
                    "WHERE gmail_thread_id = :tid OR id = :eid"
                ),
                {"sid": str(supplier_id), "tid": tid, "eid": tracking.id},
            )

            # Auto-save sender domain if requested
            if body.save_domain and body.sender:
                from includes.gmail.matching import save_sender_domain
                save_sender_domain(session, body.sender, "supplier", supplier_id)

            session.commit()

            # Trigger quote pipeline if any received email in thread
            # has an rfq_token — now linked to both supplier + RFQ
            _maybe_trigger_quote_pipeline(session, tid)

            return JSONResponse({
                "status": "ok",
                "message": f"Linked to {supplier.name}",
                "entity_name": supplier.name,
            })

        elif body.link_type == "rfq":
            rfq_token = (body.rfq_token or "").strip()
            if not rfq_token:
                return JSONResponse(
                    {"status": "error", "message": "No RFQ token provided"},
                    status_code=400,
                )

            # Update all messages in the thread
            session.execute(
                text(
                    "UPDATE email_tracking "
                    "SET rfq_token = :token, match_type = 'manual' "
                    "WHERE gmail_thread_id = :tid OR id = :eid"
                ),
                {"token": rfq_token, "tid": tid, "eid": tracking.id},
            )
            session.commit()

            # Trigger quote pipeline if any received email in thread
            # has a supplier_id — now linked to both RFQ + supplier
            _maybe_trigger_quote_pipeline(session, tid)

            return JSONResponse({
                "status": "ok",
                "message": f"Linked to {rfq_token}",
                "entity_name": rfq_token,
            })

        else:
            return JSONResponse(
                {"status": "error", "message": "link_type must be 'customer', 'supplier', or 'rfq'"},
                status_code=400,
            )

    except Exception as exc:
        session.rollback()
        logger.exception("Error linking email")
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
        )
    finally:
        session.close()


def _maybe_trigger_quote_pipeline(session, gmail_thread_id: str):
    """Trigger the supplier quote pipeline if the thread has received emails
    linked to both a supplier and an RFQ.

    Finds the first received email in the thread that meets the criteria
    and queues it for quotation analysis.
    """
    from includes.dashboard.models import EmailTracking

    if not gmail_thread_id:
        return

    # Find all received emails in the thread linked to both
    # supplier and RFQ — the pipeline handles non-quote emails gracefully.
    candidates = (
        session.query(EmailTracking)
        .filter(
            EmailTracking.gmail_thread_id == gmail_thread_id,
            EmailTracking.direction == "received",
            EmailTracking.supplier_id.isnot(None),
            (
                EmailTracking.rfq_token.isnot(None)
                | EmailTracking.rfq_id.isnot(None)
            ),
        )
        .all()
    )

    for candidate in candidates:
        from includes.tools.supplier_quote_pipeline import trigger_supplier_quote_pipeline
        trigger_supplier_quote_pipeline(candidate.id, user_id="addon")
        logger.info(
            "Triggered quote pipeline for email #%d (thread %s) via Gmail add-on",
            candidate.id,
            gmail_thread_id,
        )
