"""READ-ONLY: run just the content-bundle step for an email and show its report.

This is the narrowest way to verify the input-completeness work: it reads the
email's attachments, extracts them exactly as the pipeline would, and prints the
resulting ContentBundle report. It creates no RFQ, resets nothing, and mutates no
rows (the only possible write is a Gmail content backfill, which is a no-op for
an email whose body is already stored).

Usage:
    uv run python -m scripts.probe_content_bundle 49663
    uv run python -m scripts.probe_content_bundle 49663 --inject "estimate QBRI1207.pdf:model_error"
    uv run python -m scripts.probe_content_bundle 49663 --inject "*:unsupported"
"""
import json
import os
import sys

# Importable both ways, same as scripts/test_rfq_creation.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.test_rfq_creation as t  # noqa: E402

email_id = int(sys.argv[1]) if len(sys.argv) > 1 else 49663

specs = {}
if "--inject" in sys.argv:
    specs = t.parse_injection_specs([sys.argv[sys.argv.index("--inject") + 1]])

print(f"building content bundle for email {email_id} ...")
if specs:
    print(f"injecting: {specs}  (using the same patcher the test script uses)")
print()

from includes.tools.supplier_quote_pipeline import build_content_bundle  # noqa: E402

if specs:
    with t.build_attachment_failure_injection(specs):
        bundle = build_content_bundle(email_id)
else:
    bundle = build_content_bundle(email_id)

report = bundle.to_dict()

print("=== ContentBundle report ===")
print(json.dumps(report, indent=2))

print("\n=== interpretation ===")
total, read = bundle.attachment_total, bundle.attachment_read
print(f"  attachments total          : {total}")
print(f"  attachments read           : {read}")
print(f"  skipped as signature       : {bundle.skipped_as_signature}")
print(f"  failures                   : {len(bundle.failures)}")
if bundle.bundle_failure:
    print(f"  BUNDLE FAILURE             : {bundle.bundle_failure}")

if bundle.failures:
    print("\n  unreadable attachments:")
    for f in bundle.failures:
        print(f"    - {f['filename']}  [{f['code']}]  {f['detail'][:90]}")
else:
    print("\n  every attachment was read (or skipped as a known signature)")

print(f"\n  bundle.complete            : {bundle.complete}")
print(f"  text length                : {len(bundle.text)} chars")
