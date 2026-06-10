#!/usr/bin/env python
"""Test Gmail API connectivity and domain-wide delegation.

This script verifies:
1. Service account key is readable
2. Domain-wide delegation credentials can be created
3. Gmail API responds for a test user
4. Scopes are properly configured

Usage:
    uv run python -m scripts.test_gmail_auth <staff_email@eagle-exports.com>
    
Example:
    uv run python -m scripts.test_gmail_auth tom@eagle-exports.com
"""

import sys
import json
import argparse

# Add project root to path
sys.path.insert(0, ".")

from includes.gmail import (
    get_service_account_info,
    get_credentials,
    get_gmail_client,
    test_connection,
)
from config.settings import Config


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
    parser = argparse.ArgumentParser(
        description="Test Gmail API connectivity and domain-wide delegation"
    )
    parser.add_argument(
        "staff_email",
        nargs="?",
        default=None,
        help="Staff email to test (e.g., tom@eagle-exports.com)",
    )
    args = parser.parse_args()

    print_section("Gmail API Connectivity Test")

    # Step 1: Verify service account key
    print_section("1. Service Account Key")
    try:
        sa_info = get_service_account_info()
        print_success("Service account key loaded")
        print_info(f"Client ID: {sa_info.get('client_id')}")
        print_info(f"Service Account Email: {sa_info.get('client_email')}")
        print_info(f"Project ID: {sa_info.get('project_id')}")
        print_info(f"Domain: {sa_info.get('universe_domain', 'googleapis.com')}")
    except FileNotFoundError as e:
        print_error(f"Service account key not found: {e}")
        sys.exit(1)

    # Step 2: Get staff email
    print_section("2. Staff Email (Impersonation Target)")
    
    if not args.staff_email:
        print_info("No staff email provided. Using default from config...")
        # You could read from env or config here
        print_error("Please provide staff email as argument")
        print_info("\nUsage: uv run python -m scripts.test_gmail_auth <email@eagle-exports.com>")
        sys.exit(1)
    
    staff_email = args.staff_email
    if not staff_email.endswith("@eagle-exports.com"):
        print_error(f"Email must be from @eagle-exports.com domain, got: {staff_email}")
        sys.exit(1)
    
    print_success(f"Testing with staff email: {staff_email}")

    # Step 3: Create credentials
    print_section("3. Domain-Wide Delegation Credentials")
    try:
        creds = get_credentials(staff_email)
        print_success("Credentials created successfully")
        print_info(f"Scopes: {', '.join(creds.scopes)}")
        print_info(f"Service account: {creds.service_account_email}")
    except Exception as e:
        print_error(f"Failed to create credentials: {e}")
        sys.exit(1)

    # Step 4: Build Gmail client
    print_section("4. Gmail API Client")
    try:
        gmail = get_gmail_client(staff_email)
        print_success("Gmail API client built")
    except Exception as e:
        print_error(f"Failed to build Gmail client: {e}")
        sys.exit(1)

    # Step 5: Test connection
    print_section("5. API Connectivity Test")
    result = test_connection(staff_email)
    
    if result["status"] == "ok":
        print_success(result["message"])
        details = result["details"]
        print_info(f"Email: {details.get('email_address')}")
        print_info(f"Total messages: {details.get('messages_total')}")
        print_info(f"Total threads: {details.get('threads_total')}")
        print_info(f"Draft count: {details.get('draft_count')}")
    else:
        print_error(result["message"])
        if result.get("details"):
            print_info(f"Error details: {json.dumps(result['details'], indent=2)}")
        print_error("\nTroubleshooting:")
        print_info("1. Verify scopes are authorized in Google Workspace Admin:")
        print_info("   - Admin Console → Security → API Controls → Domain-wide Delegation")
        print_info("   - Find service account and add these scopes:")
        print_info("     • https://www.googleapis.com/auth/gmail.modify")
        print_info("     • https://www.googleapis.com/auth/gmail.readonly")
        print_info("     • https://www.googleapis.com/auth/gmail.send")
        print_info("2. Wait 5-10 minutes after adding scopes (propagation delay)")
        print_info("3. Verify staff email is in @eagle-exports.com domain")
        sys.exit(1)

    # Step 6: Test draft creation (optional)
    print_section("6. Test Draft Creation")
    try:
        gmail = get_gmail_client(staff_email)
        
        # Create a test draft message
        message = {
            "raw": __import__("base64").urlsafe_b64encode(
                b"From: test@eagle-exports.com\n"
                b"To: test@example.com\n"
                b"Subject: [TEST] Gmail API Draft Test\n"
                b"MIME-Version: 1.0\n"
                b"Content-Type: text/plain; charset=utf-8\n\n"
                b"This is a test draft created by Gmail API."
            ).decode()
        }
        
        draft = gmail.users().drafts().create(userId="me", body={"message": message}).execute()
        draft_id = draft.get("id")
        print_success(f"Test draft created: {draft_id}")
        
        # Clean up: delete the draft
        gmail.users().drafts().delete(userId="me", id=draft_id).execute()
        print_success("Test draft deleted successfully")
        
    except Exception as e:
        print_error(f"Draft creation test failed: {e}")
        sys.exit(1)

    # Summary
    print_section("Test Summary")
    print_success("All tests passed! Gmail API is ready for use.")
    print_info("Next steps:")
    print_info("1. Review the Gmail Integration plan at .github/prompts/plan-gmailIntegration.prompt.md")
    print_info("2. Set up email tracking database tables (Phase 1.4)")
    print_info("3. Implement draft service (Phase 2)")


if __name__ == "__main__":
    main()
