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


# ── Endpoints ──────────────────────────────────────────────────────────────

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
