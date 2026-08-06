"""Shared helpers for dashboard routes.

All route sub-modules import from here so there's a single router,
templates instance, auth guard, and render helper.
"""

import logging
import math
import os
import hashlib

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text

from includes.dashboard.database import get_session, update_supplier, add_supplier_comment
from config import config
from includes.dashboard.models import (
    Brand,
    Product,
    Transaction,
    Supplier,
    SupplierBrand,
    RFQThread,
)

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")

# Add format_currency filter for $1,234.56 display
def _format_currency(value):
    """Format a number as currency with $ and thousands separators."""
    if value is None:
        return "—"
    try:
        v = float(value)
        return f"${v:,.2f}"
    except (ValueError, TypeError):
        return str(value)

templates.env.filters["currency"] = _format_currency


# Cache-busting hash for static assets (computed once at startup)
def _css_hash() -> str:
    css_path = os.path.join("public", "tailwind.min.css")
    try:
        with open(css_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except FileNotFoundError:
        return "dev"

templates.env.globals["css_version"] = _css_hash()

# Currency symbol mapping for display
_CURRENCY_SYMBOLS = {
    "AUD": "$", "USD": "$", "NZD": "$", "CAD": "$",
    "GBP": "£", "EUR": "€", "JPY": "¥", "INR": "₹",
}


def _currency_symbol(code: str | None) -> str:
    """Return a display-friendly currency prefix/suffix for a given ISO code.

    Returns '$' for AUD/USD/NZD/CAD, proper symbols for GBP/EUR/JPY/INR,
    or the code itself (e.g. 'SGD') for others.
    """
    if not code:
        return "$"
    return _CURRENCY_SYMBOLS.get(code.upper(), code)


templates.env.globals["currency_symbol"] = _currency_symbol

router = APIRouter()

PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------
def require_user(request: Request) -> dict:
    """Ensure a logged-in user; redirect to /login otherwise."""
    user = request.session.get("user")
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    # Attach computed role so templates/downstream code can use it
    user["role"] = (
        "Admin"
        if user.get("email", "").lower() in config.get_admin_emails()
        else "Staff"
    )
    return user


def require_role(*allowed_roles: str) -> Depends:
    """Dependency factory: restrict a route to users with one of the given roles."""
    def _guard(request: Request) -> dict:
        user = require_user(request)
        if user["role"] not in allowed_roles:
            from fastapi import HTTPException
            if _is_htmx(request):
                raise HTTPException(status_code=403)
            raise HTTPException(status_code=403)
        return user
    return Depends(_guard)


# Convenience alias for the most common guard
require_admin = require_role("Admin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _render(request: Request, full_template: str, partial_template: str,
            context: dict, user: dict):
    """Return a partial if HTMX, else the full page."""
    context["user"] = user
    if _is_htmx(request):
        response = templates.TemplateResponse(request, partial_template, context)
    else:
        response = templates.TemplateResponse(request, full_template, context)
    # Prevent browser/edge caching — dashboard data must always be fresh.
    # Communications tab especially needs real-time email updates.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
