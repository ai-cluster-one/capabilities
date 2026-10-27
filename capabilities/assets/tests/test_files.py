"""Files that are not markdown: kept in an S3-compatible bucket with a record
in the store, reached by `files add|get|list`, carried by import and export
byte for byte, proven by doctor, and gated as writes. Without a file store
connection the documents verbs work as before.

The bucket is a local S3-compatible gateway that verifies every signature:
ASSETS_TEST_VERSITYGW names a versitygw binary (else `versitygw` on PATH);
without one the bucket cases skip, as every case skips without
ASSETS_TEST_DSN."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from _cli import connect, error, needs_store, world  # noqa: F401

pytestmark = needs_store

GATEWAY = os.environ.get("ASSETS_TEST_VERSITYGW") or shutil.which("versitygw")
needs_gateway = pytest.mark.skipif(
    not GATEWAY, reason="ASSETS_TEST_VERSITYGW names no S3-compatible gateway binary")

ACCESS = "AKIDEXAMPLE0001"
SECRET = "wJalr/EXAMPLE+secret=key"
BUCKET = "example-bucket"


@pytest.fixture
def gateway(tmp_path: Path):
    root = tmp_path / "gateway"
    (root / BUCKET).mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen([GATEWAY, "--access", ACCESS, "--secret", SECRET,
                             "--port", f"127.0.0.1:{port}", "posix", str(root)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise RuntimeError("the S3 gateway did not start")
                time.sleep(0.1)
        yield SimpleNamespace(endpoint=f"http://127.0.0.1:{port}", bucket=root / BUCKET)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def connected(root: Path, gateway, secret: str = SECRET, **entry) -> Path:
    """Declare the file store connection `files` in the project, its keys in .env.local."""
    spec = {"endpoint": gateway.endpoint, "region": "us-east-1", "bucket": BUCKET,
            "access_key_id_env": "ASSETS_FILES_ACCESS_KEY_ID",
            "secret_access_key_env": "ASSETS_FILES_SECRET_ACCESS_KEY", **entry}
    (root / "capabilities" / "assets").mkdir(parents=True, exist_ok=True)
    (root / "capabilities" / "assets" / "connections.json").write_text(
        json.dumps({"default": "files", "connections": {"files": spec}}))
    (root / ".env.local").write_text(f"ASSETS_FILES_ACCESS_KEY_ID={ACCESS}\n"
                                     f"ASSETS_FILES_SECRET_ACCESS_KEY='{secret}'\n")
    return root


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452") + bytes(range(256)) * 4


@needs_gateway
def test_a_file_is_bytes_in_the_bucket_and_a_record_in_the_store(world, gateway, tmp_path):  # noqa: F811
    root = connected(world.project(), gateway)
    first = world.ok(root, "files", "add", "research/x/crash é.ips", "--from", "-", stdin=PNG)
    row = first["file"]
    assert first["outcome"] == "created" and first["replaced"] is None
    assert row["bytes"] == len(PNG) and row["sha256"] == sha(PNG)
    assert row["storage_key"] == f"{json.loads((root / 'capabilities' / 'project.json').read_text())['id']}/{row['id']}/research/x/crash é.ips"
    assert (gateway.bucket / row["storage_key"]).read_bytes() == PNG
    listed = world.ok(root, "files", "list")
    assert listed["count"] == 1 and listed["files"][0]["id"] == row["id"]
    out = tmp_path / "out" / "crash.ips"
    got = world.ok(root, "files", "get", "research/x/crash é.ips", "--out", str(out))
    assert got["outcome"] == "written" and out.read_bytes() == PNG
    assert world.ok(root, "files", "get", "research/x/crash é.ips", "--out",
                    str(out))["outcome"] == "unchanged"
    raw = world.run(root, "files", "get", "research/x/crash é.ips", "--out", "-")
    assert raw.returncode == 0 and raw.stdout == PNG
    again = world.ok(root, "files", "add", "research/x/crash é.ips", "--from", "-", stdin=PNG)
    assert again["outcome"] == "unchanged" and again["file"]["id"] == row["id"]
    changed = world.ok(root, "files", "add", "research/x/crash é.ips", "--from", "-",
                       stdin=PNG + b"!")
    assert changed["outcome"] == "replaced" and changed["replaced"] == row["id"]
    assert changed["file"]["storage_key"] != row["storage_key"]
    assert (gateway.bucket / row["storage_key"]).read_bytes() == PNG
    with connect(world.schema) as conn:
        rows = conn.execute("SELECT id::text, retired_at IS NOT NULL FROM assets_files "
                            "ORDER BY created_at").fetchall()
    assert rows == [(row["id"], True), (changed["file"]["id"], False)]
    assert world.ok(root, "files", "list")["files"][0]["sha256"] == sha(PNG + b"!")
    md = world.run(root, "files", "add", "plans/p.md", "--from", "-", stdin=b"x")
    assert md.returncode == 6 and error(md)["code"] == "bad_name"


@needs_gateway
def test_a_read_refuses_bytes_the_record_does_not_hold(world, gateway, tmp_path):  # noqa: F811
    root = connected(world.project(), gateway)
    row = world.ok(root, "files", "add", "logs/run.log", "--from", "-", stdin=b"line\n")["file"]
    (gateway.bucket / row["storage_key"]).write_bytes(b"tampered\n")
    out = tmp_path / "run.log"
    got = world.run(root, "files", "get", "logs/run.log", "--out", str(out))
    assert got.returncode == 8 and error(got)["code"] == "sha256_mismatch"
    assert not out.exists()
    exported = world.run(root, "export", str(tmp_path / "exported"))
    assert exported.returncode == 8
    report = json.loads(exported.stdout)
    assert report["files"][0]["outcome"] == "failed"
    assert report["files"][0]["code"] == "sha256_mismatch" and not report["complete"]
    assert not (tmp_path / "exported" / "logs" / "run.log").exists()
    (gateway.bucket / row["storage_key"]).unlink()
    gone = world.run(root, "files", "get", "logs/run.log", "--out", str(out))
    assert gone.returncode == 8 and error(gone)["code"] == "object_missing"


def _mixed_folder(base: Path) -> dict[str, bytes]:
    files = {
        "research/topic/analysis.md": b"# Analysis\n\n![chart](chart.png)\n[run](../../logs/run.log)\n",
        "research/topic/chart.png": PNG,
        "research/topic/data.json": b'{"a": 1}\n',
        "logs/run.log": b"started\nstopped\n",
        "plans/rollout.md": b"# Rollout\n",
        "plans/rollout/diagram.svg": b"<svg/>\n",
        "plans/.gitkeep": b"",
        "research/topic/.DS_Store": b"\x00\x00\x00\x01Bud1",
        "top.bin": bytes(range(256)),
    }
    for rel, data in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return files


def _tree(base: Path) -> dict[str, bytes]:
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}


@needs_gateway
def test_import_and_export_carry_every_file_byte_for_byte(world, gateway, tmp_path):  # noqa: F811
    root = connected(world.project(), gateway)
    source = _mixed_folder(root / "assets")
    imported = world.ok(root, "import")
    litter = {"plans/.gitkeep", "research/topic/.DS_Store"}
    assert {r["path"] for r in imported["skipped"]} == litter
    for rel in litter:
        del source[rel]
    assert {r["path"]: r["outcome"] for r in imported["documents"]} == {
        "research/topic/analysis.md": "created", "plans/rollout.md": "created"}
    kept = {r["name"]: r for r in imported["files"]}
    assert set(kept) == {rel for rel in source if not rel.endswith(".md")}
    assert {r["outcome"] for r in kept.values()} == {"created"}
    assert kept["research/topic/chart.png"]["document"] == "research/topic/analysis"
    assert kept["logs/run.log"]["document"] == "research/topic/analysis"
    assert kept["research/topic/data.json"]["document"] == "research/topic/analysis"
    assert kept["plans/rollout/diagram.svg"]["document"] == "plans/rollout"
    assert kept["top.bin"]["document"] is None
    again = world.ok(root, "import")
    assert {r["outcome"] for r in again["documents"] + again["files"]} == {"unchanged"}
    (root / "assets" / "logs" / "run.log").write_bytes(b"started\nfailed\n")
    source["logs/run.log"] = b"started\nfailed\n"
    changed = {r["name"]: r["outcome"] for r in world.ok(root, "import")["files"]}
    assert changed.pop("logs/run.log") == "replaced"
    assert set(changed.values()) == {"unchanged"}
    out = tmp_path / "exported"
    exported = world.ok(root, "export", str(out))
    assert exported["complete"] and exported["file_counts"]["written"] == 5
    assert _tree(out) == source
    (out / "top.bin").write_bytes(b"local edit")
    repeat = world.ok(root, "export", str(out))
    outcomes = {r["name"]: r["outcome"] for r in repeat["files"]}
    assert outcomes.pop("top.bin") == "kept" and set(outcomes.values()) == {"unchanged"}
    assert not repeat["complete"] and (out / "top.bin").read_bytes() == b"local edit"


@needs_gateway
def test_a_file_kept_without_a_document_is_linked_to_one_after(world, gateway, tmp_path):  # noqa: F811
    root = connected(world.project(), gateway)
    (root / "assets" / "research" / "x-sources").mkdir(parents=True)
    (root / "assets" / "research" / "x.md").write_text("# X\n")
    (root / "assets" / "research" / "y.md").write_text("# Y\n")
    (root / "assets" / "research" / "x-sources" / "crash.ips").write_bytes(PNG)
    kept = world.ok(root, "import")["files"][0]
    assert kept["name"] == "research/x-sources/crash.ips" and kept["document"] is None
    linked = world.ok(root, "files", "link", "research/x-sources/crash.ips",
                      "--document", "research/x")
    assert linked["outcome"] == "linked" and linked["replaced"] == kept["id"]
    assert linked["file"]["document"] == "research/x" and linked["file"]["sha256"] == sha(PNG)
    assert (gateway.bucket / linked["file"]["storage_key"]).read_bytes() == PNG
    assert world.ok(root, "files", "list")["files"][0]["document"] == "research/x"
    assert world.ok(root, "show", "research/x")["files"] == ["research/x-sources/crash.ips"]
    assert world.ok(root, "files", "link", "research/x-sources/crash.ips", "--document",
                    "research/x")["outcome"] == "unchanged"
    out = tmp_path / "exported"
    assert world.ok(root, "export", str(out))["complete"]
    again = world.ok(root, "import", str(out))["files"][0]
    assert again["outcome"] == "unchanged" and again["document"] == "research/x"
    unlinked = world.ok(root, "files", "link", "research/x-sources/crash.ips", "--none")
    assert unlinked["outcome"] == "unlinked" and unlinked["file"]["document"] is None
    assert world.ok(root, "show", "research/x")["files"] == []
    with connect(world.schema) as conn:
        rows = conn.execute("SELECT retired_at IS NOT NULL FROM assets_files "
                            "ORDER BY created_at").fetchall()
    assert rows == [(True,), (True,), (False,)]
    missing = world.run(root, "files", "link", "research/x-sources/crash.ips",
                        "--document", "research/z")
    assert missing.returncode == 3 and error(missing)["code"] == "not_found"


@needs_gateway
def test_doctor_proves_the_bucket(world, gateway):  # noqa: F811
    root = connected(world.project(), gateway)
    healthy = world.ok(root, "doctor")
    assert healthy["ok"] and healthy["connections"]["files"]["ok"]
    assert healthy["connections"]["files"]["bucket"] == BUCKET
    assert healthy["connections"]["files"]["proof"] == ["put", "head", "delete"]
    assert not [p for p in gateway.bucket.rglob("*") if p.is_file()]
    connected(root, gateway, allow_write=False)
    listed = world.ok(root, "doctor")["connections"]["files"]
    assert listed["ok"] and listed["proof"] == ["list"]
    connected(root, gateway, enabled=False)
    withheld = world.ok(root, "doctor")
    assert withheld["ok"] and withheld["connections"]["files"]["code"] == "connection_not_granted"
    connected(root, gateway, secret="not-the-secret")
    refused = world.run(root, "doctor")
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["connections"]["files"]["code"] == "unauthorized"
    (root / ".env.local").write_text("")
    unset = world.run(root, "doctor")
    assert unset.returncode == 2 and error(unset)["code"] == "credentials_missing"


@needs_gateway
def test_writing_to_the_bucket_is_a_write(world, gateway, tmp_path):  # noqa: F811
    root = connected(world.project(), gateway)
    world.ok(root, "files", "add", "a.bin", "--from", "-", stdin=b"a")
    switched = world.run(root, "files", "add", "b.bin", "--from", "-", stdin=b"b",
                         CAPABILITIES_READ_ONLY="1")
    assert switched.returncode == 4 and error(switched)["code"] == "read_only_switch"
    connected(root, gateway, allow_write=False)
    gated = world.run(root, "files", "add", "b.bin", "--from", "-", stdin=b"b")
    assert gated.returncode == 4 and error(gated)["code"] == "read_only"
    (root / "assets").mkdir()
    (root / "assets" / "c.bin").write_bytes(b"c")
    imported = world.run(root, "import")
    assert imported.returncode == 4 and error(imported)["code"] == "read_only"
    read = world.run(root, "files", "get", "a.bin", "--out", "-")
    assert read.returncode == 0 and read.stdout == b"a"
    assert world.ok(root, "files", "list")["count"] == 1
    world.ok(root, "save", "plans/p", "--from", "-", stdin=b"# P\n")
    unlinkable = world.run(root, "files", "link", "a.bin", "--document", "plans/p")
    assert unlinkable.returncode == 4 and error(unlinkable)["code"] == "read_only"


def test_without_a_connection_only_the_file_verbs_need_one(world, tmp_path):  # noqa: F811
    root = world.project()
    doctor = world.ok(root, "doctor")
    assert doctor["ok"] and doctor["connections"] == {}
    listing = world.run(root, "connections")
    assert listing.returncode == 6 and error(listing)["code"] == "connections_required"
    for args in (("files", "add", "a.bin", "--from", "-"), ("files", "get", "a.bin", "--out", "-")):
        refused = world.run(root, *args, stdin=b"a")
        assert refused.returncode == 6 and error(refused)["code"] == "connections_required"
    assert world.ok(root, "files", "list")["count"] == 0
    (root / "assets" / "plans").mkdir(parents=True)
    (root / "assets" / "plans" / "p.md").write_bytes(b"# P\n")
    (root / "assets" / "plans" / "chart.png").write_bytes(PNG)
    imported = world.ok(root, "import")
    assert [r["outcome"] for r in imported["documents"]] == ["created"]
    assert imported["files"] == [] and imported["skipped"] == [
        {"path": "plans/chart.png",
         "reason": "not markdown and no file store connection is configured"}]
    exported = world.ok(root, "export", str(tmp_path / "out"))
    assert exported["complete"] and exported["files"] == []
    assert _tree(tmp_path / "out") == {"plans/p.md": b"# P\n"}


@needs_gateway
def test_a_kept_file_is_deleted_from_the_bucket(world, gateway, tmp_path):  # noqa: F811
    root = connected(world.project(), gateway)
    world.ok(root, "save", "research/x", "--from", "-", stdin=b"# X\n")
    first = world.ok(root, "files", "add", "research/x/me.png", "--from", "-", "--document",
                     "research/x", stdin=PNG)["file"]
    second = world.ok(root, "files", "add", "research/x/me.png", "--from", "-", "--document",
                      "research/x", stdin=PNG + b"!")["file"]
    world.ok(root, "files", "add", "research/x/keep.bin", "--from", "-", "--document", "research/x",
             stdin=b"k")
    unasked = world.run(root, "files", "rm", "research/x/me.png")
    assert unasked.returncode == 6 and error(unasked)["code"] == "confirmation_required"
    assert (gateway.bucket / second["storage_key"]).read_bytes() == PNG + b"!"
    removed = world.ok(root, "files", "rm", "research/x/me.png", "--yes")
    assert removed["outcome"] == "removed" and removed["retired"] == second["id"]
    assert removed["deleted"] == [first["storage_key"], second["storage_key"]]
    assert not (gateway.bucket / first["storage_key"]).exists()
    assert not (gateway.bucket / second["storage_key"]).exists()
    assert [f["name"] for f in world.ok(root, "files", "list")["files"]] == ["research/x/keep.bin"]
    assert world.ok(root, "show", "research/x")["files"] == ["research/x/keep.bin"]
    out = tmp_path / "exported"
    assert world.ok(root, "export", str(out))["complete"]
    assert not (out / "research" / "x" / "me.png").exists()
    assert (out / "research" / "x" / "keep.bin").read_bytes() == b"k"
    again = world.run(root, "files", "rm", "research/x/me.png", "--yes")
    assert again.returncode == 3 and error(again)["code"] == "not_found"
    switched = world.run(root, "files", "rm", "research/x/keep.bin", "--yes",
                         CAPABILITIES_READ_ONLY="1")
    assert switched.returncode == 4 and error(switched)["code"] == "read_only_switch"
    readded = world.ok(root, "files", "add", "research/x/me.png", "--from", "-", stdin=b"new")
    assert readded["outcome"] == "created"
