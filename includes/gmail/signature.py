"""Outbound email signature.

Builds the shared, branded signature block appended to every outbound email
(RFQ outreach, quotes, invoices). The visual pattern follows the client's
signature: the logo sits in a left column, with the sender's name, company and
contact details stacked to its right.

Sender-specific lines (name, email) are supplied per send; the rest of the block
is static branding configured in :mod:`config.settings`. Generating this
server-side means the signature is never part of the editable body in the
compose editor, so staff can't accidentally edit or delete it.
"""

from __future__ import annotations

import html
from urllib.parse import quote

from config import config


def _escape(value: str | None) -> str:
    return html.escape(value or "", quote=True)


def _maps_url(address: str) -> str:
    """Google Maps search URL for a postal address."""
    return f"https://www.google.com/maps/search/?api=1&query={quote(address)}"


def logo_url() -> str:
    """Absolute URL of the signature logo (email clients require an absolute URL)."""
    return f"{config.PUBLIC_BASE_URL}{config.EMAIL_SIGNATURE_LOGO}"


def build_email_signature(sender_name: str | None, sender_email: str | None) -> str:
    """Return the HTML signature block, including the ``Regards,`` salutation.

    ``sender_name`` falls back to ``sender_email`` when blank, matching the
    previous template behaviour.
    """
    name = _escape(sender_name or sender_email or "")
    email = _escape(sender_email or "")
    company = _escape(config.EMAIL_SIGNATURE_COMPANY)
    service_line = _escape(config.EMAIL_SIGNATURE_SERVICE_LINE)
    tagline = _escape(config.EMAIL_SIGNATURE_TAGLINE)
    phone = _escape(config.EMAIL_SIGNATURE_PHONE)
    website = _escape(config.EMAIL_SIGNATURE_WEBSITE)
    website_url = _escape(config.EMAIL_SIGNATURE_WEBSITE_URL)
    address = _escape(config.EMAIL_SIGNATURE_ADDRESS)
    maps_url = _escape(_maps_url(config.EMAIL_SIGNATURE_ADDRESS))
    green = config.EMAIL_SIGNATURE_GREEN
    blue = config.EMAIL_SIGNATURE_BLUE

    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'line-height:1.45;color:#111827;">'
        '<p style="margin:0 0 12px 0;">Regards,</p>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;'
        'font-size:14px;color:#111827;">'
        '<tr>'
        # Logo — left column
        '<td valign="top" style="padding:4px 20px 0 0;">'
        f'<img src="{logo_url()}" alt="{company}" width="160" '
        'style="display:block;border:0;outline:none;width:160px;'
        'max-width:160px;height:auto;">'
        '</td>'
        # Details — right column
        '<td valign="top" style="padding:0;">'
        f'<div style="font-size:16px;font-weight:bold;color:{blue};">{name}</div>'
        f'<div style="color:#6b7280;">{company}</div>'
        '<div style="margin:10px 0 0 0;padding-top:10px;'
        'border-top:1px solid #d1d5db;">'
        f'<span style="font-weight:bold;color:{green};">{service_line}</span><br>'
        f'<span style="font-style:italic;color:#6b7280;">{tagline}</span>'
        '</div>'
        f'<div style="margin-top:8px;font-size:12px;">P: {phone} &nbsp;|&nbsp; '
        f'<a href="{website_url}" style="color:{blue};text-decoration:none;">'
        f'{website}</a></div>'
        f'<div style="font-size:12px;">'
        f'<a href="{maps_url}" style="color:#111827;text-decoration:none;">'
        f'{address}</a></div>'
        f'<div style="font-size:12px;">E: <a href="mailto:{email}" style="color:{blue};'
        f'text-decoration:none;">{email}</a></div>'
        '</td>'
        '</tr>'
        '</table>'
        '</div>'
    )


def build_email_signature_text(sender_name: str | None, sender_email: str | None) -> str:
    """Plain-text equivalent of :func:`build_email_signature`.

    Used whenever a caller supplies an explicit ``body_plain`` — the HTML form
    carries the logo and links, which have no place in the text alternative.
    """
    name = sender_name or sender_email or ""
    return "\n".join(
        [
            "",
            "Regards,",
            "",
            name,
            config.EMAIL_SIGNATURE_COMPANY,
            config.EMAIL_SIGNATURE_SERVICE_LINE,
            config.EMAIL_SIGNATURE_TAGLINE,
            f"P: {config.EMAIL_SIGNATURE_PHONE} | {config.EMAIL_SIGNATURE_WEBSITE}",
            config.EMAIL_SIGNATURE_ADDRESS,
            f"E: {sender_email or ''}",
        ]
    )
