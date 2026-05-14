"""Dashboard routes package.

Exposes ``router`` — a single FastAPI ``APIRouter`` with all dashboard
routes registered by the sub-modules.

Importing this package is a drop-in replacement for the old monolithic
``routes.py``::

    from includes.dashboard.routes import router as dashboard_router
"""

from sqlalchemy import func

from includes.dashboard.models import Supplier, Product, Transaction
from fastapi import Request, Depends

# Re-export the shared router so callers can do:
#   from includes.dashboard.routes import router
from . import _helpers
from ._helpers import router, templates, require_user, config  # noqa: F401

# Import sub-modules so their @router decorators register on the shared router.
# Order determines route matching priority (first match wins for overlapping paths).
from . import api          # noqa: F401  — /api/latest-thread, /api/rfq-thread
from . import suppliers    # noqa: F401  — /suppliers, /partial/suppliers
from . import products     # noqa: F401  — /products, /partial/products
from . import transactions # noqa: F401  — /transactions, /partial/transactions
from . import rfqs         # noqa: F401  — /rfqs, /partial/rfqs
from . import admin        # noqa: F401  — /users, /admin, /partial/admin

# Also re-export helpers that tests patch via "includes.dashboard.routes.X"
from ._helpers import _is_htmx, _render, require_role, require_admin, PAGE_SIZE  # noqa: F401
from .rfqs import _get_store, _normalize_rfq_suppliers, _enrich_rfq_supplier_contacts  # noqa: F401
from .api import _lookup_rfq_thread_id  # noqa: F401
from .admin import _humanize_timestamp  # noqa: F401


# ---------------------------------------------------------------------------
# Dashboard home (small enough to live here)
# ---------------------------------------------------------------------------
@router.get("/")
async def dashboard_home(request: Request, user: dict = Depends(require_user)):
    session = _helpers.get_session()
    try:
        stats = {
            "suppliers": session.query(func.count(Supplier.id)).scalar(),
            "products": session.query(func.count(Product.id)).scalar(),
            "purchases": session.query(func.count(Transaction.id)).scalar(),
        }
    finally:
        session.close()

    # RFQ count from SQL
    from includes.dashboard.models import RFQ as RFQModel
    rfq_session = _helpers.get_session()
    try:
        stats["rfqs"] = rfq_session.query(func.count(RFQModel.id)).scalar()
    finally:
        rfq_session.close()

    return templates.TemplateResponse(request, "home.html", {
        "user": user,
        "stats": stats,
        "active_nav": "home",
    })
