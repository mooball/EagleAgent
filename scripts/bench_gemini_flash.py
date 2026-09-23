#!/usr/bin/env python
"""Quick throughput/latency comparison across Gemini Flash generations.

Answers one question: **for our real workloads, is any Flash generation
meaningfully faster than the others?**

Compares gemini-3.5/3.6/3.7/3.8-flash (plus an optional -lite baseline) on a
small set of prompts that mirror actual EagleAgent calls:

  group_parts      10 part descriptions -> NetSuite department (the real
                   `rfq_item_departments` prompt, graded against hand labels)
  tool_select      short "which tool?" decisions (4 cases)
  item_extract     messy customer email -> structured line items
  long_context     short task on top of config/prompts/procurement_agent.md
                   (~4k prompt tokens, closer to the real agent payload)

Per call it records wall latency, time-to-first-token, token counts
(prompt / output / *thinking*), effective throughput, validity of the answer,
and retries/errors.

Design notes
------------
* Calls are issued **strictly sequentially**. The repo's latency notes show
  Vertex 429s appear even at ~1.6 generateContent/min under contention, so
  concurrency would measure rate-limiting, not model speed.
* Every model runs at the same ``thinking_level``. The default is ``low``
  because it is the ONLY level all five models accept: 3.7-flash and 3.8-flash
  reject ``THINKING_LEVEL_MINIMAL`` with a 400. Support is probed up front and
  unsupported (model, level) pairs are skipped rather than counted as failures.
  Thinking tokens are billed as output and dominate latency, so the level is
  pinned rather than compared — otherwise you A/B thinking, not the model.
  Pass ``--thinking low,medium`` to sweep it as a second dimension.
* Streaming is the default so TTFT can be measured; ``--no-stream`` uses
  generate_content for a like-for-like comparison with the pipeline call sites.
* Cost needs real prices. There is no baked-in price table (they change and a
  wrong number is worse than none) — pass ``--price`` to compute it.

Usage
-----
    uv run python scripts/bench_gemini_flash.py --dry-run      # plan only
    uv run python scripts/bench_gemini_flash.py                # full run
    uv run python scripts/bench_gemini_flash.py --models gemini-3.5-flash,gemini-3.8-flash
    uv run python scripts/bench_gemini_flash.py --thinking minimal,low
    uv run python scripts/bench_gemini_flash.py --price gemini-3.8-flash=0.30/2.50
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from includes.netsuite.departments import department_prompt_table  # noqa: E402
from includes.prompts import load_prompt  # noqa: E402

DEFAULT_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]

# Reference point: the model local dev currently defaults to (see .env).
BASELINE_MODEL = "gemini-3.5-flash-lite"

# USD per 1M tokens (input, output) on the GLOBAL endpoint, standard on-demand.
# Source: cloud.google.com/vertex-ai/generative-ai/pricing, read 2026-09-23.
# Thinking tokens bill at the output rate ("output (response and reasoning)").
# NOTE: 3.6/3.7/3.8 are on introductory pricing ($0.75/$3.75) **through
# 2026-12-31**; from 2027-01-01 they move to $1.50/$7.50, which is where
# 3.5 Flash already sits. Enable with --use-known-prices, or override with
# --price model=IN/OUT.
KNOWN_PRICES = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.8-flash": (0.75, 3.75),
}

TRANSIENT_MARKERS = (
    "429",
    "503",
    "504",
    "DEADLINE_EXCEEDED",
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "overloaded",
)


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """One gradeable benchmark case: a prompt plus a validity check."""

    name: str
    prompt: str
    kind: str
    system: str | None = None
    validator: object = None  # Callable[[str], tuple[bool, str]]


def _strip_fences(text: str) -> str:
    """Drop ```json fences the models like to add despite instructions."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _parse_json(text: str):
    try:
        return json.loads(_strip_fences(text))
    except (json.JSONDecodeError, ValueError):
        return None


# ---- group_parts ---------------------------------------------------------

# Hand-labelled against the department descriptions. Ambiguity exists in the
# real task too; treat accuracy as indicative, not as gospel.
GROUP_ITEMS = [
    {"line": 1, "input_description": "Tyre 315/80R22.5 drive axle", "part_number": "MD-XZE2", "brand": "Michelin", "want": "7"},
    {"line": 2, "input_description": "Turbocharger assembly, Cummins ISX", "part_number": "798166-5001S", "brand": "Garrett", "want": "4"},
    {"line": 3, "input_description": "Front brake caliper, air operated", "part_number": "KN47001", "brand": "Knorr-Bremse", "want": "5"},
    {"line": 4, "input_description": "Excavator bucket tooth, 25mm pin", "part_number": "R210-TOOTH", "brand": "Hyundai", "want": "1"},
    {"line": 5, "input_description": "Forklift mast chain, 5000mm lift", "part_number": "8FG-CHAIN-5M", "brand": "Toyota", "want": "11"},
    {"line": 6, "input_description": "Transfer case output shaft, 79 series", "part_number": "TC-OS-79", "brand": "Landcruiser", "want": "9"},
    {"line": 7, "input_description": "Conveyor roller bearing, sealed", "part_number": "6205-2RS", "brand": "SKF", "want": "10"},
    {"line": 8, "input_description": "Cab door mirror assembly, left hand", "part_number": "T610-MIR-LH", "brand": "Kenworth", "want": "5"},
    {"line": 9, "input_description": "Yellow reflective warning decal", "part_number": "DEC-WARN-Y", "brand": "Generic", "want": "13"},
    {"line": 10, "input_description": "Radiator cap, 90kPa", "part_number": "RC-90-13", "brand": "Gates", "want": "4"},
]

_DEPT_IDS = {"1", "4", "5", "7", "8", "9", "10", "11", "13"}


def build_group_parts_prompt() -> str:
    """The production department-classification prompt, verbatim."""
    skill = load_prompt("rfq_item_departments").replace(
        "{{DEPARTMENT_TABLE}}", department_prompt_table()
    )
    payload = json.dumps(
        [
            {
                "line": it["line"],
                "input_description": it["input_description"],
                "part_number": it["part_number"],
                "brand": it["brand"],
            }
            for it in GROUP_ITEMS
        ],
        indent=2,
    )
    return (
        f"{skill}\n\n---\n\n## Items to classify\n\n"
        f"```json\n{payload}\n```\n\n"
        f"Return ONLY the JSON object specified in section 4."
    )


def validate_group_parts(text: str) -> tuple[bool, str]:
    data = _parse_json(text)
    if not isinstance(data, dict):
        return False, "not a JSON object"
    assignments = data.get("departments")
    if not isinstance(assignments, dict):
        return False, "no 'departments' object"

    wanted = {str(it["line"]): it["want"] for it in GROUP_ITEMS}
    bad_ids = {k: v for k, v in assignments.items() if str(v).strip() not in _DEPT_IDS}
    unknown_lines = [k for k in assignments if k not in wanted]

    correct = sum(
        1 for k, v in assignments.items() if wanted.get(k) == str(v).strip()
    )
    coverage = len(assignments)
    ok = not bad_ids and not unknown_lines and len(assignments) > 0
    note = f"{correct}/{len(wanted)} correct, {coverage} assigned"
    if bad_ids:
        note += f", bad ids {bad_ids}"
    if unknown_lines:
        note += f", unknown lines {unknown_lines}"
    return ok, note


# ---- tool_select ---------------------------------------------------------

TOOL_CATALOGUE = [
    ("search_products", "Search the internal product database."),
    ("search_brands", "Look up brands/manufacturers."),
    ("search_suppliers", "Search the internal supplier database."),
    ("part_purchase_history", "Purchase history for a single part number."),
    ("search_purchase_history", "Search purchase history across parts/suppliers."),
    ("part_sale_history_batch", "Batch sale history for part numbers."),
    ("convert_currency", "Convert an amount between currencies."),
    ("get_rfq", "Fetch an RFQ and its line items."),
    ("manage_rfq", "Create/update an RFQ, line items, supplier links."),
    ("find_previous_suppliers", "Find suppliers previously used for an RFQ's items."),
    ("search_suppliers_web", "Web-search for suppliers for an RFQ."),
    ("classify_rfq_items", "Classify RFQ items and match them to products."),
    ("validate_rfq_items", "Web-validate item part numbers for discrepancies."),
    ("create_quote", "Create a supplier quote."),
    ("run_script", "Run a whitelisted server-side maintenance script."),
    ("delete_records", "Delete records from the database (admin only)."),
]

# (utterance, acceptable tool names)
TOOL_CASES = [
    ("find me suppliers for the tyre items on RFQ-2026-1040", {"find_previous_suppliers", "search_suppliers_web", "search_suppliers"}),
    ("what did we last pay for part BPW-1234?", {"part_purchase_history", "search_purchase_history"}),
    ("add sydney tools as a supplier to line 1 of RFQ-2026-1054", {"manage_rfq"}),
    ("convert 4500 EUR to AUD", {"convert_currency"}),
]


def build_tool_select_prompt(utterance: str) -> str:
    catalogue = "\n".join(f"- {n}: {d}" for n, d in TOOL_CATALOGUE)
    return (
        "You are a tool-selection router. Choose the SINGLE tool that best "
        "handles the user's request.\n\n"
        f"Available tools:\n{catalogue}\n\n"
        f'User request: "{utterance}"\n\n'
        'Reply with ONLY a JSON object: {"tool": "<name>", "args": {...}}'
    )


def _make_tool_validator(allowed: set[str]):
    valid_names = {n for n, _ in TOOL_CATALOGUE}

    def _validate(text: str) -> tuple[bool, str]:
        data = _parse_json(text)
        if not isinstance(data, dict):
            return False, "not a JSON object"
        tool = str(data.get("tool", "")).strip()
        if tool not in valid_names:
            return False, f"unknown tool '{tool}'"
        return tool in allowed, f"chose {tool}"

    return _validate


# ---- item_extract --------------------------------------------------------

EXTRACT_EMAIL = """\
From: Dave Hutchings <dave@nqearthmoving.com.au>
Subject: re: re: urgent - parts list

g'day

need pricing on the following asap, machine is down

- 2x 6I-2504 injector, cat, been getting them from you before
- 1 x hydraulic hose 1/2" 3000psi x 2m long (no part no sorry)
- 3 of the filters for the 320 excavator, think its 1R-0750?
- also need a price on a set of tracks for a PC300-8 komatsu
- 4 x 6Y-3398 seal kit

also can you tell me if you stock the 175-9738 water pump

cheers dave
0408 123 456
"""


def build_item_extract_prompt() -> str:
    return (
        "Extract the requested line items from this customer email.\n\n"
        "Return ONLY JSON: {\"items\": [{\"description\": str, "
        "\"part_number\": str|null, \"brand\": str|null, \"qty\": int}]}\n\n"
        f"--- EMAIL ---\n{EXTRACT_EMAIL}"
    )


def validate_item_extract(text: str) -> tuple[bool, str]:
    data = _parse_json(text)
    if not isinstance(data, dict):
        return False, "not a JSON object"
    items = data.get("items")
    if not isinstance(items, list):
        return False, "no 'items' array"
    bad = [i for i, it in enumerate(items) if not isinstance(it, dict) or not str(it.get("description", "")).strip()]
    ok = len(items) >= 4 and not bad
    note = f"{len(items)} items" + (f", {len(bad)} malformed" if bad else "")
    return ok, note


# ---- task registry -------------------------------------------------------


def build_tasks() -> dict[str, Task]:
    tasks: dict[str, Task] = {
        "group_parts": Task(
            name="group_parts",
            prompt=build_group_parts_prompt(),
            kind="grouping",
            validator=validate_group_parts,
        ),
        "item_extract": Task(
            name="item_extract",
            prompt=build_item_extract_prompt(),
            kind="extraction",
            validator=validate_item_extract,
        ),
    }

    for idx, (utterance, allowed) in enumerate(TOOL_CASES, start=1):
        tasks[f"tool_select_{idx}"] = Task(
            name=f"tool_select_{idx}",
            prompt=build_tool_select_prompt(utterance),
            kind="routing",
            validator=_make_tool_validator(allowed),
        )

    # ~4k-token prompt: closest thing here to a real agent call.
    agent_prompt = load_prompt("procurement_agent")
    long_utterance = "what did we last pay for part BPW-1234?"
    tasks["long_context"] = Task(
        name="long_context",
        prompt=build_tool_select_prompt(long_utterance),
        kind="routing",
        system=agent_prompt,
        validator=_make_tool_validator({"part_purchase_history", "search_purchase_history"}),
    )
    return tasks


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class Result:
    model: str
    thinking: str
    task: str
    run: int
    ok: bool
    wall_s: float
    ttft_s: float | None
    prompt_tokens: int | None
    output_tokens: int | None
    thought_tokens: int | None
    total_tokens: int | None
    valid: bool | None
    note: str
    retries: int = 0
    error: str | None = None
    out_chars: int = 0

    @property
    def overall_tokens_per_s(self) -> float | None:
        """Output tokens / full latency. Stable and comparable across models."""
        if not self.output_tokens or self.wall_s <= 0:
            return None
        return self.output_tokens / self.wall_s

    @property
    def gen_tokens_per_s(self) -> float | None:
        """Output tokens / time spent streaming after the first token.

        Only meaningful when there is a real generation window. Some Gemini
        responses arrive almost entirely after a long prefill (TTFT ~= wall),
        which would otherwise report absurd rates of thousands of tok/s.
        """
        if not self.output_tokens or self.ttft_s is None:
            return None
        window = self.wall_s - self.ttft_s
        if window < 0.25:
            return None
        return self.output_tokens / window

    @property
    def billed_output_tokens(self) -> int | None:
        """Thinking tokens bill at the output rate."""
        if self.output_tokens is None:
            return None
        return self.output_tokens + (self.thought_tokens or 0)


def _usage_from(response) -> dict:
    um = getattr(response, "usage_metadata", None)
    if not um:
        return {}
    return {
        "prompt_tokens": getattr(um, "prompt_token_count", None),
        "output_tokens": getattr(um, "candidates_token_count", None),
        "thought_tokens": getattr(um, "thoughts_token_count", None),
        "total_tokens": getattr(um, "total_token_count", None),
    }


def _is_transient(err: Exception) -> bool:
    return any(m.lower() in str(err).lower() for m in TRANSIENT_MARKERS)


def run_once(
    client,
    model: str,
    task: Task,
    thinking: str,
    stream: bool,
    temperature: float,
    max_output_tokens: int,
    timeout_ms: int,
) -> Result:
    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        thinking_config=types.ThinkingConfig(thinking_level=thinking),
        http_options=types.HttpOptions(timeout=timeout_ms),
    )
    if task.system:
        config.system_instruction = task.system

    last_err = None
    for attempt in range(3):
        t0 = time.perf_counter()
        try:
            text, ttft, usage = _invoke(
                client, model, task.prompt, config, stream
            )
            wall = time.perf_counter() - t0
            valid, note = (True, "")
            if task.validator:
                valid, note = task.validator(text)
            return Result(
                model=model,
                thinking=thinking,
                task=task.name,
                run=-1,
                ok=True,
                wall_s=wall,
                ttft_s=ttft,
                valid=valid,
                note=note,
                retries=attempt,
                out_chars=len(text),
                **_pick(usage),
            )
        except Exception as e:  # noqa: BLE001 - benchmark must not crash
            last_err = e
            if not _is_transient(e) or attempt == 2:
                break
            time.sleep(2**attempt)

    return Result(
        model=model,
        thinking=thinking,
        task=task.name,
        run=-1,
        ok=False,
        wall_s=0.0,
        ttft_s=None,
        prompt_tokens=None,
        output_tokens=None,
        thought_tokens=None,
        total_tokens=None,
        valid=None,
        note="",
        error=f"{type(last_err).__name__}: {str(last_err)[:200]}",
    )


def _pick(usage: dict) -> dict:
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thought_tokens": usage.get("thought_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _invoke(client, model, prompt, config, stream):
    """Return (text, ttft_seconds_or_None, usage_dict)."""
    if not stream:
        t0 = time.perf_counter()
        resp = client.models.generate_content(
            model=model, contents=prompt, config=config
        )
        return resp.text or "", None, _usage_from(resp)

    t0 = time.perf_counter()
    ttft = None
    parts: list[str] = []
    usage: dict = {}
    for chunk in client.models.generate_content_stream(
        model=model, contents=prompt, config=config
    ):
        if ttft is None:
            ttft = time.perf_counter() - t0
        try:
            if chunk.text:
                parts.append(chunk.text)
        except Exception:  # noqa: BLE001 - chunks can be thought-only
            pass
        if getattr(chunk, "usage_metadata", None):
            usage = _usage_from(chunk)
    return "".join(parts), ttft, usage


def probe_thinking_support(
    client, model: str, levels: list[str], timeout_ms: int
) -> dict[str, str]:
    """Return {level: 'ok'} or {level: reason it cannot be used}.

    Model generations do not share a thinking-level vocabulary: 3.7-flash and
    3.8-flash reject THINKING_LEVEL_MINIMAL outright with a 400. Probing first
    keeps that from looking like a model failure in the results.
    """
    support: dict[str, str] = {}
    for level in levels:
        try:
            client.models.generate_content(
                model=model,
                contents="Reply with the single word: ok",
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=16,
                    thinking_config=types.ThinkingConfig(thinking_level=level),
                    http_options=types.HttpOptions(timeout=timeout_ms),
                ),
            )
            support[level] = "ok"
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            support[level] = (
                "unsupported" if "unsupported" in msg.lower() else f"error: {msg[:80]}"
            )
    return support


# ---------------------------------------------------------------------------
# Aggregation / reporting
# ---------------------------------------------------------------------------


def _p90(values: list[float]) -> float:
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    idx = max(0, int(round(0.9 * (len(ordered) - 1))))
    return ordered[idx]


def summarise(results: list[Result], prices: dict[str, tuple[float, float]]):
    good = [r for r in results if r.ok]
    groups: dict[tuple[str, str, str], list[Result]] = {}
    for r in good:
        groups.setdefault((r.model, r.thinking, r.task), []).append(r)

    rows = []
    for (model, thinking, task), rs in sorted(groups.items()):
        walls = [r.wall_s for r in rs]
        ttfts = [r.ttft_s for r in rs if r.ttft_s is not None]
        overall = [r.overall_tokens_per_s for r in rs if r.overall_tokens_per_s]
        gens = [r.gen_tokens_per_s for r in rs if r.gen_tokens_per_s]
        outs = [r.output_tokens for r in rs if r.output_tokens is not None]
        thoughts = [r.thought_tokens or 0 for r in rs]
        prompts = [r.prompt_tokens or 0 for r in rs]
        valids = [r.valid for r in rs if r.valid is not None]
        rows.append(
            {
                "model": model,
                "thinking": thinking,
                "task": task,
                "n": len(rs),
                "wall_med": statistics.median(walls),
                "wall_p90": _p90(walls),
                "ttft_med": statistics.median(ttfts) if ttfts else None,
                "tps": statistics.median(overall) if overall else None,
                "gen_tps": statistics.median(gens) if gens else None,
                "out_tok": statistics.median(outs) if outs else None,
                "think_tok": statistics.median(thoughts) if thoughts else 0,
                "prompt_tok": statistics.median(prompts) if prompts else 0,
                "valid_pct": (100.0 * sum(1 for v in valids if v) / len(valids)) if valids else None,
            }
        )
    return rows


def short(model: str) -> str:
    """gemini-3.5-flash-lite -> 3.5-flash-lite (keeps tables terminal-width)."""
    return model.replace("gemini-", "")


def _fmt(v, spec=".2f", dash="—"):
    return format(v, spec) if isinstance(v, (int, float)) else dash


def print_report(results: list[Result], rows: list[dict], prices: dict[str, tuple[float, float]]):
    failures = [r for r in results if not r.ok]
    print()
    print("PER-TASK RESULTS  (median over runs; t/s = output tokens / latency)")
    header = (
        f"{'model':<16}{'think':<8}{'task':<15}{'n':>3}{'wall':>7}"
        f"{'p90':>7}{'ttft':>7}{'t/s':>7}{'out':>6}{'think':>7}{'valid%':>7}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{short(r['model']):<16}{r['thinking']:<8}{r['task']:<15}{r['n']:>3}"
            f"{_fmt(r['wall_med']):>7}{_fmt(r['wall_p90']):>7}"
            f"{_fmt(r['ttft_med']):>7}{_fmt(r['tps'], '.1f'):>7}"
            f"{_fmt(r['out_tok'], '.0f'):>6}{_fmt(r['think_tok'], '.0f'):>7}"
            f"{_fmt(r['valid_pct'], '.0f'):>7}"
        )

    # Roll up per model.
    per_model: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        per_model.setdefault((r["model"], r["thinking"]), []).append(r)

    print()
    print("PER-MODEL ROLL-UP  (unweighted mean across tasks)")
    header2 = (
        f"{'model':<16}{'think':<8}{'tasks':>6}{'wall':>7}{'ttft':>7}"
        f"{'t/s':>7}{'out':>6}{'in':>6}{'$/1k':>10}"
    )
    print(header2)
    print("-" * len(header2))
    for (model, thinking), rs in sorted(per_model.items()):
        walls = [r["wall_med"] for r in rs if r["wall_med"] is not None]
        ttfts = [r["ttft_med"] for r in rs if r["ttft_med"] is not None]
        tpss = [r["tps"] for r in rs if r["tps"]]
        outs = [r["out_tok"] for r in rs if r["out_tok"]]
        prompts = [r["prompt_tok"] for r in rs if r["prompt_tok"]]
        cost_cell = "—"
        if model in prices:
            in_p, out_p = prices[model]
            avg_in = statistics.mean(prompts) if prompts else 0
            avg_out = statistics.mean(outs) if outs else 0
            avg_think = statistics.mean([r["think_tok"] for r in rs])
            per_call = (avg_in * in_p + (avg_out + avg_think) * out_p) / 1_000_000
            cost_cell = f"{per_call * 1000:.4f}"
        print(
            f"{short(model):<16}{thinking:<8}{len(rs):>6}"
            f"{_fmt(statistics.mean(walls) if walls else None):>7}"
            f"{_fmt(statistics.mean(ttfts) if ttfts else None):>7}"
            f"{_fmt(statistics.mean(tpss) if tpss else None, '.1f'):>7}"
            f"{_fmt(statistics.mean(outs) if outs else None, '.0f'):>6}"
            f"{_fmt(statistics.mean(prompts) if prompts else None, '.0f'):>6}"
            f"{cost_cell:>10}"
        )

    if not prices:
        print("\n  $/1k: no prices given — pass --price model=IN/OUT (USD per 1M tokens).")

    if failures:
        print()
        print(f"FAILED CALLS: {len(failures)}")
        kinds: dict[str, int] = {}
        for f in failures:
            key = (f.error or "unknown").split(":")[0]
            kinds[key] = kinds.get(key, 0) + 1
        for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]):
            print(f"  {v:>3}x {k}")
        sample = failures[0]
        print(f"  e.g. {sample.model} / {sample.task}: {sample.error}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_prices(items: list[str]) -> dict[str, tuple[float, float]]:
    prices: dict[str, tuple[float, float]] = {}
    for item in items or []:
        if "=" not in item or "/" not in item:
            raise SystemExit(f"--price must look like model=IN/OUT, got {item!r}")
        model, rates = item.split("=", 1)
        inp, out = rates.split("/", 1)
        prices[model.strip()] = (float(inp), float(out))
    return prices


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Benchmark Gemini Flash generations on EagleAgent-like prompts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--models", default=None, help="comma-separated model ids")
    ap.add_argument("--baseline", action="store_true", help=f"also run {BASELINE_MODEL}")
    ap.add_argument("--thinking", default="low", help="comma-separated thinking levels (low is the only level all 5 models accept)")
    ap.add_argument("--tasks", default=None, help="comma-separated task names")
    ap.add_argument("--runs", type=int, default=3, help="timed runs per combination")
    ap.add_argument("--warmup", type=int, default=1, help="discarded warm-up runs")
    ap.add_argument("--no-stream", action="store_true", help="use generate_content instead of streaming")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-output-tokens", type=int, default=4096)
    ap.add_argument("--timeout", type=int, default=120, help="per-request timeout, seconds")
    ap.add_argument("--sleep", type=float, default=1.0, help="pause between calls, seconds")
    ap.add_argument("--price", action="append", default=[], help="model=IN/OUT (USD per 1M tokens)")
    ap.add_argument("--use-known-prices", action="store_true",
                    help="seed prices from the KNOWN_PRICES table (Vertex global, 2026-09)")
    ap.add_argument("--from-json", default=None,
                    help="skip all API calls; re-render the report from a --json-out file")
    ap.add_argument("--json-out", default=None, help="write raw results to this path")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = ap.parse_args()

    models = [m.strip() for m in (args.models.split(",") if args.models else DEFAULT_MODELS)]
    if args.baseline and BASELINE_MODEL not in models:
        models.append(BASELINE_MODEL)
    thinkings = [t.strip() for t in args.thinking.split(",") if t.strip()]
    prices = parse_prices(args.price)

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
        results = [_result_from_dict(d) for d in payload["results"]]
        prices = prices or (
            dict(KNOWN_PRICES) if args.use_known_prices
            else {k: tuple(v) for k, v in payload.get("prices_usd_per_1m", {}).items()}
        )
        print(f"Re-reporting {len(results)} calls from {args.from_json}")
        skipped = {
            tuple(k.split("|")): v
            for k, v in (payload.get("skipped_unsupported") or {}).items()
        }
        print_report(results, summarise(results, prices), prices)
        if skipped:
            print("\nSKIPPED COMBINATIONS (not supported by the model)")
            for (m, lv), why in sorted(skipped.items()):
                print(f"  {short(m):<16}{lv:<8}{why}")
        return 0

    all_tasks = build_tasks()
    task_names = (
        [t.strip() for t in args.tasks.split(",") if t.strip()]
        if args.tasks
        else list(all_tasks)
    )
    unknown = [t for t in task_names if t not in all_tasks]
    if unknown:
        raise SystemExit(f"unknown task(s): {unknown}\navailable: {list(all_tasks)}")

    call_count = len(models) * len(thinkings) * len(task_names) * (args.runs + args.warmup)

    if args.dry_run:
        print("Models:     ", ", ".join(models))
        print("Thinking:   ", ", ".join(thinkings))
        print("Tasks:      ", ", ".join(task_names))
        print(f"Runs:        {args.runs} timed + {args.warmup} warm-up (warm-up discarded)")
        print(f"Total calls: {call_count}  (sequential, {args.sleep}s apart)")
        print(f"Streaming:   {not args.no_stream}")
        print()
        for name in task_names:
            t = all_tasks[name]
            prompt_tokens_guess = len(t.prompt) // 4
            syslen = len(t.system or "") // 4
            print(f"  {name:<16} ~{prompt_tokens_guess:>5} prompt tokens"
                  f"{f' + ~{syslen} system' if syslen else ''}  [{t.kind}]")
        return 0

    client = genai.Client()
    results: list[Result] = []
    started = datetime.now(timezone.utc)

    print(f"Benchmarking {len(models)} model(s) x {len(thinkings)} thinking level(s) "
          f"x {len(task_names)} task(s) = {call_count} calls (sequential)")
    print(f"Project: {os.getenv('GOOGLE_CLOUD_PROJECT', '?')}  "
          f"Location: {os.getenv('GOOGLE_CLOUD_LOCATION', '?')}")
    if args.use_known_prices:
        prices = {**KNOWN_PRICES, **prices}
        print(f"Using KNOWN_PRICES (Vertex global, 2026-09) for {len(prices)} model(s)")
    print()

    # Probe which thinking levels each model accepts before spending calls.
    probe_timeout = min(args.timeout, 60) * 1000
    support: dict[tuple[str, str], str] = {}
    for model in models:
        for level in thinkings:
            support[(model, level)] = probe_thinking_support(
                client, model, [level], probe_timeout
            )[level]

    skipped = {k: v for k, v in support.items() if v != "ok"}
    if skipped:
        print("Pre-flight: " + "; ".join(
            f"{short(m)}/{lv} -> {why}" for (m, lv), why in sorted(skipped.items())
        ))
        print()

    runnable = len([1 for k in support if support[k] == "ok"])
    call_count = runnable * len(task_names) * (args.runs + args.warmup)

    done = 0
    for model in models:
        for thinking in thinkings:
            if support[(model, thinking)] != "ok":
                print(f"-- skipping {model} / {thinking} "
                      f"({support[(model, thinking)]})")
                continue
            for name in task_names:
                task = all_tasks[name]
                for i in range(args.runs + args.warmup):
                    is_warmup = i < args.warmup
                    run_idx = i - args.warmup
                    res = run_once(
                        client,
                        model=model,
                        task=task,
                        thinking=thinking,
                        stream=not args.no_stream,
                        temperature=args.temperature,
                        max_output_tokens=args.max_output_tokens,
                        timeout_ms=args.timeout * 1000,
                    )
                    done += 1
                    status = "ok" if res.ok else "FAIL"
                    detail = res.note or (res.error or "")[:60]
                    flag = "" if is_warmup or res.ok else "  <-- "
                    print(
                        f"[{done:>3}/{call_count}] {model:<24} {thinking:<8} "
                        f"{name:<16} {status:<4} {res.wall_s:>6.2f}s "
                        f"{'warm' if is_warmup else f'run{run_idx + 1}'} "
                        f"{detail}{flag}",
                        flush=True,
                    )
                    if not is_warmup:
                        res.run = run_idx
                        results.append(res)
                    time.sleep(args.sleep)

    rows = summarise(results, prices)
    print_report(results, rows, prices)

    if skipped:
        print("\nSKIPPED COMBINATIONS (not supported by the model)")
        for (m, lv), why in sorted(skipped.items()):
            print(f"  {short(m):<16}{lv:<8}{why}")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\nTotal wall time: {elapsed:.0f}s")

    if args.json_out:
        out = Path(args.json_out)
        payload = {
            "started": started.isoformat(),
            "elapsed_s": elapsed,
            "models": models,
            "thinking": thinkings,
            "tasks": task_names,
            "runs": args.runs,
            "warmup": args.warmup,
            "stream": not args.no_stream,
            "temperature": args.temperature,
            "project": os.getenv("GOOGLE_CLOUD_PROJECT"),
            "location": os.getenv("GOOGLE_CLOUD_LOCATION"),
            "prices_usd_per_1m": {k: list(v) for k, v in prices.items()},
            "skipped_unsupported": {
                f"{m}|{lv}": why for (m, lv), why in sorted(skipped.items())
            },
            "results": [_result_dict(r) for r in results],
            "summary": rows,
        }
        out.write_text(json.dumps(payload, indent=2))
        print(f"Raw results written to {out}")

    return 0


def _result_dict(r: Result) -> dict:
    d = {
        "model": r.model,
        "thinking": r.thinking,
        "task": r.task,
        "run": r.run,
        "ok": r.ok,
        "wall_s": round(r.wall_s, 4),
        "ttft_s": round(r.ttft_s, 4) if r.ttft_s is not None else None,
        "prompt_tokens": r.prompt_tokens,
        "output_tokens": r.output_tokens,
        "thought_tokens": r.thought_tokens,
        "total_tokens": r.total_tokens,
        "billed_output_tokens": r.billed_output_tokens,
        "tokens_per_s": round(r.overall_tokens_per_s, 2) if r.overall_tokens_per_s else None,
        "gen_tokens_per_s": round(r.gen_tokens_per_s, 2) if r.gen_tokens_per_s else None,
        "valid": r.valid,
        "note": r.note,
        "retries": r.retries,
        "error": r.error,
        "out_chars": r.out_chars,
    }
    return d


def _result_from_dict(d: dict) -> Result:
    """Rebuild a Result from a --json-out payload (for offline re-reporting)."""
    return Result(
        model=d["model"],
        thinking=d["thinking"],
        task=d["task"],
        run=d.get("run", -1),
        ok=d["ok"],
        wall_s=d.get("wall_s") or 0.0,
        ttft_s=d.get("ttft_s"),
        prompt_tokens=d.get("prompt_tokens"),
        output_tokens=d.get("output_tokens"),
        thought_tokens=d.get("thought_tokens"),
        total_tokens=d.get("total_tokens"),
        valid=d.get("valid"),
        note=d.get("note", ""),
        retries=d.get("retries", 0),
        error=d.get("error"),
        out_chars=d.get("out_chars", 0),
    )


if __name__ == "__main__":
    raise SystemExit(main())
