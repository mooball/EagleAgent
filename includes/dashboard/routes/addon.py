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
    direction: str | None = None  # 'received' | 'sent'
    sender_email: str | None = None  # actual FROM address
    recipient_email: str | None = None  # primary TO address
    user_email: str | None = None  # mailbox owner


class ContextResponse(BaseModel):
    customer: dict | None = None
    supplier: dict | None = None
    rfq: dict | None = None
    opportunity: dict | None = None
    email_tracked: bool = False
    pipeline_status: dict | None = None  # {classification, reason} from supplier quote pipeline


class LinkEmailRequest(BaseModel):
    gmail_message_id: str
    gmail_thread_id: str
    link_type: str  # "customer" | "supplier" | "rfq"
    entity_id: str | None = None  # UUID for customer/supplier
    rfq_token: str | None = None  # RFQ number for rfq type
    sender: str | None = None  # optional — sender email for domain save
    save_domain: bool = True  # auto-save domain to entity for future matching
    direction: str | None = None  # 'received' | 'sent'
    sender_email: str | None = None
    recipient_email: str | None = None
    user_email: str | None = None


class MatchRequest(BaseModel):
    """Request to look up a sender email against known entities."""
    sender: str


class UnlinkEmailRequest(BaseModel):
    gmail_message_id: str
    gmail_thread_id: str
    link_type: str  # "customer" | "supplier" | "rfq"


class MatchResponse(BaseModel):
    matched: bool
    entity: dict | None = None  # {type, id, name, match_type} or None


class CreateRfqRequest(BaseModel):
    gmail_message_id: str
    gmail_thread_id: str
    direction: str | None = None
    sender_email: str | None = None
    recipient_email: str | None = None
    user_email: str | None = None


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
            # Auto-create tracking record — email hasn't been synced yet
            tracking = _ensure_email_tracking(
                session,
                gmail_message_id=body.gmail_message_id,
                gmail_thread_id=body.gmail_thread_id,
                subject=body.subject or "",
                sender=body.sender or "",
                direction=body.direction,
                sender_email=body.sender_email,
                recipient_email=body.recipient_email,
                user_email=body.user_email or user.get("email", ""),
            )
            session.commit()
            logger.info(f"[addon] auto-created tracking #{tracking.id} for {body.gmail_message_id}")

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
                    op_number = opp.opportunity_number
                    op_title = opp.title
                    if op_number and op_title:
                        display = f"{op_number} \u2014 {op_title}"
                    else:
                        display = op_title or op_number or f"OP{opp.netsuite_id}"
                    result.opportunity = {
                        "id": str(opp.id),
                        "title": display,
                    }
            except Exception:
                pass

        # Opportunity inherited from linked RFQ (RFQ linked to Opp in dashboard)
        if not result.opportunity and tracking.rfq_token and result.rfq:
            try:
                rfq_orm = (
                    session.query(RFQ)
                    .filter(RFQ.rfq_number == tracking.rfq_token)
                    .first()
                )
                if rfq_orm and rfq_orm.opportunity_id:
                    opp = session.query(Opportunity).get(rfq_orm.opportunity_id)
                    if opp:
                        op_number = opp.opportunity_number
                        op_title = opp.title
                        if op_number and op_title:
                            display = f"{op_number} \u2014 {op_title}"
                        else:
                            display = op_title or op_number or f"OP{opp.netsuite_id}"
                        result.opportunity = {
                            "id": str(opp.id),
                            "title": display,
                        }
            except Exception:
                pass

        # Pipeline classification (supplier quote pipeline result)
        if tracking.supplier_pipeline_result:
            pr = tracking.supplier_pipeline_result
            if isinstance(pr, dict) and pr.get("classification"):
                result.pipeline_status = {
                    "classification": pr["classification"],
                    "reason": pr.get("reason", ""),
                }

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
                    "r.created_date, o.opportunity_number AS op_number "
                    "FROM rfqs r "
                    "LEFT JOIN customers c ON r.customer_id = c.id "
                    "LEFT JOIN opportunities o ON r.opportunity_id = o.id "
                    "WHERE "
                    "  LOWER(r.rfq_number) LIKE :q "
                    "  OR LOWER(r.customer) LIKE :q "
                    "  OR (o.opportunity_number IS NOT NULL AND LOWER(o.opportunity_number) LIKE :q) "
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
                        "opportunity_id": r["op_number"],
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
            # Auto-create tracking record — email hasn't been synced yet
            tracking = _ensure_email_tracking(
                session,
                gmail_message_id=body.gmail_message_id,
                gmail_thread_id=body.gmail_thread_id,
                sender=body.sender or "",
                direction=body.direction,
                sender_email=body.sender_email,
                recipient_email=body.recipient_email,
                user_email=body.user_email or user.get("email", ""),
            )
            session.commit()
            logger.info(f"[addon] auto-created tracking #{tracking.id} for link-email")

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

            # Auto-save sender domain if requested (skip internal domains)
            if body.save_domain and body.sender:
                from includes.gmail.matching import save_sender_domain, _INTERNAL_DOMAINS, _GENERIC_DOMAINS
                sender_domain = body.sender.split("@")[-1].lower() if "@" in body.sender else ""
                if sender_domain and sender_domain not in _INTERNAL_DOMAINS and sender_domain not in _GENERIC_DOMAINS:
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

            # Auto-save sender domain if requested (skip internal domains)
            if body.save_domain and body.sender:
                from includes.gmail.matching import save_sender_domain, _INTERNAL_DOMAINS, _GENERIC_DOMAINS
                sender_domain = body.sender.split("@")[-1].lower() if "@" in body.sender else ""
                if sender_domain and sender_domain not in _INTERNAL_DOMAINS and sender_domain not in _GENERIC_DOMAINS:
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


# ── Unlink-email endpoint ──────────────────────────────────────────────────

@router.post("/unlink")
def unlink_email(body: UnlinkEmailRequest, user: AddonUser):
    """Unlink a Gmail message (and its thread) from a customer, supplier, or RFQ.

    Clears the relevant field for all messages in the same thread.
    """
    from fastapi.responses import JSONResponse
    from includes.dashboard.database import get_session
    from includes.dashboard.models import EmailTracking
    from sqlalchemy import text

    session = get_session()
    try:
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
            session.execute(
                text(
                    "UPDATE email_tracking SET customer_id = NULL "
                    "WHERE gmail_thread_id = :tid"
                ),
                {"tid": tid},
            )
            session.commit()
            return JSONResponse({
                "status": "ok",
                "message": "Unlinked from customer",
            })

        elif body.link_type == "supplier":
            session.execute(
                text(
                    "UPDATE email_tracking SET supplier_id = NULL "
                    "WHERE gmail_thread_id = :tid"
                ),
                {"tid": tid},
            )
            session.commit()
            return JSONResponse({
                "status": "ok",
                "message": "Unlinked from supplier",
            })

        elif body.link_type == "rfq":
            session.execute(
                text(
                    "UPDATE email_tracking SET rfq_token = NULL "
                    "WHERE gmail_thread_id = :tid"
                ),
                {"tid": tid},
            )
            session.commit()
            return JSONResponse({
                "status": "ok",
                "message": "Unlinked from RFQ",
            })

        else:
            return JSONResponse(
                {"status": "error", "message": "link_type must be 'customer', 'supplier', or 'rfq'"},
                status_code=400,
            )

    except Exception as exc:
        session.rollback()
        logger.exception("Error unlinking email")
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
        )
    finally:
        session.close()


@router.post("/create-rfq")
def create_rfq(body: CreateRfqRequest, user: AddonUser):
    """Create an RFQ from a Gmail email thread.

    Finds the EmailTracking record, triggers the RFQ creation pipeline
    in a background thread, and returns immediately.
    """
    from fastapi.responses import JSONResponse
    from includes.dashboard.database import get_session
    from includes.dashboard.models import EmailTracking

    session = get_session()
    try:
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
            # Auto-create tracking record — email hasn't been synced yet
            tracking = _ensure_email_tracking(
                session,
                gmail_message_id=body.gmail_message_id,
                gmail_thread_id=body.gmail_thread_id,
                direction=body.direction,
                sender_email=body.sender_email,
                recipient_email=body.recipient_email,
                user_email=body.user_email or user.get("email", ""),
            )
            session.commit()
            logger.info(f"[addon] auto-created tracking #{tracking.id} for create-rfq")

        # Guard: must be linked to a customer
        if not tracking.customer_id:
            return JSONResponse(
                {"status": "error", "message": "No customer linked. Link a customer first."},
                status_code=400,
            )

        # Guard: must not already be linked to an RFQ
        if tracking.rfq_token or tracking.rfq_id:
            return JSONResponse(
                {"status": "error",
                 "message": f"Already linked to RFQ {tracking.rfq_token or tracking.rfq_id}"},
                status_code=400,
            )

        # Guard: idempotency — already processing or completed
        if tracking.rfq_creation_result:
            existing = tracking.rfq_creation_result
            if existing.get("status") == "error":
                return JSONResponse(
                    {"status": "error",
                     "message": f"Previous attempt failed: {existing.get('error', 'unknown')}"},
                    status_code=400,
                )
            return JSONResponse({
                "status": "ok",
                "message": f"RFQ already created: {existing.get('rfq_number', 'unknown')}",
            })

        user_ident = user.get("email", user.get("identifier", "addon"))

        # Create the RFQ synchronously so we can return the updated context
        from includes.dashboard.models import Customer
        from includes.tools.rfq_crud import _create_rfq_sync
        from sqlalchemy import text as sa_text

        customer_name = "Unknown"
        rfq_number = None

        # Re-open a fresh session for the synchronous write
        session2 = get_session()
        try:
            customer = session2.query(Customer).get(tracking.customer_id)
            customer_name = customer.companyname if customer else "Unknown"

            rfq = _create_rfq_sync(
                data={
                    "customer": customer_name,
                    "customer_id": str(tracking.customer_id),
                    "status": "in_progress",
                    "assigned_to": tracking.user_email or user_ident,
                    "reference": tracking.subject or "",
                },
                user_id=user_ident,
            )
            if isinstance(rfq, str):
                return JSONResponse({"status": "error", "message": rfq}, status_code=500)

            rfq_number = rfq["rfq_number"]

            # Link the email thread to the new RFQ
            session2.execute(
                sa_text(
                    "UPDATE email_tracking SET rfq_token = :token, match_type = 'manual' "
                    "WHERE gmail_thread_id = :tid"
                ),
                {"token": rfq_number, "tid": tracking.gmail_thread_id},
            )
            session2.commit()

            # Auto-create NetSuite Opportunity
            if customer and customer.netsuite_id:
                try:
                    from includes.netsuite.records.opportunity import create_and_link_opportunity
                    create_and_link_opportunity(rfq_number)
                except Exception:
                    logger.exception("Opportunity creation failed (non-fatal)")

        except Exception:
            session2.rollback()
            raise
        finally:
            session2.close()

        # Spawn LLM extraction in background (non-blocking)
        from includes.tools.rfq_creation_pipeline import trigger_rfq_creation_pipeline
        trigger_rfq_creation_pipeline(tracking.id, user_id=user_ident)

        # Build updated context to return (matches ContextResponse format)
        updated_context = {
            "customer": {
                "id": str(tracking.customer_id),
                "name": customer_name,
            },
            "supplier": None,
            "rfq": {
                "id": str(rfq["id"]) if isinstance(rfq, dict) else "",
                "rfq_number": rfq_number,
                "status": "in_progress",
            },
            "opportunity": None,
            "tracked": True,
        }

        return JSONResponse({
            "status": "ok",
            "rfq_number": rfq_number,
            "message": f"Created {rfq_number} for {customer_name}",
            "context": updated_context,
        })

    except Exception as exc:
        session.rollback()
        logger.exception("Error creating RFQ from add-on")
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


# ---------------------------------------------------------------------------
# Auto-create tracking record when email hasn't been synced yet
# ---------------------------------------------------------------------------

def _ensure_email_tracking(
    session,
    gmail_message_id: str,
    gmail_thread_id: str,
    subject: str = "",
    sender: str = "",
    direction: str | None = None,
    sender_email: str | None = None,
    recipient_email: str | None = None,
    user_email: str | None = None,
) -> "EmailTracking":
    """Get or create an EmailTracking record for a Gmail message.

    Checks by gmail_message_id first (unique), then falls back to
    the latest message in the same thread. Creates a minimal record
    if neither is found, so link/create-rfq operations can proceed
    even before the Gmail sync has caught up.

    The sync's Tier 1 dedup check will skip this record when it
    eventually processes the same message_id — no duplicates.
    """
    from datetime import datetime, timezone
    from includes.dashboard.models import EmailTracking

    # Try exact message_id match first
    tracking = (
        session.query(EmailTracking)
        .filter(EmailTracking.gmail_message_id == gmail_message_id)
        .first()
    )
    if tracking:
        return tracking

    # Fall back to latest in same thread
    tracking = (
        session.query(EmailTracking)
        .filter(EmailTracking.gmail_thread_id == gmail_thread_id)
        .order_by(EmailTracking.id.desc())
        .first()
    )
    if tracking:
        return tracking

    # Resolve direction and fields
    # direction: 'received' | 'sent' (default to 'received' if unknown)
    dir_ = (direction or "received").lower()
    user_email = user_email or ""

    # Populate sender/recipient per sync conventions:
    #   received: sender_email = FROM (external), recipient_email = user (mailbox owner)
    #   sent:     sender_email = user (mailbox owner), recipient_email = TO (external)
    if dir_ == "sent":
        s_email = sender_email or user_email
        r_email = recipient_email or sender or ""
    else:
        s_email = sender_email or sender or ""
        r_email = recipient_email or user_email

    # Create record
    tracking = EmailTracking(
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        subject=subject or "",
        sender_email=s_email,
        recipient_email=r_email,
        user_email=user_email,
        direction=dir_,
        match_type="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add(tracking)
    session.flush()  # get ID without committing
    logger.info(
        "[addon] created tracking #%d for message %s (direction=%s, user=%s)",
        tracking.id, gmail_message_id, dir_, user_email,
    )
    return tracking
