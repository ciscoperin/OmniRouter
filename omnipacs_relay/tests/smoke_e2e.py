"""End-to-end smoke test for OmniPACS Relay.

Spins up a synthetic in-process pynetdicom storage SCP (the "local PACS"),
issues a relay token, configures the relay to forward to the SCP, and
posts a STOW-RS request directly into the relay using the same wire
contract OmniRouter uses (multipart/related + Bearer + X-OmniPACS-Delivery).

Three scenarios are exercised:

  1. **Sync STOW** — expect HTTP 200 + PS3.18 response with 00081199
     containing the SOP that was successfully forwarded.
  2. **Async STOW** — expect HTTP 202 immediately, then poll the SCP for
     receipt within a deadline.
  3. **Auth** — a request with no Authorization header gets 401 + a
     `WWW-Authenticate: Bearer …` challenge.

Run from the repo root once the OmniPACSRelay workflow is up:

    python -m omnipacs_relay.tests.smoke_e2e
"""

from __future__ import annotations

import io
import os
import socket
import sys
import threading
import time
import uuid
import warnings
from typing import Iterable

import httpx

# urllib3 / httpx warn loudly about verify=False; for a smoke test that's
# pointing at the relay's deliberately self-signed dev cert, hush them.
warnings.filterwarnings("ignore", message=".*Unverified HTTPS request.*")
from pydicom import dcmwrite
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    SecondaryCaptureImageStorage,
    generate_uid,
)
from pynetdicom import AE, evt
from pynetdicom.sop_class import SecondaryCaptureImageStorage as SCStorageSOP
from pynetdicom.sop_class import Verification

RELAY_BASE = os.environ.get("RELAY_BASE", "https://127.0.0.1:8000")
HTTP_VERIFY = False  # we accept the relay's self-signed dev cert


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------
class TestSCP:
    """Minimal in-process Storage SCP that records every received SOP UID."""

    def __init__(self, port: int, aet: str = "TEST_PACS") -> None:
        self.port = port
        self.aet = aet
        self.received: list[str] = []
        self._lock = threading.Lock()
        self._scp = None

    def start(self) -> None:
        ae = AE(ae_title=self.aet)
        ae.add_supported_context(SCStorageSOP, ExplicitVRLittleEndian)
        ae.add_supported_context(Verification)

        def on_store(event):
            ds = event.dataset
            ds.file_meta = event.file_meta
            with self._lock:
                self.received.append(str(ds.SOPInstanceUID))
            return 0x0000

        handlers = [(evt.EVT_C_STORE, on_store)]
        self._scp = ae.start_server(
            ("0.0.0.0", self.port),
            evt_handlers=handlers,
            block=False,
        )
        log(f"[scp] listening on 0.0.0.0:{self.port} aet={self.aet}")

    def stop(self) -> None:
        if self._scp is not None:
            self._scp.shutdown()

    def has(self, sop_uid: str) -> bool:
        with self._lock:
            return sop_uid in self.received

    def all(self) -> list[str]:
        with self._lock:
            return list(self.received)


def pick_free_port() -> int:
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_dicom_instance() -> Dataset:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    ds = Dataset()
    ds.file_meta = file_meta
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = "RELAY^SMOKE"
    ds.PatientID = "RELAY-SMOKE"
    ds.Modality = "OT"
    ds.ConversionType = "WSD"
    ds.AccessionNumber = uuid.uuid4().hex[:12]
    ds.StudyDate = time.strftime("%Y%m%d")
    ds.StudyTime = time.strftime("%H%M%S")
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    return ds


def encode_dicom(ds: Dataset) -> bytes:
    buf = io.BytesIO()
    dcmwrite(buf, ds, write_like_original=False)
    return buf.getvalue()


def build_multipart(parts: Iterable[bytes]) -> tuple[bytes, str]:
    """Build a STOW-RS multipart body. Returns (body_bytes, content_type)."""
    boundary = f"boundary-{uuid.uuid4().hex}"
    blob = bytearray()
    for raw in parts:
        blob += f"--{boundary}\r\n".encode()
        blob += b"Content-Type: application/dicom\r\n\r\n"
        blob += raw
        blob += b"\r\n"
    blob += f"--{boundary}--\r\n".encode()
    ct = f'multipart/related; type="application/dicom"; boundary={boundary}'
    return bytes(blob), ct


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Relay control-plane helpers
# ---------------------------------------------------------------------------
def wait_for_relay() -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            r = httpx.get(f"{RELAY_BASE}/healthz", timeout=2,
                          verify=HTTP_VERIFY)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("OmniPACS Relay did not respond in time")


def issue_token(label: str) -> str:
    log(f"[relay] issuing token label={label}")
    r = httpx.post(
        f"{RELAY_BASE}/api/tokens",
        json={"label": label},
        timeout=10,
        verify=HTTP_VERIFY,
    )
    r.raise_for_status()
    return r.json()["token"]


def revoke_token(label: str) -> None:
    httpx.delete(f"{RELAY_BASE}/api/tokens/{label}", timeout=10,
                 verify=HTTP_VERIFY)


def configure_local_target(host: str, port: int, aet: str,
                            delivery: str = "sync") -> None:
    log(f"[relay] target → {aet}@{host}:{port} (default {delivery})")
    r = httpx.put(
        f"{RELAY_BASE}/api/local-target",
        json={"host": host, "port": port, "aet": aet,
              "default_delivery_mode": delivery},
        timeout=10,
        verify=HTTP_VERIFY,
    )
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_auth_required() -> bool:
    log("\n=== Scenario 1: missing Authorization → 401 ===")
    body, ct = build_multipart([encode_dicom(make_dicom_instance())])
    r = httpx.post(
        f"{RELAY_BASE}/studies",
        content=body,
        headers={"Content-Type": ct, "Accept": "application/dicom+json"},
        timeout=10,
        verify=HTTP_VERIFY,
    )
    if r.status_code != 401:
        log(f"[FAIL] expected 401, got {r.status_code}")
        return False
    www = r.headers.get("www-authenticate", "")
    if "Bearer" not in www:
        log(f"[FAIL] expected WWW-Authenticate: Bearer …, got {www!r}")
        return False
    log(f"[PASS] 401 with WWW-Authenticate: {www}")
    return True


def scenario_sync(token: str, scp: TestSCP) -> bool:
    log("\n=== Scenario 2: sync STOW → 200 + PS3.18 payload ===")
    ds = make_dicom_instance()
    sop_uid = str(ds.SOPInstanceUID)
    study_uid = str(ds.StudyInstanceUID)
    body, ct = build_multipart([encode_dicom(ds)])

    t0 = time.time()
    r = httpx.post(
        f"{RELAY_BASE}/studies/{study_uid}",
        content=body,
        headers={
            "Content-Type": ct,
            "Accept": "application/dicom+json",
            "Authorization": f"Bearer {token}",
            "X-OmniPACS-Delivery": "sync",
        },
        timeout=60,
        verify=HTTP_VERIFY,
    )
    elapsed = time.time() - t0
    log(f"[sync] HTTP {r.status_code} in {elapsed:.2f}s")
    if r.status_code != 200:
        log(f"[FAIL] expected 200, got {r.status_code}: {r.text[:300]}")
        return False
    payload = r.json()
    successes = payload.get("00081199", {}).get("Value", [])
    failures = payload.get("00081198", {}).get("Value", [])
    if not successes or failures:
        log(f"[FAIL] expected one success / no failures, got {payload}")
        return False
    ref_sop = successes[0].get("00081155", {}).get("Value", [None])[0]
    if ref_sop != sop_uid:
        log(f"[FAIL] success SOP mismatch: response={ref_sop} sent={sop_uid}")
        return False
    if not scp.has(sop_uid):
        log(f"[FAIL] SCP did not receive sop={sop_uid}; got {scp.all()}")
        return False
    log(f"[PASS] sync SOP {sop_uid} delivered to local SCP")
    return True


def scenario_async(token: str, scp: TestSCP) -> bool:
    log("\n=== Scenario 3: async STOW → 202 then eventual delivery ===")
    ds = make_dicom_instance()
    sop_uid = str(ds.SOPInstanceUID)
    body, ct = build_multipart([encode_dicom(ds)])

    t0 = time.time()
    r = httpx.post(
        f"{RELAY_BASE}/studies",
        content=body,
        headers={
            "Content-Type": ct,
            "Accept": "application/dicom+json",
            "Authorization": f"Bearer {token}",
            "X-OmniPACS-Delivery": "async",
        },
        timeout=10,
        verify=HTTP_VERIFY,
    )
    elapsed = time.time() - t0
    log(f"[async] HTTP {r.status_code} in {elapsed:.2f}s")
    if r.status_code != 202:
        log(f"[FAIL] expected 202, got {r.status_code}: {r.text[:300]}")
        return False
    body_json = r.json()
    if body_json.get("accepted") != 1 or body_json.get("rejected") != 0:
        log(f"[FAIL] unexpected 202 body: {body_json}")
        return False

    deadline = time.time() + 30
    while time.time() < deadline and not scp.has(sop_uid):
        time.sleep(0.2)
    if not scp.has(sop_uid):
        log(f"[FAIL] SCP did not receive async SOP {sop_uid} within 30s")
        return False
    log(f"[PASS] async SOP {sop_uid} delivered to local SCP")
    return True


def scenario_sync_failfast(token: str) -> bool:
    """With the local PACS unreachable, sync STOW must fail fast (well
    under SYNC_PER_INSTANCE_TIMEOUT_S=60s) rather than waiting for the
    forwarder to exhaust its retry budget."""
    log("\n=== Scenario 5: sync fail-fast when local PACS is unreachable ===")
    # Park the local target on a definitely-closed port.
    closed_port = pick_free_port()
    configure_local_target("127.0.0.1", closed_port, "NOWHERE", delivery="sync")

    ds = make_dicom_instance()
    body, ct = build_multipart([encode_dicom(ds)])
    t0 = time.time()
    r = httpx.post(
        f"{RELAY_BASE}/studies",
        content=body,
        headers={
            "Content-Type": ct,
            "Accept": "application/dicom+json",
            "Authorization": f"Bearer {token}",
            "X-OmniPACS-Delivery": "sync",
        },
        timeout=20,
        verify=HTTP_VERIFY,
    )
    elapsed = time.time() - t0
    log(f"[sync-failfast] HTTP {r.status_code} in {elapsed:.2f}s")
    if r.status_code != 200:
        log(f"[FAIL] expected 200 with failure body, got {r.status_code}")
        return False
    if elapsed > 10:
        log(f"[FAIL] sync mode took {elapsed:.1f}s — should fail fast")
        return False
    payload = r.json()
    failures = payload.get("00081198", {}).get("Value", [])
    if not failures:
        log(f"[FAIL] expected 00081198 failures, got {payload}")
        return False
    log(f"[PASS] sync failed fast in {elapsed:.2f}s with PS3.18 failure body")
    return True


def scenario_token_last_used(token: str, label: str) -> bool:
    log("\n=== Scenario 4: per-token last-used tracking ===")
    r = httpx.get(f"{RELAY_BASE}/api/tokens", timeout=5, verify=HTTP_VERIFY)
    r.raise_for_status()
    tokens = r.json()["tokens"]
    rec = next((t for t in tokens if t["label"] == label), None)
    if rec is None:
        log(f"[FAIL] token {label!r} not found in /api/tokens response")
        return False
    if not rec.get("last_used_ts"):
        log(f"[FAIL] last_used_ts is empty for {label!r}: {rec}")
        return False
    age = time.time() - rec["last_used_ts"]
    if age < 0 or age > 300:
        log(f"[FAIL] last_used_ts looks wrong (age={age:.1f}s)")
        return False
    log(f"[PASS] token last_used_ts updated ({age:.1f}s ago)")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    wait_for_relay()

    target_port = pick_free_port()
    scp = TestSCP(port=target_port, aet="TEST_PACS")
    scp.start()

    label = f"smoke-{uuid.uuid4().hex[:6]}"
    token = None
    results: list[tuple[str, bool]] = []

    try:
        configure_local_target("127.0.0.1", target_port, "TEST_PACS",
                               delivery="sync")
        token = issue_token(label)

        results.append(("auth-401", scenario_auth_required()))
        results.append(("sync-stow", scenario_sync(token, scp)))
        results.append(("async-stow", scenario_async(token, scp)))
        # Sync fail-fast moves the local target to a closed port — do
        # the last-used scenario before it (so it has a "good" history)
        # and reset the target after.
        results.append(("last-used", scenario_token_last_used(token, label)))
        results.append(("sync-failfast", scenario_sync_failfast(token)))
        configure_local_target("127.0.0.1", target_port, "TEST_PACS",
                               delivery="sync")
    finally:
        if token is not None:
            try:
                revoke_token(label)
            except Exception:
                pass
        scp.stop()

    log("\n=== Summary ===")
    for name, ok in results:
        log(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
