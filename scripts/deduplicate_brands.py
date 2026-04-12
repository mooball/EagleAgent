"""
deduplicate_brands.py

Interactive tool to find and resolve duplicate brands in the database.
Uses normalised key matching and fuzzy string matching (rapidfuzz) to identify
candidate duplicate groups, then presents them for human review.

Usage:
  Interactive (local):
    uv run python -m scripts.deduplicate_brands

  AI-assisted (LLM splits fuzzy groups into true duplicates):
    uv run python -m scripts.deduplicate_brands --ai

  Dry run (preview only, no changes):
    uv run python -m scripts.deduplicate_brands --dry-run

  Auto mode (picks shortest name as canonical):
    uv run python -m scripts.deduplicate_brands --auto

  Production:
    uv run python -m scripts.deduplicate_brands --production
"""

import json
import os
import re
import time
import argparse
from collections import defaultdict

from rapidfuzz import fuzz
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import Config
from includes.db_models import Brand


def get_engine(is_prod: bool = False):
    db_url = Config.PROD_DATABASE_URL if is_prod else Config.DATABASE_URL
    if not db_url:
        raise ValueError("Target Database URL is empty. Check your `.env` settings.")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(db_url)


def normalise_key(name: str) -> str:
    """Build a normalised comparison key for grouping obvious duplicates."""
    key = name.lower().strip()
    # Remove hyphens, apostrophes, dots, slashes, and other punctuation
    key = re.sub(r"[-'.\/,()&##+]", "", key)
    # Collapse whitespace
    key = re.sub(r"\s+", " ", key).strip()
    return key


def find_duplicate_groups(brands: list, threshold: int = 85) -> list:
    """
    Find candidate duplicate groups using two strategies:
    1. Exact normalised key match
    2. Fuzzy string matching above the threshold

    Returns a list of groups, where each group is a list of Brand objects.
    """
    # --- Strategy 1: Normalised key grouping ---
    key_groups = defaultdict(list)
    for brand in brands:
        key = normalise_key(brand.name)
        key_groups[key].append(brand)

    # Seed merged groups from exact key matches
    merged = {}  # brand.id -> group set index
    groups = []

    for key, members in key_groups.items():
        if len(members) > 1:
            group_idx = len(groups)
            groups.append(set(b.id for b in members))
            for b in members:
                merged[b.id] = group_idx

    # --- Strategy 2: Fuzzy matching across different key groups ---
    unique_keys = list(key_groups.keys())
    for i in range(len(unique_keys)):
        for j in range(i + 1, len(unique_keys)):
            score = fuzz.ratio(unique_keys[i], unique_keys[j])
            if score >= threshold:
                members_i = key_groups[unique_keys[i]]
                members_j = key_groups[unique_keys[j]]

                # Find existing group indices
                idx_i = merged.get(members_i[0].id)
                idx_j = merged.get(members_j[0].id)

                if idx_i is not None and idx_j is not None:
                    if idx_i == idx_j:
                        continue
                    # Merge group j into group i
                    groups[idx_i] |= groups[idx_j]
                    for bid in groups[idx_j]:
                        merged[bid] = idx_i
                    groups[idx_j] = set()  # empty the old group
                elif idx_i is not None:
                    for b in members_j:
                        groups[idx_i].add(b.id)
                        merged[b.id] = idx_i
                elif idx_j is not None:
                    for b in members_i:
                        groups[idx_j].add(b.id)
                        merged[b.id] = idx_j
                else:
                    group_idx = len(groups)
                    new_group = set()
                    for b in members_i + members_j:
                        new_group.add(b.id)
                        merged[b.id] = group_idx
                    groups.append(new_group)

    # Convert ID sets back to Brand lists, filtering empty groups and singletons
    brand_map = {b.id: b for b in brands}
    result = []
    for group_ids in groups:
        if len(group_ids) >= 2:
            result.append([brand_map[bid] for bid in group_ids])

    # Sort each group by name for consistent display
    for group in result:
        group.sort(key=lambda b: b.name.lower())

    return result


def pick_canonical_auto(group: list) -> int:
    """Auto-pick: choose the shortest name, tie-break by name alphabetically."""
    sorted_brands = sorted(group, key=lambda b: (len(b.name), b.name.lower()))
    return group.index(sorted_brands[0])


def display_group(group: list, group_num: int, total: int):
    """Display a candidate duplicate group."""
    print(f"\n{'='*60}")
    print(f"Duplicate group {group_num}/{total}:")
    print(f"{'='*60}")
    for i, brand in enumerate(group, 1):
        print(f"  {i}. \"{brand.name}\"  (netsuite_id: {brand.netsuite_id})")


def ai_split_group(group: list) -> list:
    """
    Use Gemini to split a fuzzy-matched group into true duplicate sub-groups.
    Returns a list of dicts: [{"canonical": "BrandName", "duplicates": ["Name1", "Name2"]}, ...]
    Names not in any duplicate relationship are omitted.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    brand_names = [b.name for b in group]

    prompt = f"""You are a data quality assistant. I have a list of brand names that fuzzy matching thinks might be duplicates of each other. Your job is to identify which ones are truly the same brand (just different casing, spelling mistakes, or minor variations) and which are distinct brands.

Brand names:
{json.dumps(brand_names)}

For each group of true duplicates, pick the best canonical name (prefer proper capitalisation, correct spelling, no trailing 's' unless it's part of the real name).

Respond ONLY with a JSON array. Each element should have:
- "canonical": the best version of the brand name
- "duplicates": array of ALL names in that duplicate group (including the canonical name)

Only include groups with 2 or more names. If a name is unique (not a duplicate of anything else), do not include it.

Example response:
[{{"canonical": "Hilti", "duplicates": ["Hilti", "HILTI", "hilti"]}}]"""

    model = ChatGoogleGenerativeAI(
        model="gemini-3-flash-preview",
        temperature=0,
        timeout=60,
    )

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = model.invoke(prompt)
            break
        except Exception as e:
            if attempt < max_retries and "DEADLINE_EXCEEDED" in str(e):
                print(f"  Timeout on attempt {attempt}/{max_retries}, retrying in {attempt * 5}s...")
                time.sleep(attempt * 5)
            else:
                print(f"  Warning: AI request failed: {e}")
                return []
    content = response.content
    if isinstance(content, list):
        # Extract text from content parts (e.g. [{"type": "text", "text": "..."}])
        parts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        text = "".join(parts).strip()
    else:
        text = content.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  Warning: Could not parse AI response. Falling back to manual mode.")
        print(f"  Raw response: {text[:200]}")
        return []


def apply_ai_suggestions(session, group: list, suggestions: list, dry_run: bool, auto: bool):
    """
    Apply AI-suggested duplicate sub-groups. Returns (groups_resolved, brands_marked).
    """
    # Build a name -> Brand lookup (case-insensitive, may have multiple matches)
    name_map = defaultdict(list)
    for brand in group:
        name_map[brand.name.lower()].append(brand)

    groups_resolved = 0
    brands_marked = 0

    for suggestion in suggestions:
        canonical_name = suggestion.get("canonical", "")
        duplicate_names = suggestion.get("duplicates", [])

        if len(duplicate_names) < 2:
            continue

        # Resolve names to Brand objects
        sub_group = []
        seen_ids = set()
        for name in duplicate_names:
            for brand in name_map.get(name.lower(), []):
                if brand.id not in seen_ids:
                    sub_group.append(brand)
                    seen_ids.add(brand.id)

        if len(sub_group) < 2:
            continue

        # Find the canonical brand
        canonical = None
        for brand in sub_group:
            if brand.name.lower() == canonical_name.lower():
                canonical = brand
                break
        if canonical is None:
            canonical = sub_group[0]

        # Display this sub-group
        print(f"\n  AI suggests these are the same brand (canonical: \"{canonical.name}\"):")
        for i, brand in enumerate(sub_group, 1):
            marker = " ← canonical" if brand.id == canonical.id else ""
            print(f"    {i}. \"{brand.name}\"  (netsuite_id: {brand.netsuite_id}){marker}")

        if dry_run:
            continue

        if not auto:
            while True:
                answer = input(f"\n    Accept? (y)es, (n)o, or pick different canonical (1-{len(sub_group)}): ").strip().lower()
                if answer in ("n", "no"):
                    canonical = None
                    break
                if answer in ("y", "yes"):
                    break
                try:
                    pick = int(answer) - 1
                    if 0 <= pick < len(sub_group):
                        canonical = sub_group[pick]
                        break
                except ValueError:
                    pass
                print(f"    Invalid input.")

            if canonical is None:
                print("    Skipped.")
                continue

        for brand in sub_group:
            if brand.id != canonical.id:
                brand.duplicate_of = canonical.id
                brands_marked += 1

        session.commit()
        groups_resolved += 1
        print(f"    ✓ Marked {len(sub_group) - 1} brand(s) as duplicate of \"{canonical.name}\"")

    return groups_resolved, brands_marked


def main():
    parser = argparse.ArgumentParser(description="Find and resolve duplicate brands.")
    parser.add_argument("--production", action="store_true", help="Target the PRODUCTION database.")
    parser.add_argument("--dry-run", action="store_true", help="Show duplicate groups without making changes.")
    parser.add_argument("--auto", action="store_true", help="Auto-pick canonical brand (shortest name, or accept AI suggestion).")
    parser.add_argument("--ai", action="store_true", help="Use AI (Gemini) to split fuzzy groups into true duplicate sub-groups.")
    parser.add_argument("--case-only", action="store_true", help="Only merge exact case-insensitive matches, skip everything else.")
    parser.add_argument("--threshold", type=int, default=92, help="Fuzzy match threshold (0-100, default: 92).")
    args = parser.parse_args()

    env_label = "PRODUCTION" if args.production else "LOCAL"
    print(f"[{env_label}] Connecting to database...")
    engine = get_engine(is_prod=args.production)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Load all canonical brands (not already marked as duplicates)
        brands = (
            session.query(Brand)
            .filter(Brand.duplicate_of.is_(None))
            .order_by(Brand.name)
            .all()
        )
        print(f"Loaded {len(brands)} canonical brands.")

        groups = find_duplicate_groups(brands, threshold=args.threshold)
        if not groups:
            print("No duplicate candidates found.")
            return

        print(f"Found {len(groups)} candidate duplicate group(s).")

        if args.dry_run:
            print("\n[DRY RUN] Showing groups only — no changes will be made.\n")

        groups_resolved = 0
        brands_marked = 0

        for idx, group in enumerate(groups, 1):
            # --- Auto-merge case-only duplicates silently ---
            # If all names in the group are identical case-insensitively, merge without asking
            lower_names = {b.name.lower() for b in group}
            if len(lower_names) == 1 and not args.dry_run:
                canonical = group[0]
                for brand in group[1:]:
                    brand.duplicate_of = canonical.id
                    brands_marked += 1
                session.commit()
                groups_resolved += 1
                print(f"  Auto-merged (case-only): {len(group)} → \"{canonical.name}\"")
                continue

            # In --case-only mode, skip groups that aren't pure case matches
            if args.case_only:
                continue

            display_group(group, idx, len(groups))

            if args.dry_run and not args.ai:
                continue

            # --- AI mode: let LLM split the group ---
            if args.ai:
                print(f"  Asking AI to analyse this group ({len(group)} brands)...")
                suggestions = ai_split_group(group)
                if not suggestions:
                    if args.dry_run:
                        continue
                    print("  AI found no duplicates in this group. Falling back to manual.")
                    # Fall through to manual mode below
                else:
                    resolved, marked = apply_ai_suggestions(session, group, suggestions, args.dry_run, args.auto)
                    groups_resolved += resolved
                    brands_marked += marked
                    time.sleep(1)  # brief pause between AI calls
                    continue

            if args.auto:
                choice = pick_canonical_auto(group)
                canonical = group[choice]
                print(f"  → Auto-selected: \"{canonical.name}\"")
            else:
                canonical = None
                while True:
                    answer = input(f"\n  Enter number to pick canonical (1-{len(group)}), 'r' to remove item, 's' to skip, 'q' to quit: ").strip().lower()
                    if answer == "q":
                        print(f"\nStopping early. Resolved {groups_resolved} group(s), marked {brands_marked} brand(s) as duplicates.")
                        return
                    if answer == "s":
                        break
                    if answer == "r":
                        rm = input(f"  Remove which item? (1-{len(group)}): ").strip()
                        try:
                            rm_idx = int(rm) - 1
                            if 0 <= rm_idx < len(group):
                                removed = group.pop(rm_idx)
                                print(f"  Removed \"{removed.name}\" from this group.")
                                if len(group) < 2:
                                    print("  Group now has fewer than 2 items — skipping.")
                                    break
                                display_group(group, idx, len(groups))
                            else:
                                print(f"  Invalid. Enter 1-{len(group)}.")
                        except ValueError:
                            print(f"  Invalid. Enter 1-{len(group)}.")
                        continue
                    try:
                        choice = int(answer) - 1
                        if 0 <= choice < len(group):
                            canonical = group[choice]
                            break
                    except ValueError:
                        pass
                    print(f"  Invalid input.")

                if canonical is None:
                    print("  Skipped.")
                    continue

            # Set duplicate_of on all non-canonical brands in the group
            for brand in group:
                if brand.id != canonical.id:
                    brand.duplicate_of = canonical.id
                    brands_marked += 1

            session.commit()
            groups_resolved += 1
            print(f"  ✓ Marked {len(group) - 1} brand(s) as duplicate of \"{canonical.name}\"")

        print(f"\n{'='*60}")
        print(f"Summary: {len(groups)} group(s) found, {groups_resolved} resolved, {brands_marked} brand(s) marked as duplicates.")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()
