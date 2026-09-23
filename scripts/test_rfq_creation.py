"""
test_rfq_creation.py — trigger the Gmail add-on "Create RFQ + OP" flow locally.

WHY THIS EXISTS
---------------
The Gmail add-on posts to production (`BACKEND_URL = https://agent.eaglexp.com.au`
in `addon/Code.gs`), and `/api/addon/*` is gated behind a Google OIDC identity
token that only Apps Script can mint. So there is no way to click the add-on and
have it hit your local server.

This script replays that flow **in-process**: it calls the same
`addon.create_rfq()` route function the add-on calls, with the same request body
(`{gmail_message_id, gmail_thread_id}`), against your local database. No HTTP and
no auth are involved, so nothing is added to the production auth surface — but
every guard and every write in the route runs exactly as it does in prod.

WHAT IT EMULATES
----------------
`create_rfq()` (`includes/dashboard/routes/addon.py`) does, in order:
  1. find (or create) the `email_tracking` row
  2. guard: email must be linked to a customer          -> `--link-customer`
  3. guard: email must not already be linked to an RFQ  -> `--reset`
  4. guard: not already processed (idempotency)         -> `--reset`
  5. create the RFQ synchronously (`_create_rfq_sync`)
  6. link the whole email thread to the RFQ (`rfq_token`)
  7. auto-create a NetSuite opportunity                 -> BLOCKED by default ⚠️
  8. spawn the LLM item-extraction pipeline in a daemon thread

Step 7 is the dangerous one. There is no NetSuite sandbox in this codebase and
`Config.NETSUITE_ACCOUNT_ID` defaults to `794882` — **production**. Both this
route and the pipeline call `create_and_link_opportunity()`, which writes a real
Opportunity record.

**NetSuite writes are therefore BLOCKED by default.** The call is intercepted and
reported as `🛡 intercepted`; nothing reaches NetSuite unless you explicitly pass
`--allow-netsuite`. Earlier versions made blocking opt-in (`--no-netsuite`) and
that was a mistake: `--reset` used to fall through to a trigger, so a command
that looked like "just clean up" created real opportunities. Fail closed.

⚠️  About step 8: the pipeline runs in a `daemon=True` thread. A one-shot script
would exit and kill it mid-run, leaving a half-populated RFQ and a stuck lock.
This script therefore waits (polls) until the run reaches a terminal state by
default. Use `--no-watch` only if you understand that trade-off.

GETTING TEST DATA IN
--------------------
Pick either:
  * real mail from your own test mailbox (recommended — attachments resolve):
        uv run python -m scripts.sync_gmail_mailboxes --user you@eagle-exports.com
  * copy emails + RFQs from production:
        uv run python -m scripts.sync_prod_mail_data --limit 50

USAGE
-----
    # 1. See what's available locally (safe, changes nothing)
    uv run python -m scripts.test_rfq_creation --recent 10

    # 2. Inspect one email: is a customer linked? already processed?
    uv run python -m scripts.test_rfq_creation --email-id 42844

    # 3. Link a customer if the route's guard complains (id or name fragment)
    uv run python -m scripts.test_rfq_creation --email-id 42844 --link-customer "Eagle Exports"

    # 4. THE MAIN EVENT — run it and watch the lock engage/clear
    uv run python -m scripts.test_rfq_creation --email-id 42844

    # 5. Iterate. --reset is STANDALONE and stops — it does not create an RFQ.
    uv run python -m scripts.test_rfq_creation --email-id 42844 --reset
    uv run python -m scripts.test_rfq_creation --email-id 42844

NetSuite writes never happen unless you add `--allow-netsuite`, which you should
not need for local testing.

    # Test the other code path (pipeline creates the RFQ itself, no add-on route)
    uv run python -m scripts.test_rfq_creation --email-id 42844 --direct

WATCHING THE AGENT-WORKING LOCK
-------------------------------
Step 4 prints the RFQ number and its dashboard URL as soon as the RFQ exists.
Open that URL in one window, then run the script in another:

  * the banner appears at the top of the RFQ Items tab
  * `--watch` (default) prints each `pipeline_activity.step` transition
  * the Add/Edit/Delete controls are gone; a direct POST gets 409
  * when the run finishes the banner disappears on its own and the detail lines
    appear (~4s poll)

To *see* the 409, while the banner is up try the browser devtools console on the
RFQ page:

    htmx.ajax('POST', '/partial/rfqs/<RFQ-NUMBER>/add-item',
              {target: '#main-content', values: {input_description: 'should fail'}})

SAFETY
------
Refuses to run against a non-local database unless you pass `--yes`. This is
deliberate: `PROD_DATABASE_URL` lives in `.env`, and this script WRITES.

NetSuite writes are blocked by default (see above). Actions that stop instead of
running: `--recent`, `--dry-run`, `--reset`. Only a plain "inspect or run"
invocation reaches the trigger.

REPRODUCING EXTRACTION FAILURES ON DEMAND
-----------------------------------------
Some failures only happen upstream at random — e.g. Gemini returning
`500 INTERNAL` for one PDF, which silently shortened an RFQ. You cannot re-create
that by retrying, so use `--inject-attachment-failure` to make a named attachment
report as unreadable. Everything downstream is the real production code path:
the bundle report, `rfq_creation_result["input"]`, the human warning, and the
"Attachments Read" block in the comms modal.

    # the real case: 10 attachments, one PDF that Gemini once 500'd on
    uv run python -m scripts.test_rfq_creation --email-id 49663 --reset --yes
    uv run python -m scripts.test_rfq_creation --email-id 49663 \
        --inject-attachment-failure "estimate QBRI1207.pdf:model_error" --yes

Syntax is `FILENAME[:CODE]`, repeatable, `CODE` defaults to `model_error`.
Use `*:CODE` to fail every attachment on the email. Valid codes:

    model_error    upstream call failed (transient — the original bug)
    parse_error    returned content we could not parse
    empty          read successfully but yielded nothing
    fetch_failed   could not retrieve the attachment bytes
    unsupported    file type we never handle (a deterministic gap)

This is a test-only monkeypatch inside this script — no production module gains
a `if TESTING` branch, and no env var can turn it on in the deployed app. It
patches the extractors as bound in `supplier_quote_pipeline` (the caller's
references), not the definitions in `email_pipeline`; patching the definitions
would silently do nothing.

Not covered: `bundle_failure` codes (`no_content`, `email_not_found`) happen
before the attachment loop and are not reachable this way.
"""

import argparse
import inspect
import json
import os
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import text

# Running this file directly (rather than with `-m`) puts `scripts/` on
# sys.path[0], not the repo root, so `includes.*` would not resolve. Several
# scripts in this repo do the same. Both invocations therefore work:
#     uv run python -m scripts.test_rfq_creation ...
#     uv run python scripts/test_rfq_creation.py ...
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 300

# Steps the create-RFQ pipeline reports via rfqs.pipeline_activity.step.
STEP_LABELS = {
    "extracting_items": "reading the email (LLM extraction)",
    "adding_items": "adding items to the RFQ",
    "updating_details": "updating title/notes",
}


# ---------------------------------------------------------------------------
# Safety / connection helpers
# ---------------------------------------------------------------------------

def _db_label() -> tuple[str, str | None]:
    """Return (label, host) for the database this script will write to."""
    from config.settings import Config

    url = Config.DATABASE_URL or ""
    # postgresql+psycopg://user:pass@host:port/db
    host = None
    if "@" in url:
        netloc = url.split("@", 1)[1]
        host = netloc.split("/", 1)[0].split(":", 1)[0]
    return url.split("@")[-1] if "@" in url else url, host


def assert_local_db(assume_yes: bool) -> None:
    """Refuse to write to anything that isn't obviously local."""
    label, host = _db_label()
    if host in LOCAL_HOSTS:
        return
    print(f"\n  ⚠️  DATABASE_URL points at host {host!r}, not localhost.")
    print(f"      Target: {label}")
    print("      This script WRITES (creates RFQs, links emails, resets guards).")
    if not assume_yes:
        print("      Refusing to continue. Re-run with --yes if you are certain.\n")
        sys.exit(2)
    print("      --yes given — continuing anyway.\n")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _hr(char: str = "=", width: int = 78) -> None:
    print(char * width)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fmt_ts(value) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    return str(value)[:16].replace("T", " ")


# ---------------------------------------------------------------------------
# Reading state
# ---------------------------------------------------------------------------

def snapshot(session, email_id: int) -> dict | None:
    """Current state of the email_tracking row plus its RFQ's lock, if any."""
    row = session.execute(text("""
        SELECT
            et.id, et.subject, et.direction, et.email_type,
            et.gmail_message_id, et.gmail_thread_id, et.user_email,
            et.sender_email, et.recipient_email, et.created_at,
            et.customer_id, et.rfq_token, et.rfq_id, et.match_type,
            et.rfq_creation_result,
            et.body_markdown IS NOT NULL AS has_body,
            CASE WHEN et.attachments_json IS NOT NULL
                 THEN jsonb_array_length(et.attachments_json) ELSE 0 END AS attachment_count,
            c.companyname AS customer_name,
            c.netsuite_id AS customer_netsuite_id
        FROM email_tracking et
        LEFT JOIN customers c ON c.id = et.customer_id
        WHERE et.id = :id
    """), {"id": email_id}).mappings().first()
    if not row:
        return None

    snap = dict(row)

    rfq_number = None
    if isinstance(snap.get("rfq_creation_result"), dict):
        rfq_number = snap["rfq_creation_result"].get("rfq_number")
    rfq_number = rfq_number or snap.get("rfq_token") or snap.get("rfq_id")

    snap["rfq_number"] = rfq_number
    snap["rfq"] = None
    if rfq_number:
        rfq = session.execute(text("""
            SELECT rfq_number, customer, status, pipeline_activity,
                   (SELECT count(*) FROM rfq_items i WHERE i.rfq_id = rfqs.id) AS item_count
            FROM rfqs WHERE rfq_number = :n
        """), {"n": rfq_number}).mappings().first()
        if rfq:
            snap["rfq"] = dict(rfq)
    return snap


def print_snapshot(snap: dict, base_url: str) -> None:
    """Human summary of one email + its pipeline/lock state."""
    _hr()
    print(f"EMAIL #{snap['id']}   {snap['direction'] or '?'}")
    _hr()
    print(f"  Subject:     {snap['subject'] or '(none)'}")
    print(f"  From:        {snap['sender_email'] or '—'}")
    print(f"  Mailbox:     {snap['user_email'] or '—'}")
    print(f"  Date:        {_fmt_ts(snap['created_at'])}")
    print(f"  Body:        {'yes' if snap['has_body'] else 'NO — extraction will fetch from Gmail'}")
    print(f"  Attachments: {snap['attachment_count']}")
    print(f"  Customer:    {snap['customer_name'] or '(none linked)'}"
          + (f"  [netsuite_id={snap['customer_netsuite_id']}]"
             if snap.get("customer_netsuite_id") else ""))

    result = snap.get("rfq_creation_result")
    status = result.get("status") if isinstance(result, dict) else None

    print("\n  Add-on guard preconditions:")
    print(f"    customer linked       {'✓' if snap['customer_id'] else '✗  -> use --link-customer'}")
    print(f"    not linked to RFQ     {'✓' if not snap['rfq_number'] else '✗  -> use --reset'}")
    print(f"    not already processed {'✓' if not result else '✗  -> use --reset'}")

    if snap.get("rfq"):
        rfq = snap["rfq"]
        print(f"\n  RFQ:         {rfq['rfq_number']}  ({rfq['item_count']} item(s), status={rfq['status']})")
        print(f"               {base_url}/rfqs/{rfq['rfq_number']}")
        act = rfq.get("pipeline_activity")
        if act:
            print(f"  LOCK:        ACTIVE — step={act.get('step')} "
                  f"(heartbeat {act.get('heartbeat_at', '?')[:19]})")
        else:
            print("  LOCK:        none (RFQ is editable)")
    if status:
        print(f"\n  Last result: {status} "
              f"(items_extracted={result.get('items_extracted', 0)})")
        for w in (result.get("warnings") or [])[:5]:
            print(f"    warning: {w}")
        print_input_report(result, prefix="    ")
    print()


def print_input_report(result: dict, prefix: str = "             ") -> None:
    """Show what the run could NOT read from the email.

    This is the whole point of the input-completeness work: an attachment that
    failed to extract used to vanish silently, leaving an RFQ that was quietly
    one line short. Printed only when there is something to say.
    """
    report = result.get("input") if isinstance(result, dict) else None
    if not report:
        return

    total = report.get("attachment_total") or 0
    read = report.get("attachment_read") or 0
    skipped = report.get("skipped_as_signature") or 0
    failures = report.get("failures") or []

    print(f"{prefix}input:       {read} of {total} attachments read"
          + (f" ({skipped} skipped as signature)" if skipped else ""))
    if report.get("bundle_failure"):
        print(f"{prefix}bundle:      FAILED — {report['bundle_failure']}")
    for f in failures:
        print(f"{prefix}unreadable:  {f.get('filename')} "
              f"[{f.get('code')}] {str(f.get('detail') or '')[:70]}")


def list_recent(session, count: int, base_url: str, search: str | None = None) -> None:
    """Show recent received emails and how ready each is to be RFQ-created."""
    params = {"n": count}
    where = "WHERE et.direction = 'received'"
    if search:
        where += " AND (et.subject ILIKE :q OR et.sender_email ILIKE :q)"
        params["q"] = f"%{search}%"

    rows = session.execute(text(f"""
        SELECT
            et.id, et.subject, et.sender_email, et.created_at,
            et.customer_id, c.companyname AS customer_name,
            et.rfq_token, et.rfq_creation_result,
            (et.rfq_creation_result ->> 'rfq_number') AS created_rfq
        FROM email_tracking et
        LEFT JOIN customers c ON c.id = et.customer_id
        {where}
        ORDER BY et.created_at DESC NULLS LAST
        LIMIT :n
    """), params).mappings().all()

    _hr()
    title = f"MOST RECENT {len(rows)} RECEIVED EMAILS"
    if search:
        title += f" matching {search!r}"
    print(title)
    _hr()
    print(f"{'ID':>7}  {'CUST':<4} {'PROC':<4} {'RFQ':<14} {'DATE':<17} SUBJECT")
    for r in rows:
        result = r["rfq_creation_result"]
        done = "yes" if result else "-"
        rfq = r["created_rfq"] or r["rfq_token"] or "-"
        print(f"{r['id']:>7}  {'✓' if r['customer_id'] else '-':<4} "
              f"{done:<4} {rfq[:14]:<14} {_fmt_ts(r['created_at']):<17} "
              f"{(r['subject'] or '')[:44]}")
    print("\n  CUST = customer linked (required by the add-on)")
    print("  PROC = already processed (needs --reset to re-run)")
    print(f"\n  Next:  uv run python -m scripts.test_rfq_creation --email-id <ID>\n")


# ---------------------------------------------------------------------------
# Mutating helpers
# ---------------------------------------------------------------------------

def link_customer(session, snap: dict, query: str, assume_yes: bool,
                  dry_run: bool = False) -> bool:
    """Link the email to a customer, by UUID or company-name fragment."""
    from includes.dashboard.models import Customer

    matches = session.query(Customer).filter(
        Customer.companyname.ilike(f"%{query}%")
    ).limit(10).all()

    if not matches:
        print(f"  ✗ No customer matches {query!r}")
        return False
    if len(matches) > 1:
        print(f"  ✗ {len(matches)} customers match {query!r} — narrow it down:")
        for c in matches:
            print(f"      {c.id}  {c.companyname}")
        return False

    customer = matches[0]
    if snap["customer_id"] and str(snap["customer_id"]) == str(customer.id):
        print(f"  = Already linked to {customer.companyname}")
        return True

    print(f"  Linking email #{snap['id']} -> customer {customer.companyname} ({customer.id})")
    if customer.isinactive:
        print("  ⚠️  That customer is inactive — the add-on route will refuse it.")
    if dry_run:
        print("  (dry run — not written)")
        return True
    if not assume_yes and not _confirm():
        print("  Aborted.")
        return False

    session.execute(text(
        "UPDATE email_tracking SET customer_id = :cid, match_type = 'manual' WHERE id = :id"
    ), {"cid": str(customer.id), "id": snap["id"]})
    session.commit()
    print("  ✓ Linked (as the add-on's link-email step would)")
    return True


def reset_email(session, snap: dict, assume_yes: bool,
                dry_run: bool = False) -> bool:
    """Undo a previous create-RFQ run so the same email can be replayed.

    Clears the idempotency guards for the whole email *thread* (the route links
    the entire thread, so resetting one message would leave the rest blocking),
    and deletes the RFQ the previous run created along with its items.
    """
    thread_id = snap["gmail_thread_id"]
    rfq_number = snap["rfq_number"]

    _hr()
    print("RESET")
    _hr()
    print(f"  Email thread: {thread_id}")
    print(f"  RFQ to remove: {rfq_number or '(none recorded)'}")

    refs = session.execute(text(
        "SELECT count(*) FROM email_tracking WHERE gmail_thread_id = :t"
    ), {"t": thread_id}).scalar()

    opportunity = None
    if rfq_number:
        opportunity = session.execute(text(
            "SELECT netsuite_opportunity FROM rfqs WHERE rfq_number = :n"
        ), {"n": rfq_number}).scalar()
    if opportunity:
        print(f"  ⚠️  This RFQ has a linked NetSuite opportunity ({opportunity}).")
        print("      --reset will NOT delete it from NetSuite — clean that up by hand.")

    if dry_run:
        print(f"  (dry run — would delete {rfq_number or 'nothing'} and clear "
              f"guards on {refs} email(s))")
        return True
    if not assume_yes and not _confirm(
            f"Delete {rfq_number or 'no RFQ'} and clear guards on {refs} email(s)?"):
        print("  Aborted.")
        return False

    if rfq_number:
        # Items first — no ON DELETE CASCADE guaranteed at the DB level.
        session.execute(text("""
            DELETE FROM rfq_items
            WHERE rfq_id = (SELECT id FROM rfqs WHERE rfq_number = :n)
        """), {"n": rfq_number})
        session.execute(text("DELETE FROM rfq_threads WHERE rfq_number = :n"),
                        {"n": rfq_number})
        session.execute(text("DELETE FROM rfqs WHERE rfq_number = :n"),
                        {"n": rfq_number})

    session.execute(text("""
        UPDATE email_tracking
        SET rfq_token = NULL,
            rfq_id = NULL,
            rfq_creation_result = NULL,
            match_type = NULL
        WHERE gmail_thread_id = :t
    """), {"t": thread_id})
    session.commit()

    print(f"  ✓ Cleared guards on {refs} email(s)"
          + (f" and deleted {rfq_number}" if rfq_number else ""))
    print("\n  Ready to re-run.\n")
    return True


def _confirm(prompt: str = "Proceed?") -> bool:
    try:
        return input(f"  {prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------------------
# Triggering
# ---------------------------------------------------------------------------

class NetSuiteRecorder:
    """Stand-in for create_and_link_opportunity() that writes nothing.

    Return shape matches CreateResult so the calling code's
    `result.success` / `result.tran_id` / `result.error` reads keep working.
    """

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, rfq_id, *args, **kwargs):
        self.calls.append(rfq_id)
        print(f"  🛡 intercepted: NetSuite opportunity for {rfq_id} NOT created")
        return SimpleNamespace(
            success=False,
            error="skipped (test_rfq_creation blocks NetSuite writes by default)",
            tran_id=None,
            netsuite_id=None,
        )


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------

def failure_codes() -> list[str]:
    """Valid `--inject-attachment-failure` codes, read off the real enum."""
    from includes.email_pipeline import AttachmentFailure

    return [c.value for c in AttachmentFailure]


def parse_injection_specs(raw: list[str] | None) -> dict[str, str]:
    """Turn `['name.pdf:code', ...]` into `{filename: code}`.

    Exits loudly on an unknown code rather than injecting nothing.
    """
    specs: dict[str, str] = {}
    for item in raw or []:
        filename, sep, code = item.rpartition(":")
        if not sep:                      # bare filename — use the default
            filename, code = item, "model_error"
        filename, code = filename.strip(), (code.strip() or "model_error")
        if code not in failure_codes():
            raise SystemExit(
                f"\n  ✗ Unknown failure code {code!r} in --inject-attachment-failure.\n"
                f"    Valid codes: {', '.join(failure_codes())}\n"
            )
        specs[filename] = code
    return specs


def attachment_filenames(session, email_id: int) -> list[str]:
    """Filenames stored on the email's `attachments_json`, in order."""
    rows = session.execute(text("""
        SELECT a->>'filename'
        FROM email_tracking et,
             jsonb_array_elements(et.attachments_json) a
        WHERE et.id = :id
        ORDER BY a->>'filename'
    """), {"id": email_id}).scalars().all()
    return [r for r in rows if r]


def build_attachment_failure_injection(specs: dict[str, str]):
    """Force named attachments to report as unreadable, in-process.

    Returns a `patch.multiple` context manager over the three extractor names
    **as bound in `supplier_quote_pipeline`**. Those are the references the
    pipeline actually calls; patching the definitions in `email_pipeline` would
    replace a name nobody looks up and silently do nothing.

    Each match returns a genuine `AttachmentExtraction` carrying a real
    `AttachmentFailure`, so every downstream branch runs unmodified.
    """
    from unittest.mock import patch

    import includes.tools.supplier_quote_pipeline as sqp
    from includes.email_pipeline import (
        PLACEHOLDER_UNREADABLE, AttachmentExtraction, AttachmentFailure,
    )

    def _wrap(real):
        sig = inspect.signature(real)

        def _fake(*args, **kwargs):
            try:
                filename = sig.bind(*args, **kwargs).arguments.get("filename")
            except TypeError:
                filename = None
            code = specs.get(filename) if filename else None
            code = code or specs.get("*")
            if not code:
                return real(*args, **kwargs)
            print(f"  💉 injected failure: {filename} -> {code}")
            return AttachmentExtraction(
                text=PLACEHOLDER_UNREADABLE,
                failure=AttachmentFailure(code),
                detail="injected by --inject-attachment-failure (test only)",
            )

        return _fake

    return patch.multiple(
        sqp,
        extract_pdf_content=_wrap(sqp.extract_pdf_content),
        extract_image_content=_wrap(sqp.extract_image_content),
        extract_spreadsheet_content=_wrap(sqp.extract_spreadsheet_content),
    )


def trigger_via_route(snap: dict) -> tuple[int, dict]:
    """Replay exactly what the Gmail add-on posts, in-process.

    Calls the real `addon.create_rfq()` route function directly — same guards,
    same writes, same pipeline trigger — minus the HTTP layer and the Google
    identity token, which is the whole reason this script exists.
    """
    from includes.dashboard.routes.addon import CreateRfqRequest, create_rfq

    user_payload = {
        "email": snap["user_email"] or "test@eagle-exports.com",
        "hd": "eagle-exports.com",
    }
    body = CreateRfqRequest(
        gmail_message_id=snap["gmail_message_id"],
        gmail_thread_id=snap["gmail_thread_id"],
        direction=snap["direction"],
        sender_email=snap["sender_email"],
        recipient_email=snap["recipient_email"],
        user_email=snap["user_email"],
    )

    print("  POST /api/addon/create-rfq  (in-process, no HTTP)")
    print(f"    gmail_message_id: {body.gmail_message_id}")
    print(f"    gmail_thread_id:  {body.gmail_thread_id}")
    print(f"    as user:          {user_payload['email']}\n")

    response = create_rfq(body, user_payload)
    try:
        payload = json.loads(response.body)
    except Exception:
        payload = {"status": "unknown", "message": str(response.body)}
    return response.status_code, payload


def trigger_directly(snap: dict) -> tuple[int, dict]:
    """Skip the add-on route; let the pipeline create the RFQ itself.

    Exercises the other lock path (RFQ created *by* the pipeline rather than
    pre-created by the caller), and the add-on's customer guard is not applied.
    """
    from includes.tools.rfq_creation_pipeline import trigger_rfq_creation_pipeline

    print("  Calling trigger_rfq_creation_pipeline() directly (no add-on route)")
    print("  NOTE: the customer-linked guard is NOT checked on this path.\n")
    trigger_rfq_creation_pipeline(
        snap["id"],
        user_id=snap["user_email"] or "test-script",
        rfq_number=None,
    )
    return 200, {"status": "ok", "message": "pipeline triggered directly"}


# ---------------------------------------------------------------------------
# Watching
# ---------------------------------------------------------------------------

def watch(session, email_id: int, rfq_number: str | None, base_url: str,
          timeout: int, announced_url: set) -> None:
    """Poll until the run reaches a terminal state, printing lock transitions.

    Also keeps this process alive: the pipeline runs in a daemon thread, so
    exiting here would kill it mid-run.
    """
    started = time.time()
    last_line = None
    last_rfq = rfq_number

    while True:
        session.expire_all()
        snap = snapshot(session, email_id)
        if snap is None:
            print("  ✗ email row disappeared")
            return

        if not last_rfq and snap["rfq_number"]:
            last_rfq = snap["rfq_number"]

        # Announce the dashboard URL the moment the RFQ exists, so the user can
        # open it and watch the banner while the pipeline is still working.
        if last_rfq and last_rfq not in announced_url:
            announced_url.add(last_rfq)
            print(f"\n  → RFQ created: {last_rfq}")
            print(f"    Open {base_url}/rfqs/{last_rfq} now to watch the banner.\n")

        result = snap.get("rfq_creation_result")
        status = result.get("status") if isinstance(result, dict) else None
        act = (snap.get("rfq") or {}).get("pipeline_activity")
        step = act.get("step") if isinstance(act, dict) else None

        if status and status != "processing":
            # Terminal.
            items = result.get("items_extracted", 0)
            print(f"  [{_ts()}] pipeline finished — status={status}, items={items}")
            for w in (result.get("warnings") or []):
                print(f"             warning: {w}")
            if result.get("error"):
                print(f"             error: {result['error']}")
            print_input_report(result)

            final = snapshot(session, email_id)
            if final and final.get("rfq"):
                print(f"  [{_ts()}] lock: {'STILL SET (bug!)' if final['rfq'].get('pipeline_activity') else 'cleared'}"
                      f"   items on RFQ: {final['rfq']['item_count']}")
                announced_url.add("_done")
                print(f"\n  Check it: {base_url}/rfqs/{final['rfq']['rfq_number']}\n")
            return

        line = f"step={step or '—'}  status={status or '—'}"
        if line != last_line:
            print(f"  [{_ts()}] {line}")
            last_line = line

        if time.time() - started > timeout:
            print(f"\n  ✗ Timed out after {timeout}s.")
            print("     The pipeline may still be running. If the lock is stuck,")
            print("     it self-clears 600s after the last heartbeat, or run --reset.")
            return

        time.sleep(2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_email_id(session, args) -> int | None:
    if args.email_id:
        return args.email_id
    if args.gmail_message_id:
        found = session.execute(text(
            "SELECT id FROM email_tracking WHERE gmail_message_id = :m"
        ), {"m": args.gmail_message_id}).scalar()
        if not found:
            print(f"  ✗ No email_tracking row with gmail_message_id={args.gmail_message_id}")
        return found
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trigger the Gmail add-on 'Create RFQ' flow against the LOCAL database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The add-on posts to production, so it cannot drive a local server. This\n"
            "replays the same code path in-process.\n\n"
            "Quick start:\n"
            "  --recent 10                       list candidate emails\n"
            "  --email-id N                      inspect one email\n"
            "  --email-id N --link-customer X    satisfy the customer guard\n"
            "  --email-id N                      run it and watch the lock\n"
            "  --email-id N --reset              clean up (standalone, creates nothing)\n"
        ),
    )
    pick = parser.add_argument_group("choosing an email")
    pick.add_argument("--email-id", type=int, help="email_tracking.id to process")
    pick.add_argument("--gmail-message-id", help="Gmail message id (alternative to --email-id)")
    pick.add_argument("--recent", type=int, metavar="N",
                      help="List the N most recent received emails and exit")
    pick.add_argument("--search", metavar="TEXT",
                      help="With --recent: filter by subject or sender")

    actions = parser.add_argument_group("actions")
    actions.add_argument("--link-customer", metavar="NAME_OR_ID",
                         help="Link the email to a customer (satisfies the add-on guard)")
    actions.add_argument("--reset", action="store_true",
                         help="Delete the previously created RFQ and clear the guards")
    actions.add_argument("--allow-netsuite", action="store_true",
                         help="Permit REAL writes to the NetSuite account in "
                              "NETSUITE_ACCOUNT_ID. OFF by default — without this "
                              "the opportunity call is intercepted and nothing is "
                              "written to NetSuite.")
    actions.add_argument("--no-netsuite", action="store_true",
                         help=argparse.SUPPRESS)  # legacy no-op: now the default
    actions.add_argument("--direct", action="store_true",
                         help="Bypass the add-on route; let the pipeline create the RFQ")

    run = parser.add_argument_group("run control")
    run.add_argument("--dry-run", action="store_true", help="Show what would happen, then stop")
    run.add_argument("--no-watch", action="store_true",
                     help="Don't poll for completion. The pipeline is a daemon thread, so "
                          "the run is KILLED when this process exits — use with care.")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                     help=f"Seconds to wait for completion (default {DEFAULT_TIMEOUT})")
    run.add_argument("--base-url", default=DEFAULT_BASE_URL,
                     help=f"Dashboard base URL for printed links (default {DEFAULT_BASE_URL})")
    run.add_argument("--yes", "-y", action="store_true",
                     help="Skip confirmations, and allow a non-local database")

    inject = parser.add_argument_group("fault injection (testing the failure paths)")
    inject.add_argument("--inject-attachment-failure", metavar="FILENAME[:CODE]",
                        action="append",
                        help="Make a named attachment report as unreadable, so the "
                             "failure path can be tested without waiting for a real "
                             "upstream error. Repeatable. CODE defaults to "
                             "'model_error'; use '*' as the filename to fail every "
                             f"attachment. Codes: {', '.join(failure_codes())}")

    args = parser.parse_args()

    from includes.dashboard.database import get_session

    assert_local_db(args.yes)
    _label, host = _db_label()
    print(f"\n  Local database: {_label}")

    session = get_session()
    try:
        if args.recent:
            list_recent(session, args.recent, args.base_url, args.search)
            return 0

        email_id = resolve_email_id(session, args)
        if not email_id:
            parser.print_help()
            print("\n  Tip: --recent 10 lists candidate emails.\n")
            return 2

        snap = snapshot(session, email_id)
        if not snap:
            print(f"  ✗ No email_tracking row with id={email_id}")
            return 2

        print_snapshot(snap, args.base_url)

        # Injecting a failure for an attachment the email does not have would
        # "pass" while testing nothing at all, so refuse instead. Checked before
        # the mutating prerequisites so a typo cannot trigger a --reset first.
        inject_specs = parse_injection_specs(args.inject_attachment_failure)
        if inject_specs:
            present = set(attachment_filenames(session, email_id))
            unknown = sorted(f for f in inject_specs if f != "*" and f not in present)
            if unknown:
                print("  ✗ --inject-attachment-failure names attachment(s) this "
                      "email does not have:\n")
                for f in unknown:
                    print(f"      {f}")
                print("\n  Attachments on this email:")
                for f in sorted(present):
                    print(f"      {f}")
                print()
                return 2

            print("  💉 FAULT INJECTION ACTIVE — test-only, no production code involved")
            for filename, code in inject_specs.items():
                label = "every attachment" if filename == "*" else filename
                print(f"      {label}  ->  {code}")
            print("      These attachments will be reported as unreadable, so the\n"
                  "      failure path (report, warning, modal block) runs for real.\n")

        # Mutating prerequisites.
        if args.link_customer:
            if not link_customer(session, snap, args.link_customer, args.yes,
                                 dry_run=args.dry_run):
                return 1
            snap = snapshot(session, email_id)
            print_snapshot(snap, args.base_url)

        if args.reset:
            if not reset_email(session, snap, args.yes, dry_run=args.dry_run):
                return 1
            snap = snapshot(session, email_id)
            print_snapshot(snap, args.base_url)
            if not args.dry_run:
                # --reset is a STANDALONE maintenance action: it must not fall
                # through to the trigger. It used to, which meant a "just reset
                # it" command also created an RFQ — and, before writes were
                # gated, real NetSuite opportunities.
                print("  --reset is standalone — no RFQ was created.")
                print("  Re-run without --reset to create one.\n")
                return 0

        # Guard preconditions the route itself enforces — fail early and clearly.
        blockers = []
        if not snap["customer_id"]:
            blockers.append("no customer linked            -> --link-customer NAME")
        if snap["rfq_number"]:
            blockers.append(f"already linked to {snap['rfq_number']}   -> --reset")
        if snap.get("rfq_creation_result"):
            blockers.append("already processed             -> --reset")

        if args.dry_run:
            # Evaluate the guards as if the requested --link-customer/--reset had
            # run, otherwise every dry run reports the blockers the user just
            # asked it to simulate away.
            simulated = []
            still_blocking = list(blockers)
            if args.link_customer:
                simulated.append(f"link customer {args.link_customer!r}")
                still_blocking = [b for b in still_blocking
                                  if not b.startswith("no customer linked")]
            if args.reset:
                simulated.append("delete the previous RFQ + clear guards")
                still_blocking = [b for b in still_blocking
                                  if not b.startswith(("already linked",
                                                       "already processed"))]

            _hr()
            print("DRY RUN — nothing was written")
            _hr()
            for s in simulated:
                print(f"  simulated: {s}")
            if still_blocking:
                print("\n  The add-on route would STILL reject this:\n")
                for b in still_blocking:
                    print(f"      {b}")
            else:
                print(f"  Guards pass. The route would:")
                print(f"    - create an RFQ for {snap['customer_name'] or '(customer)'}")
                print(f"    - link email thread {snap['gmail_thread_id']} to it")
                print("    - " + ("⚠️  create a PRODUCTION NetSuite opportunity "
                                  "(--allow-netsuite)"
                                  if args.allow_netsuite
                                  else "skip the NetSuite opportunity (writes blocked)"))
                print("    - start the item-extraction pipeline")
            print()
            return 0

        if blockers:
            print("  ✗ Cannot run yet — the add-on route would reject this:\n")
            for b in blockers:
                print(f"      {b}")
            print()
            return 1

        if args.allow_netsuite and snap.get("customer_netsuite_id"):
            from config.settings import Config

            print("  ⚠️⚠️  --allow-netsuite: this run WILL WRITE to the real NetSuite "
                  "account")
            print(f"      {Config.NETSUITE_ACCOUNT_ID} (production unless you changed it).")
            print(f"      A real Opportunity will be created for "
                  f"'{snap['customer_name']}' and must be cleaned up by hand.")
            if not args.yes and not _confirm("Really create a production opportunity?"):
                print("      Aborted. Drop --allow-netsuite to run safely.\n")
                return 1
            print()
        elif snap.get("customer_netsuite_id"):
            print("  🛡  NetSuite writes BLOCKED (default). The opportunity call will be")
            print("      intercepted and reported, not created. Pass --allow-netsuite to")
            print("      really write.\n")

        # ---- Trigger + watch ------------------------------------------------
        _hr()
        print("TRIGGER")
        _hr()

        # The patches must stay active while the pipeline thread runs, so the
        # whole trigger+watch block stays inside the stack.
        from unittest.mock import patch

        recorder = NetSuiteRecorder()
        with ExitStack() as stack:
            # FAIL CLOSED: NetSuite writes are intercepted unless explicitly allowed.
            if not args.allow_netsuite:
                stack.enter_context(patch(
                    "includes.netsuite.records.opportunity.create_and_link_opportunity",
                    recorder))
            if inject_specs:
                stack.enter_context(build_attachment_failure_injection(inject_specs))
            if args.direct:
                _, payload = trigger_directly(snap)
            else:
                _, payload = trigger_via_route(snap)

            status = payload.get("status")
            if status != "ok":
                print(f"  ✗ Backend said: {status} — {payload.get('message')}")
                return 1
            rfq_number = payload.get("rfq_number")
            print(f"  ✓ {payload.get('message')}")

            if args.no_watch:
                print("\n  --no-watch: the pipeline is a daemon thread and will be")
                print("  killed when this process exits. Re-run without --no-watch.\n")
                return 0

            print("\nWATCH")
            watch(session, email_id, rfq_number, args.base_url,
                  args.timeout, set())

            if not args.allow_netsuite:
                print(f"  🛡  NetSuite writes intercepted: {len(recorder.calls)} "
                      "(nothing was created in NetSuite)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
