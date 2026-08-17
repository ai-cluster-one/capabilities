# Generated capability contract

Use this guide to understand how the canonical preamble is vendored, stamped into capability scripts, synchronized, and verified.

`contract/preamble.py` is vendored by `source init`. `capabilities new` stamps
its fenced regions, and `capabilities source sync <id>` deterministically
updates the authoring kit and every existing generated region.

`source check` and installation reject missing or changed regions. Generated
regions have one writer: the manager.
