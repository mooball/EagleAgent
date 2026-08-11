"""Create NetSuite Opportunity records and link them to local RFQs."""

import logging

from includes.dashboard.database import get_session
from includes.dashboard.models import Customer, Opportunity, RFQ
from includes.netsuite.client import NetSuiteClient
from .base import CreateResult

logger = logging.getLogger(__name__)


def create_opportunity(
    customer_netsuite_id: str,
    title: str,
    salesrep_netsuite_id: str | None = None,
    department_id: str | None = None,
) -> CreateResult:
    """Create an Opportunity in NetSuite and return the result.

    The minimum required fields are entity (customer) and title.
    NetSuite auto-generates the tranId (e.g. OP72309) and sets
    default values for status, probability, currency, etc.

    Args:
        customer_netsuite_id: NetSuite internal ID of the customer entity.
        title: Opportunity name/title.
        salesrep_netsuite_id: Optional NetSuite employee ID for sales rep.
        department_id: Optional NetSuite department ID.

    Returns:
        CreateResult with netsuite_id and tran_id on success.
    """
    client = NetSuiteClient()

    payload: dict = {
        "entity": {"id": str(customer_netsuite_id)},
        "title": title,
    }
    if salesrep_netsuite_id:
        payload["salesRep"] = {"id": str(salesrep_netsuite_id)}
    if department_id:
        payload["department"] = {"id": str(department_id)}

    try:
        netsuite_id = client.create_record("opportunity", payload)
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("Failed to create opportunity in NetSuite: %s", exc)
        return CreateResult(
            success=False,
            error=str(exc),
            error_code=status_code,
            record_type="opportunity",
        )

    # Fetch the created record to get the auto-generated tranId (e.g. OP72309)
    tran_id = None
    try:
        resp = client.get(f"record/v1/opportunity/{netsuite_id}")
        tran_id = resp.json().get("tranId")
    except Exception:
        logger.warning("Created opportunity %s but failed to fetch tranId", netsuite_id)

    return CreateResult(
        success=True,
        netsuite_id=netsuite_id,
        tran_id=tran_id,
        record_type="opportunity",
    )


def _resolve_salesrep(session, rfq: RFQ) -> str | None:
    """Resolve the NetSuite employee ID for an RFQ's assigned user.

    Looks up the netsuite_employee_mappings table by the RFQ's
    assigned_to email. Returns the netsuite_employee_id or None.
    """
    if not rfq.assigned_to:
        return None
    from sqlalchemy import text
    row = session.execute(
        text(
            "SELECT netsuite_employee_id FROM netsuite_employee_mappings "
            "WHERE email = :email AND is_active = true LIMIT 1"
        ),
        {"email": rfq.assigned_to.lower().strip()},
    ).fetchone()
    return row[0] if row else None


def create_and_link_opportunity(rfq_id: str) -> CreateResult:
    """Create a NetSuite opportunity for an RFQ and link them locally.

    Performs the full workflow:
      1. Validates the RFQ exists and doesn't already have an opportunity.
      2. Resolves the customer's NetSuite ID (required).
      3. Creates the opportunity in NetSuite.
      4. Creates a local Opportunity record and links it to the RFQ.

    This is idempotent — if the RFQ already has an opportunity_id,
    it returns an error without creating a duplicate.

    Args:
        rfq_id: The RFQ identifier (e.g. "RFQ-2026-0042").

    Returns:
        CreateResult with netsuite_id and tran_id on success.
    """
    session = get_session()
    try:
        rfq = session.query(RFQ).filter(RFQ.rfq_number == rfq_id).first()
        if not rfq:
            rfq = session.query(RFQ).get(rfq_id)
        if not rfq:
            return CreateResult(
                success=False,
                error=f"RFQ {rfq_id} not found",
                record_type="opportunity",
            )
        if rfq.opportunity_id:
            return CreateResult(
                success=False,
                error="RFQ already has a linked opportunity",
                record_type="opportunity",
            )

        customer = session.query(Customer).get(rfq.customer_id) if rfq.customer_id else None
        if not customer or not customer.netsuite_id:
            return CreateResult(
                success=False,
                error="Customer has no NetSuite ID — cannot create opportunity",
                record_type="opportunity",
            )

        title = rfq.title or rfq.rfq_number

        # Create in NetSuite
        result = create_opportunity(
            customer_netsuite_id=customer.netsuite_id,
            title=title,
            salesrep_netsuite_id=_resolve_salesrep(session, rfq),
        )
        if not result.success:
            return result

        # Create local Opportunity record and link to RFQ
        opp = Opportunity(
            netsuite_id=result.netsuite_id,
            opportunity_number=result.tran_id,
            title=title,
            status="In Negotiation",
            netsuite_customer_id=customer.netsuite_id,
            customer_id=customer.id,
        )
        session.add(opp)
        session.flush()

        rfq.opportunity_id = opp.id
        rfq.netsuite_opportunity = result.tran_id
        session.commit()

        logger.info(
            "Linked opportunity %s to RFQ %s (NS ID: %s)",
            result.tran_id, rfq.rfq_number, result.netsuite_id,
        )
        return result

    except Exception as exc:
        session.rollback()
        logger.exception("Failed to create/link opportunity for RFQ %s", rfq_id)
        return CreateResult(
            success=False,
            error=str(exc),
            record_type="opportunity",
        )
    finally:
        session.close()


def update_opportunity_title(netsuite_id: str, title: str) -> None:
    """Update the title of an existing NetSuite Opportunity.

    Called whenever an RFQ's title changes and the RFQ is linked
    to an opportunity. Runs in a background thread so the UI isn't blocked.

    Args:
        netsuite_id: NetSuite internal ID of the opportunity.
        title: New title to set.
    """
    try:
        client = NetSuiteClient()
        client.update_record("opportunity", netsuite_id, {"title": title})
        logger.info("Updated opportunity %s title to %r", netsuite_id, title)
    except Exception:
        logger.exception("Failed to update opportunity %s title", netsuite_id)
