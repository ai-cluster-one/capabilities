import sys
from pathlib import Path

# Put capabilities/slack/service/ on the import path for the daemon modules.
SERVICE_DIR = Path(__file__).resolve().parent.parent / "service"
sys.path.insert(0, str(SERVICE_DIR))
