#!/usr/bin/env python3
"""Recount the MCP parity table in 2026-07-27-youtrack-mcp-parity.md.

The count line in that plan has been miscounted four times, each time by
editing the numbers in place or by a row heuristic that swept in the wrong
rows (including, once, this script's own first draft, which classified
`search_issues` as absent because its status cell contains the phrase "sort
still absent, deliberately"). Classify on the leading status glyph only, and
assert the total against the table rather than against the prose.

Run from the repo root:  python3 .claude/plans/recount-parity.py
"""
import re
import sys
from pathlib import Path

PLAN = Path(__file__).with_name("2026-07-27-youtrack-mcp-parity.md")


def main() -> int:
    doc = PLAN.read_text()
    start = doc.index("| MCP tool | CLI verb today | status |")
    block = doc[start:doc.index("\n\n", start)]
    rows = [r for r in block.splitlines()[2:] if r.startswith("|")]

    parity = absent = by_design = cli_only = 0
    unclassified = []
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        tool, status = cells[0], cells[-1]
        if tool == "—":                     # CLI-only verb, not a parity item
            cli_only += 1
        elif status.startswith("⬜"):
            absent += 1
        elif status.startswith("✅ covered"):
            by_design += 1
        elif status.startswith("✅"):
            parity += 1
        else:
            unclassified.append(row)

    mcp_rows = len(rows) - cli_only
    total = parity + absent + by_design
    print(f"table rows          : {len(rows)} ({cli_only} CLI-only, {mcp_rows} MCP)")
    print(f"at parity           : {parity}")
    print(f"absent              : {absent}")
    print(f"covered by design   : {by_design}")
    print(f"sum over MCP rows   : {total}")
    for row in unclassified:
        print(f"UNCLASSIFIED        : {row}")

    ok = True
    if unclassified:
        print("FAIL: every MCP row must classify on its status glyph")
        ok = False
    if mcp_rows != 23:
        print(f"FAIL: expected 23 MCP rows, found {mcp_rows}")
        ok = False
    if total != 23:
        print(f"FAIL: statuses sum to {total}, not 23")
        ok = False

    # The prose must agree with what was just counted.
    claim = re.search(r"(\d+) at parity, (\d+) near/partial, (\d+) absent", doc)
    if not claim:
        print("FAIL: could not find the count line in the plan")
        ok = False
    else:
        claimed = tuple(int(g) for g in claim.groups())
        counted = (parity, 0, absent)
        print(f"count line claims   : {claimed[0]} parity, {claimed[1]} near/partial, "
              f"{claimed[2]} absent")
        if claimed != counted:
            print(f"FAIL: count line says {claimed}, table says {counted} — "
                  "fix the line to match the table")
            ok = False

    print("OK" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
