"""Supplier normalisation and match-key maintenance.

Shared foundation for supplier deduplication (Track A) and supplier lookup
filtering (Track B). Python normalises supplier data at write time; the
results are stored as rows in `supplier_match_keys`, so all matching runs as
pure SQL against indexed columns.

Nothing in this module mutates supplier data except `rebuild_match_keys()`.
"""

import logging
import re
import unicodedata

from includes.dashboard.database import GENERIC_SLD_LABELS, _extract_domain
from includes.dashboard.models import SupplierMatchKey

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------

NOISE_TOKENS = {
    "and", "the", "of", "for",
    "pty", "ltd", "limited", "co", "company", "inc", "incorporated",
    "corp", "corporation", "llc", "plc", "pl", "gmbh", "bv", "nv", "srl",
    "group", "holdings", "international", "intl",
    "australia", "australian", "aust", "au",
    # currency annotations found in vendor names, e.g. "(AUD)", "(usd)"
    # ('euro' is deliberately NOT here — see _CURRENCY_SUFFIX_RE)
    "aud", "usd", "eur", "nzd", "gbp",
}

_PUNCT_RE = re.compile(r"[^a-z0-9]+")

# Trailing parenthesised currency annotations: "... (euro)", "... (AUD)".
# The word 'euro' is NOT a global noise token — it's also a brand word
# ("Euro Signs and Safety", "Euro Glass") — so it is only stripped when it
# appears as a trailing currency annotation.
_CURRENCY_SUFFIX_RE = re.compile(
    r"\s*\((?:aud|usd|eur|euro|nzd|gbp)\)\s*$", re.IGNORECASE
)


def _join_single_letters(tokens: list[str]) -> list[str]:
    """Join runs of single-letter tokens: 'a c m' -> 'acm'.

    Dotted initials ('A.C.M.') normalise to spaced single letters; joining
    them makes 'ACM' and 'A.C.M.' share a key. Only pure a-z singles join —
    alphanumerics like '3m' are untouched.
    """
    joined: list[str] = []
    buf: list[str] = []
    for t in tokens:
        if len(t) == 1 and t.isalpha():
            buf.append(t)
        else:
            if buf:
                joined.append("".join(buf))
                buf = []
            joined.append(t)
    if buf:
        joined.append("".join(buf))
    return joined


def normalize_supplier_name(name: str | None) -> str:
    """'A.C.M. Laboratory Pty. Ltd.' -> 'acm laboratory'.

    NFKD-folds accents and smart punctuation, lowercases, replaces punctuation
    with spaces, joins runs of single letters, and drops NOISE_TOKENS. Word
    order is preserved so trigrams still see ordering. A name made entirely of
    noise tokens keeps its tokens rather than normalising to the empty string
    (an empty key would false-match every other empty key).
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = _CURRENCY_SUFFIX_RE.sub(" ", s.lower())
    s = _PUNCT_RE.sub(" ", s)
    tokens = [t for t in s.split() if t not in NOISE_TOKENS]
    if not tokens:  # all-noise name (e.g. "The Company") — keep as-is
        tokens = s.split()
    return " ".join(_join_single_letters(tokens))


# Currency annotations are stripped from name keys so "Envirostraw (USD)"
# matches "EnviroStraw Pty Ltd" — but two records carrying *different*
# currencies are deliberately separate NetSuite vendors, never duplicates.
# 'euro' and 'cad' are excluded: both are common words in real trading names.
_CURRENCY_CODES = {
    "aud", "usd", "eur", "nzd", "gbp", "sgd", "jpy", "chf", "hkd", "myr",
}
_CURRENCY_TOKEN_RE = re.compile(
    r"\b(" + "|".join(sorted(_CURRENCY_CODES)) + r")\b", re.IGNORECASE
)


def currency_tokens(name: str | None) -> set[str]:
    """Currency codes appearing as whole words in a supplier name."""
    if not name:
        return set()
    return {m.lower() for m in _CURRENCY_TOKEN_RE.findall(name)}


# ---------------------------------------------------------------------------
# Domain keys (free-mail excluded, TLD stripped)
# ---------------------------------------------------------------------------

FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com",
    "msn.com", "yahoo.com", "yahoo.com.au", "ymail.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "bigpond.com",
    "bigpond.net.au", "optusnet.com.au", "tpg.com.au", "iinet.net.au",
}


def domain_key(value: str | None) -> str | None:
    """'https://www.abcparts.com.au/x' -> 'abcparts'. None for free-mail.

    Uses the ccTLD-aware _extract_domain() to find the registrable domain,
    then strips the TLD(s) so abc.com and abc.com.au share a key. The result
    is a wider candidate signal — always corroborated by name similarity
    before a pair is accepted.
    """
    if not value:
        return None
    raw = value if "//" in value else f"http://{value}"
    d = _extract_domain(raw)
    if not d or d in FREEMAIL_DOMAINS:
        return None
    stem = d.split(".")[0]
    # A public-suffix label as the stem means the domain was mis-split;
    # keys like 'com' or 'gov' would false-match hundreds of suppliers.
    if not stem or stem in GENERIC_SLD_LABELS:
        return None
    return stem


# ---------------------------------------------------------------------------
# Match-key maintenance
# ---------------------------------------------------------------------------

def supplier_match_keys(sup) -> list[tuple[str, str]]:
    """All searchable (key_type, key_value) pairs for a supplier.

    - 'name':   normalised name + each normalised alt_name
    - 'domain': domain keys from url, alt_domains, contact URLs and
                contact email domains (free-mail excluded)
    """
    keys: set[tuple[str, str]] = set()

    for n in [sup.name, *(sup.alt_names or [])]:
        if n and n.startswith("__"):  # legacy __dedup_reviewed__ marker
            continue
        k = normalize_supplier_name(n or "")
        if k:
            keys.add(("name", k))

    for u in [sup.url, *(sup.alt_domains or [])]:
        if k := domain_key(u):
            keys.add(("domain", k))

    for c in (sup.contacts or []):
        if not isinstance(c, dict):
            continue
        if k := domain_key(c.get("url")):
            keys.add(("domain", k))
        email = c.get("email") or ""
        if "@" in email and (k := domain_key(email.rsplit("@", 1)[-1])):
            keys.add(("domain", k))

    return sorted(keys)


def rebuild_match_keys(session, sup) -> None:
    """Delete and re-create all match keys for a supplier. Idempotent."""
    session.query(SupplierMatchKey).filter(
        SupplierMatchKey.supplier_id == sup.id
    ).delete()
    for key_type, key_value in supplier_match_keys(sup):
        session.add(SupplierMatchKey(
            supplier_id=sup.id, key_type=key_type, key_value=key_value,
        ))
    logger.debug(f"Rebuilt {len(supplier_match_keys(sup))} match keys for {sup.name!r}")
