"""Which contact at a supplier to use, and why.

Most suppliers have one contact, but ~500 have several, and they are not
interchangeable:

* ``Source``     — the **Go Source** contact: the person who handles purchasing.
  NetSuite holds this one, and it is the address an RFQ should go to.
* ``Source CC``  — someone who was copied on that person's email.
* ``Main``       — the supplier's recorded mailbox (often a generic
                  ``info@``/``sales@``).
* (none)         — provenance rows written from inbound email.

The rest of the codebase already treats ``Source`` as the supplier's preferred
contact — ``routes/rfqs.py::_resolve_salutation_name`` says so in its docstring —
but the recipient picker did not: it took the *first* row with an email and the
*first* row with a name, two independent scans, so the address and the person
could come from different contacts. This module is that rule in one place.

**The table wins over the snapshot.** An RFQ line stores a contacts snapshot
written when the supplier was linked, and it goes stale. Over 1,357 line entries
with more than one contact, the snapshot's first email disagreed with the
contacts table's best contact 28% of the time, and the table was the useful
side — a branch, export or named contact (``joshuac@``, ``Brisbane.export@``,
``coopersplains@``) against a generic ``info@``/``sales@``, or in one case a
``customersecurity@`` mailbox. The snapshot is still the fallback, because a
web-discovered supplier may have no table rows at all.

**The data quality is poor in specific, known ways**, so every value is
normalised before it is used:

* the literal string ``"None"`` sitting in an email column,
* two addresses in one field (``"qld@exedy.com.au; GWilson@exedy.com.au"``),
* name and email in each other's columns on provenance rows (``SensaTek`` holds
  email ``"Daniel"`` and name ``"daniel@sensatek.com.au"``),
* trailing semicolons, and the same address on several rows.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Iterable, Optional

#: The NetSuite-managed "Go Source" contact — a supplier's purchasing person.
SOURCE = "Source"
#: Someone copied on the Go Source email rather than the person who sent it.
SOURCE_CC = "Source CC"
#: The mailbox the supplier is recorded under.
MAIN = "Main"

#: Best first, for a supplier. ``Source`` is the Go Source contact; ``Main`` is
#: the recorded mailbox; ``Source CC`` only beats an unlabelled row because it
#: at least came from the supplier's own correspondence.
SUPPLIER_LABELS = (SOURCE, MAIN, SOURCE_CC)

#: Customers are not managed the same way — their recorded contact is ``Main``.
CUSTOMER_LABELS = (MAIN, SOURCE)

#: Values that mean "nothing here", seen in real data.
_JUNK = {"", "-", "n/a", "na", "nan", "none", "null", "unknown", "undefined"}

#: Deliberately strict: a bare domain or a person's name must not pass, because
#: both appear in the email column of provenance rows.
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

#: One field can hold several addresses, split by comma or semicolon.
_SPLIT = re.compile(r"[,;]")


def normalise_email(value: Any) -> str:
    """The address in a field, or ``""``.

    Takes the first real address out of a field that holds several, because
    ``"qld@exedy.com.au; GWilson@exedy.com.au"`` is one row in the data and
    neither a valid recipient nor something a caller should have to split.

    The pattern is the whole gate: ``"None"``, ``"n/a"``, a person's name and a
    bare domain all fail it, which is why there is no separate junk list here.
    """
    text = str(value or "").strip()
    for part in _SPLIT.split(text):
        candidate = part.strip().strip("<>").strip()
        if candidate and _EMAIL.match(candidate):
            return candidate
    return ""


def _value(contact: Any, key: str) -> Any:
    """One field, from a snapshot dict or a ``Contact`` row."""
    if isinstance(contact, dict):
        return contact.get(key)
    return getattr(contact, key, None)


def _raw_name(contact: Any) -> str:
    """The name-ish column, whichever the source calls it.

    Snapshots use ``name``, ``Contact`` rows use ``fullname`` — and a provenance
    row may hold the address in it, which is why the value is needed even when it
    turns out not to be a name.
    """
    return str(_value(contact, "name") or _value(contact, "fullname") or "").strip()


def _display_name(contact: Any) -> str:
    """A person's name, or ``""`` when the column holds something else.

    Provenance rows sometimes carry the address in the name column (and the name
    in the email column), and several carry the literal ``"None"``. Returning
    that as a salutation is how "Hi None," happens.
    """
    text = _raw_name(contact)
    if text.lower() in _JUNK or "@" in text:
        return ""
    return text


def normalise_contact(contact: Any) -> dict[str, str]:
    """One shape for either source, so callers never branch on the type."""
    contact_id = _value(contact, "id")
    return {
        "id": str(contact_id) if contact_id else "",
        "label": str(_value(contact, "label") or "").strip(),
        "name": _display_name(contact),
        # A swapped column is still the address for this row (see the module
        # docstring) — taking it here is what makes those rows usable at all.
        # Both name columns are tried: a snapshot says ``name``, a Contact row
        # says ``fullname``, and the address can be in either.
        "email": normalise_email(_value(contact, "email"))
        or normalise_email(_raw_name(contact)),
        "phone": str(_value(contact, "phone") or "").strip(),
    }


def _label_rank(contact: dict[str, str], labels: tuple[str, ...]) -> int:
    try:
        return labels.index(contact["label"])
    except ValueError:
        return len(labels)


def _sort_key(contact: dict[str, str], labels: tuple[str, ...]):
    """Best first: the label's rank, then a named person ahead of a bare mailbox.

    Shared with :func:`merge_ordered` so combining two lists and ranking one list
    agree on what "better" means — the disagreement between them is what let a
    ``Main`` row shadow the ``Source`` row holding the same mailbox.
    """
    return (_label_rank(contact, labels), 0 if contact["name"] else 1,
            contact["name"].lower())


def rank_contacts(
    contacts: Optional[Iterable[Any]],
    *,
    labels: tuple[str, ...] = SUPPLIER_LABELS,
) -> list[dict[str, str]]:
    """Usable contacts, best first, deduplicated by address.

    Only contacts with an address are returned: a row with a name and no way to
    reach it is no use to a picker, and counting it as a choice would make the
    "more than one contact" question answer itself wrongly.

    Within a label, a named person sorts ahead of a bare mailbox — given two
    unlabelled addresses, ``joshuac@`` deserves to win over ``info@``. Deduping
    happens *after* sorting, so the row kept for an address is the best one that
    carries it, not the first one a query happened to return.
    """
    ranked: list[dict[str, str]] = []
    seen: set[str] = set()
    # Anything that is not a dict or a Contact row normalises to empty fields and
    # is dropped by the address filter, so junk in the list is harmless.
    candidates = [normalise_contact(c) for c in (contacts or []) if c is not None]
    for contact in sorted(
        (c for c in candidates if c["email"]),
        key=lambda c: _sort_key(c, labels),
    ):
        key = contact["email"].lower()
        if key in seen:
            continue
        seen.add(key)
        ranked.append(contact)
    return ranked


def best_contact(
    contacts: Optional[Iterable[Any]],
    *,
    preferred_id: str = "",
    labels: tuple[str, ...] = SUPPLIER_LABELS,
) -> Optional[dict[str, str]]:
    """The contact to use, or ``None`` when nothing is reachable.

    ``preferred_id`` is a choice someone already made for this supplier on this
    RFQ — it wins over the labels, because a decision beats a default. An id
    that no longer exists (the contact was deleted or merged) falls through to
    the labels rather than being an error: the RFQ still has to be emailed.
    """
    ranked = rank_contacts(contacts, labels=labels)
    if preferred_id:
        wanted = str(preferred_id)
        for contact in ranked:
            if contact["id"] and contact["id"] == wanted:
                return contact
    return ranked[0] if ranked else None


def best_from(
    db_contacts: Optional[Iterable[Any]] = None,
    snapshot: Optional[Iterable[Any]] = None,
    *,
    preferred_id: str = "",
    labels: tuple[str, ...] = SUPPLIER_LABELS,
) -> Optional[dict[str, str]]:
    """The contacts table wins; the stored snapshot is the fallback.

    Not a merge: the point is to stop a stale row from winning. The snapshot is
    only read when the table has nothing reachable, which is the case for a
    supplier whose contacts live solely in ``suppliers.contacts`` (a
    web-discovered row, or one not yet promoted to the contacts table).
    """
    for source in (db_contacts, snapshot):
        contact = best_contact(source, preferred_id=preferred_id, labels=labels)
        if contact:
            return contact
    return None


def first_phone(contacts: Optional[Iterable[Any]]) -> str:
    """The first usable phone number in a list, address or not.

    Separate from the resolver because the two questions differ: an addressless
    contact cannot be emailed (so it is not a recipient), but its phone number is
    still the supplier's phone number, and the NetSuite vendor record wants it.
    """
    for contact in (contacts or []):
        if not isinstance(contact, dict):
            continue
        phone = str(contact.get("phone") or "").strip()
        if phone and phone.lower() not in _JUNK:
            return phone
    return ""


def salutation_name(contact: Optional[dict[str, str]]) -> Optional[str]:
    """The first name to open an email with, or ``None``."""
    if not contact:
        return None
    full = (contact.get("name") or "").strip()
    if not full:
        return None
    first = full.split()[0]
    return first if len(first) >= 2 else None


def merge_ordered(
    primary: Optional[Iterable[Any]],
    secondary: Optional[Iterable[Any]] = None,
) -> list[Any]:
    """``primary`` rows first, then whatever ``secondary`` adds.

    Order is the point. Both a stored snapshot and the contacts table can carry a
    ``Source`` row — 1,060 snapshot rows are labelled that way — so ranking alone
    leaves a tie, and a stable sort would hand it to whichever list came first.
    Putting the table's rows in front resolves the tie in favour of the live
    record.

    Rows are deduplicated by address (then phone) because the two lists describe
    the same supplier, and the row **kept** is the better one: a ``Main`` copy of
    a mailbox must not shadow the ``Source`` row that holds the same address, or
    the label — and the person's name with it — is lost. A genuine tie keeps the
    first seen, which is the primary list's row.
    """
    best: dict[str, Any] = {}
    order: list[str] = []
    for group in (primary, secondary):
        for contact in (group or []):
            if not isinstance(contact, dict):
                continue
            email = normalise_email(contact.get("email")).lower()
            phone = str(contact.get("phone") or "").strip()
            if not email and not phone:
                # No way to tell whether it duplicates anything; keep it, keyed
                # by position so it can never collide with another row's key.
                order.append(f"#{len(order)}")
                best[order[-1]] = contact
                continue
            key = email or phone
            if key not in best:
                best[key] = contact
                order.append(key)
                continue
            if _sort_key(normalise_contact(contact), SUPPLIER_LABELS) < _sort_key(
                normalise_contact(best[key]), SUPPLIER_LABELS
            ):
                best[key] = contact
    return [best[key] for key in order]


def best_contacts_for(
    session: Any,
    supplier_ids: Iterable[Any],
    *,
    snapshots: Optional[dict[str, Iterable[Any]]] = None,
    preferences: Optional[dict[str, str]] = None,
) -> dict[str, dict[str, str]]:
    """One contact per supplier, with a single query for the whole page.

    The contacts table is read here; ``snapshots`` supplies each supplier's
    fallback list (``suppliers.contacts`` JSONB, or an RFQ line's stored
    snapshot) and ``preferences`` any choice already made. Callers that only have
    an in-memory list should use :func:`best_from` directly instead.
    """
    from includes.dashboard.models import Contact

    wanted: list[str] = []
    for supplier_id in supplier_ids or []:
        text = str(supplier_id or "").strip()
        if not text:
            continue
        try:
            uuid.UUID(text)
        except (ValueError, AttributeError, TypeError):
            continue        # legacy ids like "sup_1597" — not a contacts key
        if text not in wanted:
            wanted.append(text)
    if not wanted:
        return {}

    rows = (
        session.query(Contact)
        .filter(Contact.supplier_id.in_(wanted), Contact.isinactive == False)  # noqa: E712
        .all()
    )
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row.supplier_id), []).append(row)

    resolved: dict[str, dict[str, str]] = {}
    for supplier_id in wanted:
        chosen = best_from(
            grouped.get(supplier_id),
            (snapshots or {}).get(supplier_id),
            preferred_id=(preferences or {}).get(supplier_id, ""),
        )
        if chosen:
            resolved[supplier_id] = chosen
    return resolved
