# Plan: Create NetSuite Opportunities from EagleAgent

## Overview

Enable creating NetSuite Opportunities from EagleAgent, linked to RFQs. This is the first write-back feature to NetSuite (all current integrations are read-only).

**Key Constraint**: The Customer must already exist in NetSuite — we pass an existing customer `netsuite_id`. No customer creation in v1.

**Safe test customer**: "Test company 2" — NetSuite ID `5780424`, local ID `4ddfca33-ee42-4341-9bc6-127b8a0f8f07`. Use this for all testing since we're working against the production NetSuite environment.

---

## Phase 1: API Exploration & Client Extension ✅ CONFIRMED

### 1.1 Endpoint confirmed working
- **Endpoint**: `POST /record/v1/opportunity` (note: `record` not `records`)
- Returns **204 No Content** with `Location` header containing the new record URL
- Extract NetSuite ID from Location: `.../opportunity/{id}`

### 1.2 Extend NetSuiteClient
- Add `create_record(record_type, data)` method to `includes/netsuite/client.py`
- Returns the created record's NetSuite internal ID (parsed from Location header)
- Handle auth, error responses, validation errors from NetSuite

### 1.3 Confirmed field behaviour (tested 2026-08-04)
**Minimum payload** (only these two are required):
- `entity`: `{"id": "<customer_netsuite_id>"}` — **required**
- `title`: string — opportunity name

**Auto-populated by NetSuite**:
- `tranId`: auto-generated (e.g. "OP72309") ✅
- `entityStatus`: defaults to "In Negotiation" (id: 11)
- `salesRep`: inherited from customer record (e.g. Bill Watt for Test company 2)
- `probability`: 75.0 (default)
- `subsidiary`: "Parent Company" (id: 1)
- `currency`: AUD (id: 1) — inherited from customer
- `expectedCloseDate`, `total`: empty/zero

**Optional fields we may want to set**:
- `salesRep`: `{"id": "<employee_netsuite_id>"}` — override if different from customer default
- `entityStatus`: `{"id": "<status_id>"}` — override default status
- `department`: `{"id": "<dept_id>"}` — if needed for reporting

### Test record created
- **OP72309** (NS ID: 1537834) — linked to "Test company 2", safe to delete

---

## Architecture: NetSuite Record Creation Framework

This is the first write-back to NetSuite. The architecture below is designed to be reused for Customers, Contacts, and any future record types.

### File Layout

```
includes/netsuite/
├── __init__.py
├── auth.py                  # OAuth (existing)
├── client.py                # HTTP client — add create_record() here
├── constants.py             # Status maps (existing)
├── queries.py               # SuiteQL query strings (existing)
├── sync_utils.py            # Sync helpers (existing)
└── records/                 # NEW — record creation logic
    ├── __init__.py
    ├── base.py              # CreateResult dataclass, shared helpers
    ├── opportunity.py       # create_opportunity()
    └── customer.py          # Future: create_customer()
```

**Why `includes/netsuite/records/`?**
- Keeps creation logic separate from read-only sync code
- One file per record type — easy to find, easy to test
- Shared patterns live in `base.py` (result type, validation, error mapping)
- The `scripts/sync_netsuite_*.py` files remain read-only pull scripts

### Result Type

Every creation function returns the same structured result, making it easy for callers (UI, agent, pipeline) to handle uniformly:

```python
# includes/netsuite/records/base.py
from dataclasses import dataclass


@dataclass
class CreateResult:
    """Outcome of a NetSuite record creation attempt."""
    success: bool
    netsuite_id: str | None = None
    tran_id: str | None = None        # e.g. "OP72309" — fetched after creation
    error: str | None = None
    error_code: int | None = None      # HTTP status code on failure
    record_type: str = ""


class NetSuiteCreateError(Exception):
    """Raised when NetSuite rejects a record creation request."""
    def __init__(self, message: str, status_code: int, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
```

### Client Extension

Add a generic `create_record()` to the existing client:

```python
# In includes/netsuite/client.py — new method on NetSuiteClient

def create_record(self, record_type: str, data: dict) -> str:
    """Create a record in NetSuite via REST API.

    Args:
        record_type: e.g. "opportunity", "customer", "contact"
        data: JSON body per NetSuite REST API schema

    Returns:
        The NetSuite internal ID of the created record.

    Raises:
        requests.HTTPError: on 4xx/5xx responses
    """
    url = f"{self._base_url}/record/v1/{record_type}"
    headers = self._headers()
    headers["Content-Type"] = "application/json"

    response = self._session.post(url, headers=headers, json=data, timeout=_DEFAULT_TIMEOUT)
    if not response.ok:
        logger.error(
            "CREATE %s → %s: %s", record_type, response.status_code, response.text[:500]
        )
        response.raise_for_status()

    # 204 No Content — ID is in the Location header
    location = response.headers.get("Location", "")
    netsuite_id = location.rstrip("/").split("/")[-1]
    logger.info("Created %s/%s", record_type, netsuite_id)
    return netsuite_id
```

### Error Handling Strategy

| Error Type | HTTP Code | Action | Retry? |
|-----------|-----------|--------|--------|
| Validation error (missing field, bad value) | 400/422 | Return `CreateResult(success=False, error=..., error_code=...)` | No — fix payload |
| Permission denied | 403 | Return error, log loudly | No — fix permissions |
| Record not found (bad entity ref) | 404 | Return error | No — caller has bad ID |
| Rate limit | 429 | Handled by existing retry adapter (backoff) | Yes — automatic |
| Server error | 500/502/503 | Retry adapter handles it (3 retries with backoff) | Yes — automatic |
| Network timeout | - | Retry adapter handles it | Yes — automatic |

**Key principles:**
1. **Never silently swallow errors** — always return a `CreateResult` so the caller knows what happened.
2. **Don't retry validation errors** — a 400 means the payload is wrong, retrying won't help.
3. **Rely on the existing retry adapter** for transient failures (already configured with 3 retries, exponential backoff, on 502/503/504).
4. **Log everything** — record type, payload summary, response code, error body.

### Idempotency

To prevent duplicate records on retry:

```python
# Before creating in NetSuite, check if we already have a linked record
def _already_linked(session, rfq_id: str) -> bool:
    """Check if this RFQ already has a linked opportunity."""
    rfq = session.query(RFQ).get(rfq_id)
    return rfq is not None and rfq.opportunity_id is not None
```

For the pipeline (auto-create on RFQ creation):
- Check `rfq.opportunity_id IS NOT NULL` before creating
- If the pipeline is re-run, it skips RFQs that already have opportunities
- If NetSuite creation succeeds but local DB write fails → orphan in NetSuite. The next sync will pull it back and we can detect/link it by title match.

---

## Phase 2: Core Creation Logic

### 2.1 Opportunity creation module

```python
# includes/netsuite/records/opportunity.py
import logging

from includes.netsuite.client import NetSuiteClient
from .base import CreateResult

logger = logging.getLogger(__name__)


def create_opportunity(
    customer_netsuite_id: str,
    title: str,
    salesrep_netsuite_id: str | None = None,
    department_id: str | None = None,
) -> CreateResult:
    """Create an Opportunity in NetSuite and return the result."""
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
        return CreateResult(
            success=False,
            error=str(exc),
            error_code=status_code,
            record_type="opportunity",
        )

    # Fetch the created record to get the auto-generated tranId
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
```

### 2.2 Create + link local Opportunity record

```python
# includes/netsuite/records/opportunity.py (continued)
from includes.dashboard.models import Opportunity, RFQ, Customer
from includes.dashboard.database import get_session


def create_and_link_opportunity(rfq_id: str) -> CreateResult:
    """Create a NetSuite opportunity for an RFQ and link them locally."""
    session = get_session()
    try:
        rfq = session.query(RFQ).get(rfq_id)
        if not rfq:
            return CreateResult(success=False, error=f"RFQ {rfq_id} not found", record_type="opportunity")
        if rfq.opportunity_id:
            return CreateResult(success=False, error="RFQ already has an opportunity", record_type="opportunity")

        customer = session.query(Customer).get(rfq.customer_id) if rfq.customer_id else None
        if not customer or not customer.netsuite_id:
            return CreateResult(
                success=False, error="Customer has no NetSuite ID", record_type="opportunity"
            )

        # Create in NetSuite
        result = create_opportunity(
            customer_netsuite_id=customer.netsuite_id,
            title=rfq.title or f"RFQ {rfq.rfq_number}",
            salesrep_netsuite_id=_resolve_salesrep(session, rfq),
        )
        if not result.success:
            return result

        # Create local Opportunity record and link to RFQ
        opp = Opportunity(
            netsuite_id=result.netsuite_id,
            opportunity_number=result.tran_id,
            title=rfq.title or f"RFQ {rfq.rfq_number}",
            status="In Negotiation",
            netsuite_customer_id=customer.netsuite_id,
            customer_id=customer.id,
        )
        session.add(opp)
        session.flush()

        rfq.opportunity_id = opp.id
        rfq.netsuite_opportunity = result.tran_id
        session.commit()

        return result
    except Exception as exc:
        session.rollback()
        logger.exception("Failed to create/link opportunity for RFQ %s", rfq_id)
        return CreateResult(success=False, error=str(exc), record_type="opportunity")
    finally:
        session.close()
```

### 2.3 Field mapping (RFQ → Opportunity)
| RFQ field | Opportunity field | NetSuite field |
|-----------|------------------|----------------|
| customer_id → Customer.netsuite_id | entity | entity |
| title | title | title |
| assigned_to → NetSuiteEmployeeMapping.netsuite_employee_id | salesrep | salesRep |
| (auto) | entityStatus | entityStatus |
| (from system_settings) | department | department |

---

## Phase 3: UI Integration

### 3.1 RFQ detail page — "Create Opportunity" button
- Visible when RFQ has no linked opportunity (`opportunity_id IS NULL`)
- Pre-fills: customer (from RFQ), title (from RFQ), owner/salesrep (from assigned_to)
- HTMX POST to `/rfqs/{id}/create-opportunity`
- On success: swaps in the opportunity link, shows success toast
- On failure: shows error message from `CreateResult.error`

```python
# In includes/dashboard/routes/rfqs.py
@router.post("/rfqs/{rfq_id}/create-opportunity")
async def create_opportunity_for_rfq(rfq_id: str, session=Depends(get_db)):
    result = create_and_link_opportunity(rfq_id)
    if result.success:
        return templates.TemplateResponse("partials/_rfq_opportunity_link.html", {
            "request": request, "tran_id": result.tran_id, "netsuite_id": result.netsuite_id,
        })
    raise HTTPException(status_code=400, detail=result.error)
```

### 3.2 RFQ detail page — "Link Existing Opportunity"
- Search/select from local opportunities table
- Already partially supported via `netsuite_opportunity` field in edit form

---

## Phase 4: Agent Integration

### 4.1 Auto-create opportunity during RFQ creation pipeline
In `includes/tools/rfq_creation_pipeline.py`, after RFQ is created:
1. Check if customer has a `netsuite_id` (required for NS opportunity)
2. If yes: call `create_and_link_opportunity(rfq_id)`
3. If no: skip (opportunity can be created later manually)
4. Pipeline continues regardless — opportunity creation failure is non-fatal

### 4.2 manage_rfq tool — "create_opportunity" action
```python
# In includes/tools/rfq_crud.py
elif action == "create_opportunity":
    from includes.netsuite.records.opportunity import create_and_link_opportunity
    result = create_and_link_opportunity(rfq_id)
    if result.success:
        return f"Created opportunity {result.tran_id} in NetSuite and linked to RFQ"
    return f"Failed to create opportunity: {result.error}"
```

---

## Risks & Open Questions

1. ~~**Does `POST /record/v1/opportunity` work?**~~ ✅ Yes, confirmed working.
2. ~~**Is `tranid` (OP number) auto-generated?**~~ ✅ Yes, auto-generated (e.g. OP72309).
3. ~~**Required fields**~~ ✅ Only `entity` + `title` required. Everything else has defaults.
4. **Error handling** — Defined above: fail immediately for validation, retry automatically for transient. Non-fatal in pipeline.
5. ~~**Permissions** — Does the service account/token have write access?~~ ✅ Yes.
6. **Department assignment** — Optional. Can set later from system_settings if needed.
7. **Idempotency** — Check `rfq.opportunity_id` before creating. Orphan recovery via next sync cycle.

---

## Next Steps

1. Add `create_record()` method to `includes/netsuite/client.py`
2. Create `includes/netsuite/records/` package with `base.py` and `opportunity.py`
3. Wire up `create_and_link_opportunity()` to the RFQ detail page
4. Add `create_opportunity` action to `manage_rfq` tool
5. Test end-to-end with "Test company 2" (NS ID 5780424)
