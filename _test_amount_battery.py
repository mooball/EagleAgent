"""Battery of experiments: how can we make NetSuite recompute a line's `amount`?

Target: OP73207 (test opportunity). Target line: the one with item EGTEST-ITEM-002.

Each test rewrites the item sublist and reports the target line + the
transaction `total` afterwards (the total is derived from line amounts, so it
cross-checks the REST view).

Usage: uv run python -u _test_amount_battery.py <test>
  a  change QUANTITY only, no amount
  b  change RATE + send AMOUNT + keep the `line` id
  c  change RATE + set custcol_update_line_on_record_save = true
  d  change RATE + send AMOUNT, plain PATCH (no replace=item)
  s  show current state
"""

import sys

from includes.netsuite.client import NetSuiteClient

OPP_ID = "1584602"
TARGET_ITEM = "EGTEST-ITEM-002"


def fetch(c):
    data = c.get(
        f"record/v1/opportunity/{OPP_ID}?expandSubResources=true"
    ).json()
    lines = ((data.get("item") or {}).get("items") or []) or []
    return data, lines


def find(lines, code):
    for ln in lines:
        if str((ln.get("item") or {}).get("refName")) == code:
            return ln
    return None


def report(data, lines, title):
    print(f"\n--- {title} ---")
    print(f"  transaction total = {data.get('total')}")
    for ln in lines:
        q = float(ln.get("quantity") or 0)
        r = float(ln.get("rate") or 0)
        a = float(ln.get("amount") or 0)
        code = (ln.get("item") or {}).get("refName")
        flag = "OK " if abs(a - round(q * r, 2)) < 0.005 else "BAD"
        mark = "  <== target" if code == TARGET_ITEM else ""
        print(f"  {flag} line {ln.get('line'):>4} {str(code):<16} "
              f"qty={q:<5} rate={r:<8} amount={a:<9}{mark}")
    print(f"  sum(amount) = {round(sum(float(l.get('amount') or 0) for l in lines), 2)}")


def clean(ln, keep_line=False, keep_amount=False):
    fresh = {k: v for k, v in ln.items() if k != "links"}
    if not keep_line:
        fresh.pop("line", None)
    if not keep_amount:
        fresh.pop("amount", None)
    return fresh


def apply(c, items, replace=True):
    if replace:
        return c.update_record("opportunity", OPP_ID,
                               {"item": {"items": items}},
                               params={"replace": "item"})
    return c.update_record("opportunity", OPP_ID,
                           {"item": {"items": items}})


def main():
    test = sys.argv[1] if len(sys.argv) > 1 else "s"
    c = NetSuiteClient()
    data, lines = fetch(c)
    report(data, lines, "BEFORE")
    target = find(lines, TARGET_ITEM)
    if not target:
        raise SystemExit(f"{TARGET_ITEM} not on the opportunity")
    qty = float(target.get("quantity") or 0)
    rate = float(target.get("rate") or 0)
    print(f"\nTARGET: qty={qty} rate={rate} amount={target.get('amount')} "
          f"line={target.get('line')}")

    if test == "a":
        new_qty = qty + 2
        print(f"TEST A: quantity {qty} -> {new_qty}, rate unchanged, "
              f"no amount sent")
        items = [clean(l) for l in lines]
        for it in items:
            if str((it.get("item") or {}).get("refName")) == TARGET_ITEM:
                it["quantity"] = new_qty
        apply(c, items)

    elif test == "b":
        new_rate = round(rate + 5.0, 2)
        print(f"TEST B: rate {rate} -> {new_rate}, explicit amount "
              f"{round(qty * new_rate, 2)}, keeping `line` id")
        items = [clean(l, keep_line=True) for l in lines]
        for it in items:
            if str((it.get("item") or {}).get("refName")) == TARGET_ITEM:
                it["rate"] = new_rate
                it["amount"] = round(qty * new_rate, 2)
        apply(c, items)

    elif test == "c":
        new_rate = round(rate + 5.0, 2)
        print(f"TEST C: rate {rate} -> {new_rate}, "
              f"custcol_update_line_on_record_save=true, no amount")
        items = [clean(l) for l in lines]
        for it in items:
            if str((it.get("item") or {}).get("refName")) == TARGET_ITEM:
                it["rate"] = new_rate
                it["custcol_update_line_on_record_save"] = True
        apply(c, items)

    elif test == "d":
        new_rate = round(rate + 5.0, 2)
        print(f"TEST D: rate {rate} -> {new_rate}, explicit amount "
              f"{round(qty * new_rate, 2)}, plain PATCH (no replace)")
        items = [clean(l, keep_line=True) for l in lines]
        for it in items:
            if str((it.get("item") or {}).get("refName")) == TARGET_ITEM:
                it["rate"] = new_rate
                it["amount"] = round(qty * new_rate, 2)
        apply(c, items, replace=False)

    elif test == "e":
        new_rate = round(rate + 5.0, 2)
        print(f"TEST E: clear ALL lines, then re-add all with target rate "
              f"{rate} -> {new_rate} (no amount sent)")
        c.update_record("opportunity", OPP_ID, {"item": {"items": []}},
                        params={"replace": "item"})
        print("  cleared. now:", [
            (l.get("line"), (l.get("item") or {}).get("refName"))
            for l in fetch(c)[1]
        ])
        items = [clean(l) for l in lines]
        for it in items:
            if str((it.get("item") or {}).get("refName")) == TARGET_ITEM:
                it["rate"] = new_rate
        apply(c, items)

    elif test == "f":
        new_rate = round(rate + 5.0, 2)
        want = round(qty * new_rate, 2)
        print(f"TEST F: target gets explicit amount={want} and NO rate "
              f"(others rebuilt with no rate either)")
        items = [clean(l) for l in lines]
        for it in items:
            it.pop("rate", None)
            if str((it.get("item") or {}).get("refName")) == TARGET_ITEM:
                it["amount"] = want
        apply(c, items)

    elif test == "restore":
        # Put the opportunity back to its original, internally-consistent
        # state: item 002 qty 1 rate 156 (so amount 156 == qty x rate again).
        want = {
            "EGTEST-ITEM-001": (2, 160.0),
            "EGTEST-ITEM-002": (1, 156.0),
            "EGTEST-ITEM-003": (1, 100.0),
        }
        print("TEST restore: setting qty/rate back to original values "
              "(dropping grossAmt so amounts recalculate)")
        items = []
        for ln in lines:
            it = clean(ln)
            for d in ("grossAmt", "quantityOnHand", "quantityAvailable"):
                it.pop(d, None)
            code = str((it.get("item") or {}).get("refName"))
            if code in want:
                q, r = want[code]
                it["quantity"] = q
                it["rate"] = r
            items.append(it)
        apply(c, items)

    elif test == "g":
        # Minimal payload: item + quantity + rate only. No custom fields.
        new_rate = round(rate + 5.0, 2)
        print(f"TEST G: MINIMAL payload (item/quantity/rate only), "
              f"target rate {rate} -> {new_rate}")
        items = []
        for ln in lines:
            it = {
                "item": ln.get("item"),
                "quantity": ln.get("quantity"),
                "rate": ln.get("rate"),
            }
            if str((it.get("item") or {}).get("refName")) == TARGET_ITEM:
                it["rate"] = new_rate
            items.append(it)
        apply(c, items)

    elif test == "h":
        # Bisect: minimal payload + ONE group of extra fields.
        group = sys.argv[2] if len(sys.argv) > 2 else "all"
        extras = {
            "po": lambda: {"custcol_po_rate": 120.0,
                           "custcol_po_vendor": {"id": "31398"}},
            "costest": lambda: {"costEstimateType": {"id": "PURCHORDERRATE"},
                                "costEstimateRate": 120.0,
                                "costEstimate": 120.0},
            "newitem": lambda: {"custcol_new_item_code": "TEST",
                                "custcol_new_item_brand": {"id": "811"}},
            "dept": lambda: {"department": {"id": "10"}},
            "saveflag": lambda: {"custcol_update_line_on_record_save": False},
        }
        chosen = list(extras) if group == "all" else group.split(",")
        new_rate = round(rate + 5.0, 2)
        print(f"TEST H[{group}]: minimal + {chosen}, "
              f"target rate {rate} -> {new_rate}")
        items = []
        for ln in lines:
            code = str((ln.get("item") or {}).get("refName"))
            it = {"item": ln.get("item"), "quantity": ln.get("quantity"),
                  "rate": ln.get("rate")}
            for g in chosen:
                if g not in extras:
                    continue
                base = extras[g]()
                for k, v in base.items():
                    if k == "custcol_new_item_code":
                        v = code
                    it[k] = v
            if code == TARGET_ITEM:
                it["rate"] = new_rate
            items.append(it)
        apply(c, items)

    elif test == "i":
        # Test echo-back of computed/read-only fields.
        group = sys.argv[2] if len(sys.argv) > 2 else "prod"
        new_rate = round(rate + 5.0, 2)
        print(f"TEST I[{group}]: target rate {rate} -> {new_rate}")
        items = []
        for ln in lines:
            code = str((ln.get("item") or {}).get("refName"))
            if group == "prod":
                # Exactly what production does: echo the whole line back.
                it = clean(ln)
            else:
                it = {"item": ln.get("item"), "quantity": ln.get("quantity"),
                      "rate": ln.get("rate")}
                if group == "gross":
                    it["grossAmt"] = ln.get("amount")
                elif group == "tax":
                    it["tax1Amt"] = ln.get("tax1Amt")
                    it["taxRate1"] = ln.get("taxRate1")
                elif group == "row":
                    it["price"] = ln.get("price")
                    it["rateSchedule"] = ln.get("rateSchedule")
                    it["itemType"] = ln.get("itemType")
                    it["marginal"] = ln.get("marginal")
                    it["printItems"] = ln.get("printItems")
            if code == TARGET_ITEM:
                it["rate"] = new_rate
            items.append(it)
        apply(c, items)

    elif test == "j":
        # Production behaviour (echo whole line) MINUS a drop-list.
        drop = (sys.argv[2] if len(sys.argv) > 2 else "").split(",")
        drop = [d for d in drop if d]
        new_rate = round(rate + 5.0, 2)
        print(f"TEST J: full echo minus {drop or '(nothing)'}, "
              f"target rate {rate} -> {new_rate}")
        items = []
        for ln in lines:
            it = clean(ln)                      # full echo, no line/amount
            for d in drop:
                it.pop(d, None)
            code = str((it.get("item") or {}).get("refName"))
            if code == TARGET_ITEM:
                it["rate"] = new_rate
            items.append(it)
        apply(c, items)

    else:
        return

    data2, lines2 = fetch(c)
    report(data2, lines2, f"AFTER TEST {test.upper()}")


if __name__ == "__main__":
    main()
