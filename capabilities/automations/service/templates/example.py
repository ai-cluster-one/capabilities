#!/usr/bin/env python3
from __future__ import annotations

import json
import os


print(json.dumps({
    "ok": True,
    "automation": os.environ["AUTOMATION_ID"],
    "run_id": os.environ["AUTOMATION_RUN_ID"],
    "environment": os.environ["AUTOMATION_ENVIRONMENT"],
}))
