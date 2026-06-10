#!/usr/bin/env python
"""Test Gmail draft service.

This script tests draft creation and email tracking functionality.

Usage:
    uv run python -m scripts.test_gmail_draft <staff_email@eagle-exports.com>
    
Example:
    uv run python -m scripts.test_gmail_draft harry@eagle-exports.com
"""

import sys
import argparse

# Add project root to path
sys.path.insert(0, ".")

from includes.gmail.draft_service import (
    create_draft_email,
    generate_compose_url,
    get_draft_info,
)


def print_section(title: str):
    """Print a formatted section header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def print_success(message: str):
    """Print a success message."""
    print(f"✓ {message}")


def print_error(message: str):
    """Print an error message."""
    print(f"✗ {message}")


def print_info(message: str, indent: int = 2):
    """Print an info message with optional indentation."""
    print(" " * indent + message)


def main():
    parser = argparse.ArgumentParser(description="Test Gmail draft service")
    parser.add_argument(
        "staff_email",
        nargs="?",
        default=None,
        help="Staff email to test (e.g., harry@eagle-exports.com)",
    )
    args = parser.parse_args()

    if not args.staff_email:
        print_error("Please provide staff email as argument")
        print_info("\nUsage: uv run python -m scripts.test_gmail_draft <email@eagle-exports.com>")
        sys.exit(1)

    staff_email = args.staff_email

    print_section("Gmail Draft Service Test")

    # Test 1: Generate compose URL
    print_section("1. Generate Compose URL")
    test_draft_id = "draft_test_12345"
    compose_url = generate_compose_url(test_draft_id, staff_email)
    print_success("Compose URL generated")
    print_info(f"URL: {compose_url}")
    
    # Verify URL contains expected parts
    assert "mail.google.com" in compose_url
    assert test_draft_id in compose_url
    # Email will be URL-encoded, so check for part of it
    assert "eagle-exports.com" in compose_url
    print_success("URL format verified")

    # Test 2: Create draft email
    print_section("2. Create Draft Email")
    
    result = create_draft_email(
        user_email=staff_email,
        recipient_email="test@example.com",
        subject="Quote Request",
        body_html="<p>Dear Supplier,</p><p>Could you provide a quote for the following items?</p>",
        rfq_id="RFQ-00001",
        email_type="rfq_outreach",
        opportunity_id=None,
    )
    
    if result["status"] != "ok":
        print_error(f"Failed to create draft: {result['message']}")
        if result.get("details"):
            print_info(f"Details: {result['details']}")
        sys.exit(1)
    
    print_success(result["message"])
    draft_id = result["draft_id"]
    thread_id = result["thread_id"]
    compose_url = result["compose_url"]
    
    print_info(f"Draft ID: {draft_id}")
    print_info(f"Thread ID: {thread_id}")
    print_info(f"Compose URL: {compose_url}")

    # Test 3: Fetch draft info
    print_section("3. Fetch Draft Info")
    
    draft_info = get_draft_info(draft_id, staff_email)
    if draft_info:
        print_success("Draft fetched successfully")
        print_info(f"Draft ID: {draft_info.get('id')}")
        print_info(f"Thread ID: {draft_info.get('thread_id')}")
    else:
        print_error("Failed to fetch draft info")
        sys.exit(1)

    # Test 4: Verify email_tracking database
    print_section("4. Verify email_tracking Database")
    try:
        from includes.dashboard.database import get_session
        from sqlalchemy import text
        
        session = get_session()
        result = session.execute(
            text("SELECT COUNT(*) FROM email_tracking WHERE gmail_draft_id = :draft_id"),
            {"draft_id": draft_id}
        ).scalar()
        session.close()
        
        if result > 0:
            print_success(f"Draft tracked in database")
        else:
            print_error(f"Draft not found in email_tracking table")
            sys.exit(1)
    except Exception as e:
        print_error(f"Database query failed: {e}")
        sys.exit(1)

    # Test 5: Verify RFQ email fields exist (summary only, not all events)
    print_section("5. Verify RFQ Email Summary Fields")
    try:
        from includes.dashboard.database import _sync_url
        from sqlalchemy import create_engine, inspect
        
        # Create engine to inspect schema
        engine = create_engine(_sync_url(), pool_pre_ping=True)
        inspector = inspect(engine)
        rfq_columns = inspector.get_columns('rfqs')
        column_names = [col['name'] for col in rfq_columns]
        
        # These are the summary/denormalization fields on RFQ
        # (email_tracking table is the source of truth for full history)
        required_fields = ['email_status', 'last_email_sent_at', 'supplier_emails']
        missing = [f for f in required_fields if f not in column_names]
        
        if missing:
            print_error(f"Missing RFQ email summary fields: {missing}")
            sys.exit(1)
        
        # Verify old single-thread fields were removed
        removed_fields = ['email_thread_id', 'email_draft_id', 'email_sent_at']
        still_exist = [f for f in removed_fields if f in column_names]
        
        if still_exist:
            print_error(f"Old single-thread fields still exist (should be removed): {still_exist}")
            sys.exit(1)
        
        print_success("RFQ email summary fields verified (many-threads design)")
        for field in required_fields:
            print_info(f"✓ {field}")
        
        print_success("Old single-thread fields removed as expected")
        for field in removed_fields:
            print_info(f"✓ {field} removed")
        
    except Exception as e:
        print_error(f"RFQ schema check failed: {e}")
        sys.exit(1)

    # Summary
    print_section("Test Summary")
    print_success("All tests passed! Draft service is ready.")
    print_info("\nArchitecture:")
    print_info("- email_tracking table: Source of truth for ALL email events")
    print_info("- RFQ summary fields: Denormalization for quick queries")
    print_info("  • email_status: Aggregate status (no_email_sent|draft_pending|sent|awaiting_reply)")
    print_info("  • last_email_sent_at: Most recent send time")
    print_info("  • supplier_emails: Contact list [{email, name}, ...]")
    print_info("\nThis design supports:")
    print_info("✓ Multiple threads per RFQ (one per supplier)")
    print_info("✓ Multiple email types (rfq_outreach, quote, invoice, etc.)")
    print_info("✓ Full conversation history with threading")
    print_info("\nNext steps:")
    print_info("1. Integrate draft creation into RFQ routes")
    print_info("2. Implement UI modal for compose URL")
    print_info("3. Implement mailbox scanning (Phase 3)")


if __name__ == "__main__":
    main()
