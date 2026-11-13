#!/usr/bin/env python3
"""Isolated reproduction of the two integrity-check outcomes.

This driver is intentionally separate from the application's data directory.
It only talks to the public HTTP boundary of an unmodified
avprovenance service, and never deletes the durable records it creates.

Scenarios (each gets its own empty data directory):
  A  missing      : the source blob is deleted outright (source not readable).
  B  trunc-odd    : source is truncated by an odd byte count; RIFF/WAVE header
                    and data chunk header stay intact, only the PCM tail breaks.
  C  trunc-aligned: source is truncated by whole 16-bit frames (control case
                    that shows where the internal-error boundary actually is).

For every scenario the flow is:
  ingest -> complete a rendition -> baseline verified check ->
  damage the source blob -> integrity check again (real response captured) ->
  stop the server -> start a brand-new process against the same data dir ->
  read the persisted state.json so the leftover audit record is demonstrated
  after a real restart.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

PROJECT_ROOT = Path("/workspace")
sys.path.insert(0, str(PROJECT_ROOT))  # import only the trusted project package

RUNS_ROOT = Path("/tmp/av-diag/runs")


# --------------------------------------------------------------------------
# media sample construction (deterministic, stdlib only)
# --------------------------------------------------------------------------

def make_wav(path: Path, *, channels: int = 1, sample_rate: int = 8000,
             frame_count: int = 480, amplitude: int = 12000) -> bytes:
    """Write a bounded PCM s16le WAV and return its exact bytes."""
    values = []
    for index in range(frame_count):
        sample = int(amplitude * 0.5 * (1 + (index % 7) / 7.0)) - amplitude // 2
        for _ in range(channels):
            values.append(sample)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(_pack_shorts(values))
    data = buffer.getvalue()
    path.write_bytes(data)
    return data


def _pack_shorts(values: list[int]) -> bytes:
    return b"".join(int(v).to_bytes(2, "little", signed=True) for v in values)


def truncate_file(path: Path, drop_bytes: int) -> int:
    data = path.read_bytes()
    new_size = len(data) - drop_bytes
    path.write_bytes(data[:new_size])
    return new_size


# --------------------------------------------------------------------------
# tiny HTTP client against the public boundary
# --------------------------------------------------------------------------

class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def call(self, method: str, path: str, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            parsed = json.loads(raw.decode("utf-8"))
            return response.status, parsed
        finally:
            conn.close()

    def upload(self, filename: str, data: bytes) -> dict:
        status, body = self.call(
            "POST", "/v1/assets",
            {"filename": filename,
             "data_base64": base64.b64encode(data).decode("ascii")},
        )
        assert status == 201, (status, body)
        return body

    def render(self, asset_id: str, output_name: str) -> dict:
        # The plan consumes the whole 0.06 s / 480-frame source, so any tail
        # truncation lands inside the replayed region and cannot be hidden.
        status, body = self.call(
            "POST", "/v1/renditions",
            {"primary_asset_id": asset_id,
             "secondary_asset_id": None,
             "output_name": output_name,
             "operations": [
                 {"type": "media_trim", "start_seconds": 0.0,
                  "duration_seconds": 0.06},
                 {"type": "audio_gain", "db": -3},
             ]},
        )
        assert status == 201, (status, body)
        return body

    def check(self, rendition_id: str):
        return self.call(
            "POST",
            f"/v1/renditions/{rendition_id}/integrity-checks",
            {},
        )


# --------------------------------------------------------------------------
# real server subprocess, restarted against the same durable data directory
# --------------------------------------------------------------------------

def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_server(data_dir: Path, label: str):
    port = _free_port()
    log_path = data_dir / f"server-{label}.log"
    log_handle = log_path.open("wb")
    # -I ignores cwd/PYTHONPATH, so a trusted bootstrap in the scripts dir adds
    # only the project root; the data dir is an argument, never an import root.
    bootstrap = Path("/tmp/av-diag/scripts/launch_server.py")
    proc = subprocess.Popen(
        [sys.executable, "-I", str(bootstrap),
         "--host", "127.0.0.1", "--port", str(port),
         "--data-dir", str(data_dir / "data")],
        cwd=str(PROJECT_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    client = Client(port)
    while time.time() < deadline:
        try:
            status, _ = client.call("GET", "/health")
            if status == 200:
                return proc, client, log_handle
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"server for {data_dir} never became healthy")


def stop_server(proc, log_handle) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    log_handle.close()


# --------------------------------------------------------------------------
# scenario driver
# --------------------------------------------------------------------------

def run_scenario(name: str, damage: str) -> dict:
    scenario_dir = RUNS_ROOT / name
    data_dir = scenario_dir / "data"
    scenario_dir.mkdir(parents=True, exist_ok=True)

    sample_path = scenario_dir / "source.wav"
    original = make_wav(sample_path)
    original_digest = hashlib.sha256(original).hexdigest()

    proc, client, log_handle = start_server(scenario_dir, "first")
    report: dict = {
        "scenario": name,
        "damage": damage,
        "sample_path": str(sample_path),
        "original_size_bytes": len(original),
        "original_sha256": original_digest,
    }
    try:
        asset = client.upload("source.wav", original)
        rendition = client.render(asset["id"], "derived.wav")
        report["asset_id"] = asset["id"]
        report["rendition_id"] = rendition["id"]
        report["output_asset_id"] = rendition["output_asset"]["id"]

        # content key of the primary source, derived from its digest
        content_root = data_dir / "content"
        source_blob = (
            content_root
            / original_digest[:2] / original_digest[2:4] / original_digest
        )
        report["source_blob"] = str(source_blob)

        # baseline: an untouched source must verify cleanly
        base_status, base_body = client.check(rendition["id"])
        report["baseline_http_status"] = base_status
        report["baseline_audit_status"] = base_body.get("status")

        # apply the damage directly at the storage layer (simulates loss /
        # truncation on disk); the JSON records stay untouched
        if damage == "missing":
            source_blob.unlink()
            report["post_damage_size_bytes"] = 0
        elif damage == "truncate_odd":
            report["post_damage_size_bytes"] = truncate_file(source_blob, 1)
        elif damage == "truncate_aligned":
            report["post_damage_size_bytes"] = truncate_file(source_blob, 2)
        else:
            raise ValueError(damage)

        with sample_path.open("rb") as handle:
            head = handle.read(12)
        report["header_recognizable_after_damage"] = (
            head.startswith(b"RIFF") and head[8:12] == b"WAVE"
        )

        # the check whose divergent behavior we are studying
        damaged_status, damaged_body = client.check(rendition["id"])
        report["damaged_http_status"] = damaged_status
        report["damaged_response"] = damaged_body

        # repeat the odd case once more so a second pending audit is visible
        if damage == "truncate_odd":
            again_status, again_body = client.check(rendition["id"])
            report["repeat_damaged_http_status"] = again_status
            report["repeat_damaged_response"] = again_body
    finally:
        stop_server(proc, log_handle)

    # ---- restart a brand-new process against the same durable directory ----
    proc2, client2, log_handle2 = start_server(scenario_dir, "restarted")
    try:
        health_status, health = client2.call("GET", "/health")
        report["restart_health_status"] = health_status

        state = json.loads((data_dir / "state.json").read_text("utf-8"))
        audits = state["integrity_checks"]
        report["persisted_audit_count"] = len(audits)
        report["persisted_audits"] = [
            {
                "id": audit["id"],
                "status": audit["status"],
                "reason": audit["reason"],
                "expected_digest_present": audit["expected_digest"] is not None,
                "reproduced_digest": audit["reproduced_digest"],
                "created_at": audit["created_at"],
                "updated_at": audit["updated_at"],
            }
            for audit in audits.values()
        ]
        report["rendition_status_after_restart"] = (
            state["renditions"][report["rendition_id"]]["status"]
        )
        report["source_blob_still_present_after_restart"] = source_blob.exists()
    finally:
        stop_server(proc2, log_handle2)

    return report


def main() -> int:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    scenarios = [
        ("a-missing", "missing"),
        ("b-trunc-odd", "truncate_odd"),
        ("c-trunc-aligned", "truncate_aligned"),
    ]
    reports = [run_scenario(name, damage) for name, damage in scenarios]
    out = RUNS_ROOT / "diagnosis-report.json"
    out.write_text(json.dumps(reports, indent=2, ensure_ascii=False))
    print(json.dumps(reports, indent=2, ensure_ascii=False))
    print(f"\nreport written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
