from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "bin" / "capabilities"


def _audit(name: str, script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(MANAGER), "audit", name, "--from", str(script)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )


def test_manager_accepts_deploy_descriptor_and_non_service_capability() -> None:
    service = _audit("automations", ROOT / "capabilities" / "automations" / "bin" / "automations")
    assert service.returncode == 0, service.stdout + service.stderr
    non_service = _audit("deployment", ROOT / "capabilities" / "deployment" / "bin" / "deployment")
    assert non_service.returncode == 0, non_service.stdout + non_service.stderr


def test_manager_rejects_unknown_deploy_schema(tmp_path: Path) -> None:
    bundle = tmp_path / "automations"
    shutil.copytree(ROOT / "capabilities" / "automations", bundle)
    script = bundle / "bin" / "automations"
    script.write_text(
        script.read_text().replace(
            '"schema": "capabilities.service.deploy.v1"',
            '"schema": "capabilities.service.deploy.v999"',
            1,
        )
    )
    proc = _audit("automations", script)
    assert proc.returncode == 7
    payload = json.loads(proc.stdout)
    assert any("service.deploy.schema" in failure for failure in payload["failures"])
