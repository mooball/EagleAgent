"""
generate_supplier_notes.py

Background job that researches suppliers via search-grounded Gemini and generates
structured notes optimized for vector-search retrieval.

Notes are designed so that embedding them enables semantic matching of the form:
  "find me a supplier who can supply [product/part/brand]"

Also captures alternative company names and domain variants for improved
supplier deduplication.

By default, only processes suppliers with no existing notes.
Use --force to regenerate notes for all suppliers.
Use --limit N to cap the number of suppliers processed per run.

Usage:
  uv run python -m scripts.generate_supplier_notes
  uv run python -m scripts.generate_supplier_notes --limit 50
  uv run python -m scripts.generate_supplier_notes --force --limit 100
  uv run python -m scripts.generate_supplier_notes --dry-run
"""

import argparse
import json
import socket
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types
from sqlalchemy import func

from config import config as app_config
from includes.dashboard.database import get_session
from includes.dashboard.models import Supplier, Transaction


def check_domain_alive(url: str | None) -> str:
    """Check if a supplier's domain resolves and returns an HTTP response.

    Returns a short status string to include in the prompt context.
    """
    if not url:
        return "NO_URL"

    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        domain = parsed.hostname
        if not domain:
            return "NO_URL"

        # DNS resolution check
        socket.setdefaulttimeout(5)
        socket.getaddrinfo(domain, 80)
    except (socket.gaierror, socket.timeout, OSError):
        return f"DOMAIN_DEAD: {domain} does not resolve (DNS lookup failed)"

    # HTTP check
    try:
        resp = httpx.head(
            f"http://{domain}",
            follow_redirects=True,
            timeout=10,
        )
        if resp.status_code < 400:
            return f"DOMAIN_ALIVE: {domain} responds (HTTP {resp.status_code})"
        else:
            return f"DOMAIN_ERROR: {domain} returns HTTP {resp.status_code}"
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError):
        return f"DOMAIN_DEAD: {domain} does not respond (connection failed)"


NOTES_PROMPT_TEMPLATE = """You are a procurement research assistant. Your task is to IDENTIFY and then research a supplier.

IMPORTANT: Accuracy is paramount. It is far better to report "Supplier unidentified" than to produce notes about the wrong company.

## Supplier to Research

- **Name:** {name}
- **Website:** {url}
- **Email domain(s):** {email_domains}
- **Location:** {city}, {country}
- **Purchase history with us:** {purchase_count} transactions
- **Domain check result:** {domain_status}
- **Current date:** {current_date}

IMPORTANT: You MUST base your research on what you can find via web search RIGHT NOW.
Do NOT rely on your training data or general knowledge about this company.
If your web search returns no results confirming this business currently exists, it does NOT exist.
Old cached content from web archives does NOT count as evidence of a current business.

## Step 1: IDENTIFY the business (do this FIRST)

You must confirm that this is a real, CURRENTLY-OPERATING business before writing any notes.
Use ALL available signals to verify identity:

1. **Check the website** (if provided): Does the domain resolve RIGHT NOW to an active website? Not a parked page, not a "coming soon", not a domain for sale. NOTE: We have already checked the domain for you — see "Domain check result" above. If it says DOMAIN_DEAD, the website is CONFIRMED to be offline right now.
2. **Search for CURRENT evidence**: Search for the company name combined with the city/country. Look for:
   - A LIVE company website with recent content (check copyright dates, news, blog posts)
   - A Google Business Profile showing as OPEN (not "Permanently closed")
   - A LinkedIn company page with current employees and recent activity
   - Recent customer reviews (within the last 2 years)
   - An active business registration (ABN/ACN for Australian companies)
3. **Distinguish current from historical**: Web archives, old directory listings, expired job ads, and cached pages from years ago do NOT count as evidence the business currently exists.
4. **Red flags that the business has CLOSED**:
   - Domain is unregistered, expired, parked, or does not load
   - LinkedIn page exists but is unclaimed, or shows no current employees
   - No recent activity, reviews, or mentions in the last 2+ years
   - Google Maps shows "Permanently closed"
   - Business registration shows "cancelled" or "deregistered"
   - Only old/archived content can be found

**Decision rules:**
- If the domain check says DOMAIN_DEAD → the business is likely closed. You need VERY strong current evidence from other sources (active Google Business profile, active LinkedIn with current employees, recent reviews from this year) to override this. Old cached web content does NOT count.
- If you can find a LIVE, currently-active website that matches the name/location → proceed to Step 2
- If no website but you find CURRENT evidence (active Google Business listing, active LinkedIn with employees) → proceed to Step 2
- If the domain is dead/unregistered AND you cannot find CURRENT evidence of operations → output "Supplier unidentified"
- If ALL the information you can find appears to be historical (more than 2 years old) → output "Supplier unidentified"
- If you find multiple businesses with similar names and cannot determine which one this is → output "Supplier unidentified"
- If you are less than 90% confident you have the right, currently-operating entity → output "Supplier unidentified"

## Step 2: Research the business (only if positively identified)

Only proceed here if you are confident you have identified the correct, currently-operating business.

1. From their OWN website (not third-party sources), identify:
   - What they sell or manufacture
   - Their specialisations
   - Key brands they carry or represent
   - Industries they serve
2. Note alternative company names ONLY if stated on their own website (e.g. "trading as X")
3. Note alternative domains ONLY if they redirect to the same business

## Required Output Format

Respond with ONLY a JSON object (no markdown fences, no extra text):
{{
  "status": "identified" or "unidentified",
  "summary": "<1-2 sentences: what the company does and their core specialisation>",
  "products": ["<product category 1>", "<product category 2>", ...],
  "services": ["<service 1>", "<service 2>", ...],
  "industries": ["<industry 1>", "<industry 2>", ...],
  "brands_carried": ["<brand 1>", "<brand 2>", ...],
  "alt_names": ["<alternative name 1>", ...],
  "alt_domains": ["<alternative domain 1>", ...]
}}

Rules:
- status: MUST be "identified" (you found the business) or "unidentified" (you could not confirm it exists/is correct).
- If status is "unidentified": set summary to "Supplier unidentified" and leave ALL arrays empty.
- products: Specific product categories from their own website (e.g. "Hydraulic Cylinders", "Diesel Engine Parts"). 5-20 items.
- services: Services they provide. Empty array if none found on their website.
- industries: Industries they serve (e.g. "Mining", "Construction"). 1-5 items.
- brands_carried: Major brands they distribute/represent. Empty array if not applicable or not clearly listed.
- alt_names: MOST companies have NONE. Only include if their own website says "trading as X" or "formerly known as X". Empty array is the expected default.
- alt_domains: MOST companies have NONE. Only include domains confirmed to be owned by the same entity. Empty array is the expected default.
- summary: Focus on WHAT they supply and to whom. Do not repeat their location.
- Do NOT use information from directory sites, marketplaces, or third-party reviews to populate products/brands unless confirmed on the company's own site."""


def get_suppliers_to_process(force: bool = False, limit: int | None = None) -> list[dict]:
    """Fetch suppliers from DB that need notes generated."""
    session = get_session()
    try:
        query = session.query(
            Supplier,
            func.count(Transaction.id).label("purchase_count"),
        ).outerjoin(
            Transaction, Transaction.supplier_id == Supplier.id
        ).group_by(Supplier.id)

        if not force:
            # Pick up suppliers with no notes_updated_at — either missing notes
            # or notes flagged as bad quality (notes_updated_at left NULL)
            query = query.filter(Supplier.notes_updated_at.is_(None))

        # Prioritise suppliers with more purchases (more likely to be important)
        query = query.order_by(func.count(Transaction.id).desc())

        if limit:
            query = query.limit(limit)

        rows = query.all()
        suppliers = []
        for s, pc in rows:
            # Extract email domains from contacts as fallback identifier
            email_domains = set()
            if s.contacts:
                for c in s.contacts:
                    if isinstance(c, dict) and c.get("email"):
                        domain = c["email"].split("@")[-1].lower()
                        # Skip generic email providers
                        if domain not in ("gmail.com", "yahoo.com", "hotmail.com",
                                          "outlook.com", "icloud.com", "live.com"):
                            email_domains.add(domain)

            suppliers.append({
                "id": str(s.id),
                "name": s.name,
                "url": s.url,
                "city": s.city,
                "country": s.country,
                "purchase_count": pc,
                "email_domains": sorted(email_domains),
            })
        return suppliers
    finally:
        session.close()


def build_prompt(supplier: dict) -> str:
    """Build the research prompt for a single supplier."""
    # Check primary URL domain
    url = supplier.get("url")
    email_domains = supplier.get("email_domains", [])

    # If no URL but we have email domains, use the first one as the domain to check
    check_url = url
    if not check_url and email_domains:
        check_url = email_domains[0]

    domain_status = check_domain_alive(check_url)

    return NOTES_PROMPT_TEMPLATE.format(
        name=supplier.get("name", "Unknown"),
        url=supplier.get("url") or "No URL available",
        email_domains=", ".join(email_domains) if email_domains else "None",
        city=supplier.get("city") or "Unknown",
        country=supplier.get("country") or "Unknown",
        purchase_count=supplier.get("purchase_count", 0),
        domain_status=domain_status,
        current_date=datetime.now().strftime("%d %B %Y"),
    )


def parse_response(text: str) -> dict:
    """Parse the LLM response, handling markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return json.loads(text)


def format_notes(result: dict) -> str:
    """Convert structured JSON result into a notes string optimized for embedding.

    Format:
        <summary sentence(s)>
        Products: <comma-separated list>
        Services: <comma-separated list>
        Industries: <comma-separated list>
        Brands: <comma-separated list>
    """
    parts = []

    summary = result.get("summary", "").strip()
    if summary and summary != "No information available":
        parts.append(summary)

    products = result.get("products", [])
    if products:
        parts.append(f"Products: {', '.join(products)}.")

    services = result.get("services", [])
    if services:
        parts.append(f"Services: {', '.join(services)}.")

    industries = result.get("industries", [])
    if industries:
        parts.append(f"Industries: {', '.join(industries)}.")

    brands = result.get("brands_carried", [])
    if brands:
        parts.append(f"Brands: {', '.join(brands)}.")

    return " ".join(parts) if parts else ""


def save_to_db(supplier_id: str, result: dict, notes_text: str) -> None:
    """Write notes and alt fields to the database."""
    session = get_session()
    try:
        supplier = session.query(Supplier).filter(Supplier.id == supplier_id).first()
        if not supplier:
            return

        supplier.notes = notes_text
        # Clear embedding so it gets regenerated on next embedding run
        supplier.embedding = None

        alt_names = result.get("alt_names", [])
        if alt_names:
            supplier.alt_names = alt_names

        alt_domains = result.get("alt_domains", [])
        if alt_domains:
            supplier.alt_domains = alt_domains

        supplier.modified_at = datetime.now(timezone.utc)
        supplier.modified_by = "ai:notes_generator"
        supplier.notes_updated_at = datetime.now(timezone.utc)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def research_supplier(client: genai.Client, model: str, supplier: dict) -> dict:
    """Research a single supplier using search-grounded Gemini."""
    prompt = build_prompt(supplier)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.2,
        ),
    )

    raw_text = response.text
    try:
        return parse_response(raw_text)
    except (json.JSONDecodeError, KeyError) as e:
        return {
            "summary": f"PARSE_ERROR: {e}",
            "products": [],
            "services": [],
            "industries": [],
            "brands_carried": [],
            "alt_names": [],
            "alt_domains": [],
            "_raw": raw_text[:500],
        }


def main():
    parser = argparse.ArgumentParser(
        description="Generate supplier notes via search-grounded Gemini research."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate notes for all suppliers, not just those missing notes."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of suppliers to process."
    )
    parser.add_argument(
        "--model", type=str, default=app_config.DEFAULT_MODEL,
        help=f"Gemini model to use (default: {app_config.DEFAULT_MODEL})."
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="Delay in seconds between API calls (default: 2.0)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be processed without calling the API."
    )
    args = parser.parse_args()

    suppliers = get_suppliers_to_process(force=args.force, limit=args.limit)
    total = len(suppliers)

    mode = "all" if args.force else "missing notes only"
    print(f"Found {total} suppliers to research ({mode})")

    if total == 0:
        print("Nothing to do.")
        return

    if args.dry_run:
        for i, s in enumerate(suppliers[:10], 1):
            print(f"  {i}. {s['name']} ({s.get('url', 'no URL')}) — {s['purchase_count']} purchases")
        if total > 10:
            print(f"  ... and {total - 10} more")
        return

    client = genai.Client()
    results = Counter()

    for i, supplier in enumerate(suppliers, 1):
        print(f"\n[{i}/{total}] {supplier['name']} ({supplier.get('url', 'no URL')})...")

        try:
            # Pre-check: if we can identify a domain (from URL or email) and it's dead,
            # skip the LLM entirely — no point researching a business whose domain is gone
            url = supplier.get("url")
            email_domains = supplier.get("email_domains", [])
            check_url = url or (email_domains[0] if email_domains else None)
            domain_status = check_domain_alive(check_url) if check_url else "NO_URL"

            if domain_status.startswith("DOMAIN_DEAD"):
                domain_name = check_url if check_url else "unknown"
                print(f"  ⚠ Domain dead ({domain_name}) — marking as unidentified")
                unidentified_result = {
                    "status": "unidentified",
                    "summary": "Supplier unidentified",
                    "products": [], "services": [], "industries": [],
                    "brands_carried": [], "alt_names": [], "alt_domains": [],
                }
                note = f"Supplier unidentified - domain ({domain_name}) not responding"
                save_to_db(supplier["id"], unidentified_result, note)
                results["DOMAIN_DEAD"] += 1
                continue

            result = research_supplier(client, args.model, supplier)
            summary = result.get("summary", "")
            status = result.get("status", "identified")

            if "PARSE_ERROR" in summary:
                print(f"  ✗ Parse error: {summary[:100]}")
                results["ERROR"] += 1
            elif status == "unidentified" or summary == "Supplier unidentified":
                print(f"  ⚠ Supplier unidentified — marking for manual review")
                save_to_db(supplier["id"], result, "Supplier unidentified - uncertainty")
                results["UNIDENTIFIED"] += 1
            elif summary == "No information available":
                print(f"  ⚠ No info found — marking for manual review")
                save_to_db(supplier["id"], result, "Supplier unidentified - no information found")
                results["NO_INFO"] += 1
            else:
                notes_text = format_notes(result)
                products_count = len(result.get("products", []))
                alt_count = len(result.get("alt_names", []))

                print(f"  → {summary[:100]}")
                print(f"    {products_count} products, {len(result.get('services', []))} services, {alt_count} alt names")

                save_to_db(supplier["id"], result, notes_text)
                results["OK"] += 1

        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results["ERROR"] += 1

        # Rate limiting
        if i < total:
            time.sleep(args.delay)

    # Summary
    print(f"\n{'='*60}")
    print(f"Done! Processed {total} suppliers.")
    print(f"\nResults:")
    for status, count in results.most_common():
        print(f"  {status}: {count}")


if __name__ == "__main__":
    main()
