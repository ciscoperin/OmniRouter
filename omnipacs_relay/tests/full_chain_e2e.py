"""Full-chain end-to-end test: DIMSE → OmniRouter → STOW-RS → OmniPACS Relay → DIMSE.

The existing ``smoke_e2e.py`` posts STOW requests directly into the relay,
which validates the relay's own logic but stops at the relay's HTTP front
door. This test exercises the full production wire chain in a single run:

    [test SCU] --C-STORE--> [OmniRouter:7775]
                                |
                                | quiet-period batches the study,
                                | then STOW-RS over HTTPS
                                v
                           [OmniPACS Relay:8000]
                                |
                                | bearer-auth validates,
                                | spools, then C-STORE
                                v
                           [stand-in PACS]

If any leg breaks (DIMSE in, TLS / bearer / multipart on the STOW hop, or
C-STORE out) the test reports a clear PASS/FAIL line per scenario and
exits non-zero.

If the OmniRouter and OmniPACSRelay workflows are already running, the
test attaches to them. Otherwise it boots both services itself as
subprocesses and shuts them down at the end. The test:

  1. Snapshots each service's runtime config, then mutates them to drive
     the chain into the test harness, and restores the snapshot on exit.
     The router's bearer token is write-only via the API, so to avoid
     overwriting a real operator token with a throwaway one the test
     **refuses to run** when the router is already in DICOMweb mode (set
     ``FULL_CHAIN_E2E_FORCE=1`` to override at your own risk; the
     pre-existing bearer cannot be restored).
  2. Spins up an in-process pynetdicom Storage SCP on a free port to act
     as the local PACS.
  3. Sends synthetic DICOM through the chain and observes arrivals at
     the stand-in PACS.

Run from the repo root:

    python -m omnipacs_relay.tests.full_chain_e2e
"""

from __future__ import annotations

import io
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from typing import Iterator

import httpx

warnings.filterwarnings("ignore", message=".*Unverified HTTPS request.*")

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    SecondaryCaptureImageStorage,
    generate_uid,
)
from pynetdicom import AE, evt
from pynetdicom.sop_class import SecondaryCaptureImageStorage as SCStorageSOP
from pynetdicom.sop_class import Verification

# --- Endpoints --------------------------------------------------------------
RELAY_BASE = os.environ.get("RELAY_BASE", "https://127.0.0.1:8000")
ROUTER_BASE = os.environ.get("ROUTER_BASE", "http://127.0.0.1:5000")
ROUTER_DIMSE_HOST = os.environ.get("ROUTER_DIMSE_HOST", "127.0.0.1")
ROUTER_DIMSE_PORT = int(os.environ.get("ROUTER_DIMSE_PORT", "7775"))
ROUTER_DIMSE_AET = os.environ.get("ROUTER_DIMSE_AET", "OMNI")

# OmniRouter batches per study with STUDY_QUIET_SECONDS=3, then forwards.
# Allow generous slack for the STOW hop + relay sync forward + C-STORE.
QUIET_PERIOD_SLACK_S = 30.0
NEGATIVE_OBSERVATION_S = 15.0

HTTP_VERIFY = False  # the relay uses a self-signed dev cert


# ---------------------------------------------------------------------------
# Stand-in PACS (in-process pynetdicom SCP)
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

        self._scp = ae.start_server(
            ("0.0.0.0", self.port),
            evt_handlers=[(evt.EVT_C_STORE, on_store)],
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


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Synthetic DICOM
# ---------------------------------------------------------------------------
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
    ds.PatientName = "FULLCHAIN^E2E"
    ds.PatientID = "FULLCHAIN-E2E"
    ds.Modality = "OT"
    ds.ConversionType = "WSD"
    ds.AccessionNumber = uuid.uuid4().hex[:12]
    ds.StudyDate = time.strftime("%Y%m%d")
    ds.StudyTime = time.strftime("%H%M%S")
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    return ds


def cstore_to_router(ds: Dataset) -> tuple[bool, str]:
    """Open a DIMSE association to OmniRouter and C-STORE one instance.

    Returns (ok, detail). ok is True only when the SCP returned 0x0000.
    """
    ae = AE(ae_title="FULLCHAIN_SCU")
    ae.add_requested_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)
    try:
        assoc = ae.associate(
            ROUTER_DIMSE_HOST, ROUTER_DIMSE_PORT, ae_title=ROUTER_DIMSE_AET
        )
    except Exception as exc:
        return False, f"associate raised: {exc!r}"
    if not assoc.is_established:
        return False, "association not established"
    try:
        status = assoc.send_c_store(ds)
        code = getattr(status, "Status", 0xFFFF) if status else 0xFFFF
        if code != 0x0000:
            return False, f"C-STORE status 0x{code:04X}"
        return True, "ok"
    finally:
        try:
            assoc.release()
        except Exception:
            pass


def make_study_instances(n: int) -> list[Dataset]:
    """Build n synthetic instances belonging to the same study + series.

    Each instance gets a fresh SOPInstanceUID and InstanceNumber but
    shares StudyInstanceUID, SeriesInstanceUID, and AccessionNumber so
    the OmniRouter's quiet-period batcher groups them as one study.
    """
    study_uid = generate_uid()
    series_uid = generate_uid()
    accession = uuid.uuid4().hex[:12]
    instances: list[Dataset] = []
    for i in range(n):
        ds = make_dicom_instance()
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.AccessionNumber = accession
        ds.InstanceNumber = i + 1
        instances.append(ds)
    return instances


def cstore_batch_to_router(datasets: list[Dataset]) -> tuple[int, list[str]]:
    """Open one DIMSE association and C-STORE every dataset in order.

    Returns (success_count, error_strings). Sending in a single
    association mirrors how a real modality emits a multi-image study
    and keeps every instance's ``last_update`` inside the router's
    quiet-period window so they batch together.
    """
    if not datasets:
        return 0, []
    ae = AE(ae_title="FULLCHAIN_SCU")
    ae.add_requested_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)
    errors: list[str] = []
    try:
        assoc = ae.associate(
            ROUTER_DIMSE_HOST, ROUTER_DIMSE_PORT, ae_title=ROUTER_DIMSE_AET
        )
    except Exception as exc:
        return 0, [f"associate raised: {exc!r}"]
    if not assoc.is_established:
        return 0, ["association not established"]
    succeeded = 0
    try:
        for i, ds in enumerate(datasets):
            try:
                status = assoc.send_c_store(ds)
                code = getattr(status, "Status", 0xFFFF) if status else 0xFFFF
                if code == 0x0000:
                    succeeded += 1
                else:
                    errors.append(
                        f"#{i} sop={str(ds.SOPInstanceUID)[:12]}… "
                        f"status=0x{code:04X}"
                    )
            except Exception as exc:
                errors.append(f"#{i} send raised: {exc!r}")
    finally:
        try:
            assoc.release()
        except Exception:
            pass
    return succeeded, errors


def _build_dicom_multipart(
    datasets: list[Dataset], boundary: str
) -> bytes:
    """Minimal ``multipart/related; type=application/dicom`` body builder.

    Used by the async-direct-to-relay sub-check so the test does not
    have to import private helpers from ``omnirouter.forwarders``.
    """
    crlf = b"\r\n"
    bb = boundary.encode("ascii")
    buf = bytearray()
    for ds in datasets:
        bio = io.BytesIO()
        ds.save_as(bio, write_like_original=False)
        buf += b"--" + bb + crlf
        buf += b"Content-Type: application/dicom" + crlf
        buf += crlf
        buf += bio.getvalue()
        buf += crlf
    buf += b"--" + bb + b"--" + crlf
    return bytes(buf)


# ---------------------------------------------------------------------------
# Service control planes
# ---------------------------------------------------------------------------
def wait_for_relay() -> None:
    deadline = time.time() + 15
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{RELAY_BASE}/healthz", timeout=2, verify=HTTP_VERIFY)
            if r.status_code == 200:
                return
        except Exception as exc:
            last_err = exc
        time.sleep(0.25)
    raise RuntimeError(f"OmniPACS Relay did not respond in time: {last_err}")


def wait_for_router() -> None:
    deadline = time.time() + 15
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{ROUTER_BASE}/api/status", timeout=2)
            if r.status_code == 200 and r.json().get("router", {}).get("running"):
                return
        except Exception as exc:
            last_err = exc
        time.sleep(0.25)
    raise RuntimeError(f"OmniRouter did not respond in time: {last_err}")


def issue_relay_token(label: str) -> str:
    r = httpx.post(
        f"{RELAY_BASE}/api/tokens", json={"label": label},
        timeout=10, verify=HTTP_VERIFY,
    )
    r.raise_for_status()
    return r.json()["token"]


def revoke_relay_token(label: str) -> None:
    try:
        httpx.delete(
            f"{RELAY_BASE}/api/tokens/{label}", timeout=10, verify=HTTP_VERIFY,
        )
    except Exception:
        pass


def get_relay_local_target() -> dict:
    r = httpx.get(f"{RELAY_BASE}/api/local-target", timeout=10, verify=HTTP_VERIFY)
    r.raise_for_status()
    return r.json()


def put_relay_local_target(host: str, port: int, aet: str, delivery: str) -> None:
    r = httpx.put(
        f"{RELAY_BASE}/api/local-target",
        json={"host": host, "port": port, "aet": aet,
              "default_delivery_mode": delivery},
        timeout=10, verify=HTTP_VERIFY,
    )
    r.raise_for_status()


def get_router_destination() -> dict:
    r = httpx.get(f"{ROUTER_BASE}/api/destination", timeout=10)
    r.raise_for_status()
    return r.json()


def put_router_destination(payload: dict) -> None:
    r = httpx.put(f"{ROUTER_BASE}/api/destination", json=payload, timeout=10)
    if r.status_code >= 400:
        raise RuntimeError(f"PUT /api/destination → {r.status_code}: {r.text}")


def get_router_status() -> dict:
    r = httpx.get(f"{ROUTER_BASE}/api/status", timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Snapshot / restore so a developer running the test doesn't lose their
# carefully-tuned dashboard config.
# ---------------------------------------------------------------------------
@contextmanager
def restore_relay_local_target() -> Iterator[None]:
    snap = get_relay_local_target()
    try:
        yield
    finally:
        try:
            put_relay_local_target(
                host=snap["host"], port=snap["port"], aet=snap["aet"],
                delivery=snap.get("default_delivery_mode", "sync"),
            )
        except Exception as exc:
            log(f"[warn] could not restore relay local target: {exc}")


@contextmanager
def restore_router_destination() -> Iterator[None]:
    """Snapshot router destination on entry, restore the *non-secret*
    fields on exit. Bearer tokens are write-only via the API so we can't
    restore them — the caller must guarantee (via ``assert_router_safe_to_mutate``)
    that the snapshot is not in DICOMweb mode, where the bearer is the
    only thing keeping the destination usable.
    """
    snap = get_router_destination()
    try:
        yield
    finally:
        try:
            mode = snap.get("mode", "dicom_tls")
            if mode in ("dicom", "dicom_tls"):
                put_router_destination({
                    "mode": mode,
                    "host": snap.get("host") or "wan.example.com",
                    "port": int(snap.get("port") or 11112),
                    "aet": snap.get("aet") or "REMOTE_PACS",
                })
            else:
                # Snapshot was DICOMweb (the assert lets this through only
                # under FULL_CHAIN_E2E_FORCE=1) — we cannot restore the
                # bearer the operator had. Fall back to a benign DIMSE-TLS
                # placeholder so the router stops trying to authenticate
                # with our test token. Operator must re-enter their
                # DICOMweb config from the dashboard.
                log("[warn] router was in DICOMweb mode at start; cannot restore "
                    "the original bearer token — falling back to a DIMSE-TLS "
                    "placeholder. Re-enter your DICOMweb destination in the UI.")
                put_router_destination({
                    "mode": "dicom_tls",
                    "host": "wan.example.com",
                    "port": 11112,
                    "aet": "REMOTE_PACS",
                })
        except Exception as exc:
            log(f"[warn] could not restore router destination: {exc}")


def assert_router_safe_to_mutate() -> None:
    """Refuse to run when the router is already in DICOMweb mode.

    The destination's bearer token is write-only via the API; once we
    overwrite it with our throwaway test token (and later revoke it),
    there's no way to put the operator's original bearer back. Aborting
    here is safer than silently corrupting their config.

    Setting ``FULL_CHAIN_E2E_FORCE=1`` bypasses the check (the user
    explicitly accepts that the router will end up on a DIMSE-TLS
    placeholder and they'll need to re-enter their DICOMweb destination
    from the dashboard afterwards).
    """
    snap = get_router_destination()
    if snap.get("mode") != "dicomweb":
        return
    if os.environ.get("FULL_CHAIN_E2E_FORCE", "").lower() in ("1", "true", "yes"):
        log("[warn] router is in DICOMweb mode and FULL_CHAIN_E2E_FORCE=1 is "
            "set — your existing bearer token WILL be lost. The router will "
            "be left on a DIMSE-TLS placeholder; re-enter the DICOMweb "
            "destination in the dashboard after the test.")
        return
    raise RuntimeError(
        "Refusing to run: OmniRouter is currently configured for DICOMweb "
        "(STOW-RS) and the test would overwrite its bearer token with a "
        "throwaway one that gets revoked at the end. Switch the router to "
        "DIMSE / DIMSE-TLS in the dashboard before running the test, or "
        "set FULL_CHAIN_E2E_FORCE=1 to accept that the bearer token will "
        "be lost."
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_happy_path(scp: TestSCP) -> bool:
    """DIMSE C-STORE → OmniRouter → STOW-RS → Relay → C-STORE → stand-in PACS."""
    log("\n=== Scenario 1: full chain delivery ===")
    ds = make_dicom_instance()
    sop_uid = str(ds.SOPInstanceUID)
    study_uid = str(ds.StudyInstanceUID)

    pre = get_router_status()["router"]
    pre_received = pre.get("instances_received", 0)
    pre_forwarded = pre.get("instances_forwarded", 0)
    pre_failures = pre.get("forward_failures", 0)

    log(f"[chain] sending sop={sop_uid[:12]}… study={study_uid[:12]}… "
        f"into router DIMSE {ROUTER_DIMSE_AET}@"
        f"{ROUTER_DIMSE_HOST}:{ROUTER_DIMSE_PORT}")
    t_send = time.time()
    ok, detail = cstore_to_router(ds)
    if not ok:
        log(f"[FAIL] DIMSE leg into OmniRouter failed: {detail}")
        return False
    log(f"[chain] DIMSE C-STORE accepted by OmniRouter ({detail})")

    # Wait for: quiet-period batch → STOW-RS hop → relay sync forward → C-STORE.
    deadline = t_send + QUIET_PERIOD_SLACK_S
    while time.time() < deadline and not scp.has(sop_uid):
        time.sleep(0.25)
    if not scp.has(sop_uid):
        # Surface every leg's view of why it didn't arrive.
        post = get_router_status()["router"]
        log(f"[FAIL] sop {sop_uid} did not reach stand-in PACS within "
            f"{QUIET_PERIOD_SLACK_S:.0f}s. Stand-in received: {scp.all()!r}")
        log(f"[FAIL] router stats before/after: "
            f"received {pre_received}->{post.get('instances_received')}, "
            f"forwarded {pre_forwarded}->{post.get('instances_forwarded')}, "
            f"forward_failures={pre_failures}->{post.get('forward_failures')}")
        return False

    elapsed = time.time() - t_send
    post = get_router_status()["router"]
    fwd_delta = post.get("instances_forwarded", 0) - pre_forwarded
    fail_delta = post.get("forward_failures", 0) - pre_failures
    log(f"[chain] sop {sop_uid[:12]}… arrived at stand-in PACS in {elapsed:.1f}s")
    log(f"[chain] router instances_forwarded += {fwd_delta}, "
        f"forward_failures += {fail_delta}")

    if fwd_delta < 1:
        log("[FAIL] router did not record a forwarded instance for this study")
        return False
    if fail_delta != 0:
        log("[FAIL] router recorded a forward_failures bump during happy path")
        return False
    log(f"[PASS] DIMSE→STOW→DIMSE chain delivered sop {sop_uid}")
    return True


def scenario_negative_relay_target_unreachable(scp: TestSCP) -> bool:
    """Break the relay's *outbound* leg (closed local PACS port) and confirm
    the C-STORE we send into OmniRouter never makes it to the stand-in
    PACS, *and* that the OmniRouter records a forward failure.

    This proves the chain fails clearly when the relay's last hop breaks
    — silent success would be the worst possible regression.
    """
    log("\n=== Scenario 2: relay's local PACS leg is broken ===")

    closed_port = pick_free_port()
    snap = get_relay_local_target()
    log(f"[neg] re-pointing relay local target → 127.0.0.1:{closed_port} "
        f"(known-closed); will restore to {snap['aet']}@{snap['host']}:{snap['port']}")
    put_relay_local_target("127.0.0.1", closed_port, "NOWHERE", "sync")

    try:
        pre = get_router_status()["router"]
        pre_failures = pre.get("forward_failures", 0)
        before_count = len(scp.all())

        ds = make_dicom_instance()
        sop_uid = str(ds.SOPInstanceUID)
        ok, detail = cstore_to_router(ds)
        if not ok:
            log(f"[FAIL] DIMSE into OmniRouter failed: {detail}")
            return False

        # Allow the chain time to *try* and fail.
        deadline = time.time() + NEGATIVE_OBSERVATION_S
        while time.time() < deadline:
            if scp.has(sop_uid):
                log(f"[FAIL] sop {sop_uid} reached stand-in PACS even though "
                    f"the relay's local target is parked on a closed port")
                return False
            time.sleep(0.25)

        post = get_router_status()["router"]
        new_arrivals = len(scp.all()) - before_count
        if new_arrivals != 0:
            log(f"[FAIL] stand-in PACS unexpectedly received "
                f"{new_arrivals} instance(s) during the negative scenario")
            return False

        failure_delta = post.get("forward_failures", 0) - pre_failures
        if failure_delta < 1:
            log(f"[FAIL] expected router to record at least one forward "
                f"failure (relay rejected the STOW with 00081198 failures); "
                f"forward_failures only changed by {failure_delta}")
            return False

        log(f"[PASS] sop {sop_uid} did NOT reach stand-in PACS, and router "
            f"forward_failures rose by {failure_delta}")
        return True
    finally:
        # Always put the relay's local target back to the stand-in PACS
        # so subsequent scenarios still have a working chain. The outer
        # ``restore_relay_local_target`` context manager will swing it
        # back to the operator's snapshot at the very end.
        put_relay_local_target("127.0.0.1", scp.port, scp.aet, "sync")


def scenario_multi_image_sync(scp: TestSCP) -> bool:
    """Send a multi-image study via DIMSE in a single association and
    confirm every instance survives quiet-period batching, the STOW-RS
    hop, and the relay's C-STORE forwarding — exactly once each.

    A regression in the OmniRouter's per-study batcher or the relay's
    multipart parser would either drop instances or duplicate them; both
    are caught here with explicit per-UID accounting.
    """
    log("\n=== Scenario 3: multi-image study (sync) ===")
    n = 5
    instances = make_study_instances(n)
    sop_uids = [str(ds.SOPInstanceUID) for ds in instances]
    study_uid = str(instances[0].StudyInstanceUID)

    pre = get_router_status()["router"]
    pre_received = pre.get("instances_received", 0)
    pre_forwarded = pre.get("instances_forwarded", 0)
    pre_failures = pre.get("forward_failures", 0)
    arrivals_before = list(scp.all())

    log(f"[multi] sending {n} instances of study {study_uid[:12]}… "
        f"in one DIMSE association")
    t_send = time.time()
    succeeded, errs = cstore_batch_to_router(instances)
    if succeeded != n:
        log(f"[FAIL] only {succeeded}/{n} C-STORE calls accepted by router: "
            f"{errs}")
        return False
    log(f"[multi] all {n} C-STOREs accepted by OmniRouter "
        f"(took {time.time() - t_send:.2f}s)")

    deadline = t_send + QUIET_PERIOD_SLACK_S
    while time.time() < deadline:
        if all(scp.has(u) for u in sop_uids):
            break
        time.sleep(0.25)

    elapsed = time.time() - t_send
    missing = [u for u in sop_uids if not scp.has(u)]
    if missing:
        post = get_router_status()["router"]
        log(f"[FAIL] only {n - len(missing)}/{n} instances reached stand-in "
            f"PACS within {elapsed:.1f}s; missing: "
            f"{[u[:12] + '…' for u in missing]}")
        log(f"[FAIL] router stats: "
            f"received {pre_received}->{post.get('instances_received')}, "
            f"forwarded {pre_forwarded}->{post.get('instances_forwarded')}, "
            f"forward_failures {pre_failures}->{post.get('forward_failures')}")
        return False

    # Duplicate detection: count how many times each test UID showed up
    # in the stand-in PACS arrivals after we started sending.
    new_arrivals = scp.all()[len(arrivals_before):]
    counts: dict[str, int] = {}
    for uid in new_arrivals:
        counts[uid] = counts.get(uid, 0) + 1
    duplicates = {u: c for u, c in counts.items() if u in sop_uids and c > 1}
    if duplicates:
        log(f"[FAIL] stand-in PACS received duplicate instances: "
            f"{ {u[:12] + '…': c for u, c in duplicates.items()} }")
        return False

    post = get_router_status()["router"]
    fwd_delta = post.get("instances_forwarded", 0) - pre_forwarded
    fail_delta = post.get("forward_failures", 0) - pre_failures
    log(f"[multi] all {n} instances arrived at stand-in PACS in {elapsed:.1f}s "
        f"(no duplicates); router instances_forwarded += {fwd_delta}, "
        f"forward_failures += {fail_delta}")

    if fwd_delta < n:
        log(f"[FAIL] router only recorded {fwd_delta} forwarded instances; "
            f"expected ≥ {n}")
        return False
    if fail_delta != 0:
        log(f"[FAIL] router recorded {fail_delta} forward_failures during "
            f"multi-image scenario")
        return False
    log(f"[PASS] {n}-instance study {study_uid[:12]}… delivered exactly once "
        f"end-to-end")
    return True


# Async direct-to-relay must respond within this many seconds — well
# under the sync per-instance timeout (60s). In practice the relay
# returns 202 right after the spool fsync, which is millis to ~1s.
ASYNC_RESPONSE_DEADLINE_S = 5.0


def scenario_async_delivery(scp: TestSCP, token: str) -> bool:
    """Verify the relay's async (HTTP 202) contract directly, then verify
    the full chain still delivers every instance when the OmniRouter
    runs in async delivery mode.

    This is split into two parts because the "relay returns 202
    immediately" half can only be observed by the STOW client (the
    OmniRouter), and going around the router lets the test assert that
    contract directly.

    On exit the router destination is reset to ``delivery_mode=sync`` so
    later scenarios (or a re-run inside the same process) start from a
    known-good baseline.
    """
    log("\n=== Scenario 4: async delivery semantics ===")

    # ---- Part A: direct STOW to the relay must return 202 immediately ----
    n_direct = 3
    direct_instances = make_study_instances(n_direct)
    direct_sop_uids = [str(ds.SOPInstanceUID) for ds in direct_instances]
    direct_study_uid = str(direct_instances[0].StudyInstanceUID)

    boundary = f"fullchain-{uuid.uuid4().hex}"
    body = _build_dicom_multipart(direct_instances, boundary)
    headers = {
        "Content-Type": (
            f'multipart/related; type="application/dicom"; '
            f"boundary={boundary}"
        ),
        "Authorization": f"Bearer {token}",
        "X-OmniPACS-Delivery": "async",
        "Accept": "application/dicom+json",
    }
    arrivals_before_direct = list(scp.all())
    log(f"[async] direct STOW to relay with {n_direct} instance(s) of "
        f"study {direct_study_uid[:12]}…, X-OmniPACS-Delivery=async")
    t0 = time.monotonic()
    try:
        r = httpx.post(
            f"{RELAY_BASE}/studies/{direct_study_uid}",
            content=body, headers=headers,
            timeout=10.0, verify=HTTP_VERIFY,
        )
    except Exception as exc:
        log(f"[FAIL] direct async STOW raised: {exc!r}")
        return False
    elapsed_post = time.monotonic() - t0

    if r.status_code != 202:
        log(f"[FAIL] direct async STOW expected HTTP 202, got "
            f"{r.status_code}: {r.text[:200]}")
        return False
    try:
        payload = r.json()
    except Exception as exc:
        log(f"[FAIL] direct async STOW response was not JSON: {exc!r}")
        return False
    if payload.get("accepted") != n_direct:
        log(f"[FAIL] direct async STOW response 'accepted' was "
            f"{payload.get('accepted')!r}, expected {n_direct} "
            f"(full body: {payload!r})")
        return False
    if elapsed_post > ASYNC_RESPONSE_DEADLINE_S:
        log(f"[FAIL] direct async STOW responded in {elapsed_post:.2f}s, "
            f"expected ≤ {ASYNC_RESPONSE_DEADLINE_S:.0f}s — relay must not "
            f"block on forwarding when X-OmniPACS-Delivery=async")
        return False
    log(f"[async] relay returned HTTP 202 accepted={n_direct} in "
        f"{elapsed_post:.2f}s (well under the {ASYNC_RESPONSE_DEADLINE_S:.0f}s "
        f"deadline)")

    # The relay's worker should drain the spool and C-STORE every
    # instance to the stand-in PACS within the slack window.
    deadline = time.time() + QUIET_PERIOD_SLACK_S
    while time.time() < deadline:
        if all(scp.has(u) for u in direct_sop_uids):
            break
        time.sleep(0.25)
    missing = [u for u in direct_sop_uids if not scp.has(u)]
    if missing:
        log(f"[FAIL] async-mode relay accepted {n_direct} instance(s) but "
            f"only {n_direct - len(missing)} reached stand-in PACS within "
            f"{QUIET_PERIOD_SLACK_S:.0f}s; missing: "
            f"{[u[:12] + '…' for u in missing]}")
        return False

    # Duplicate guard for the direct-async leg too — the relay's worker
    # must not double-deliver an instance even if it's woken multiple
    # times after a kick().
    new_arrivals_direct = scp.all()[len(arrivals_before_direct):]
    direct_counts: dict[str, int] = {}
    for uid in new_arrivals_direct:
        direct_counts[uid] = direct_counts.get(uid, 0) + 1
    direct_dupes = {
        u: c for u, c in direct_counts.items()
        if u in direct_sop_uids and c > 1
    }
    if direct_dupes:
        log(f"[FAIL] direct-async leg produced duplicate arrivals at stand-in "
            f"PACS: { {u[:12] + '…': c for u, c in direct_dupes.items()} }")
        return False
    log(f"[async] all {n_direct} direct-async instances reached stand-in PACS "
        f"(no duplicates)")

    # ---- Part B: full chain in async mode --------------------------------
    # Switch the router destination to async delivery mode and send a
    # multi-image study via DIMSE. The router's STOW client should set
    # X-OmniPACS-Delivery: async and the relay's background worker must
    # still C-STORE everything to the stand-in PACS.
    log("[async] switching router destination to delivery_mode=async")
    put_router_destination({
        "mode": "dicomweb",
        "base_url": RELAY_BASE,
        "bearer_token": token,
        "verify_tls": False,
        "delivery_mode": "async",
    })

    try:
        n_chain = 4
        chain_instances = make_study_instances(n_chain)
        chain_sop_uids = [str(ds.SOPInstanceUID) for ds in chain_instances]
        chain_study_uid = str(chain_instances[0].StudyInstanceUID)
        arrivals_before = list(scp.all())

        pre = get_router_status()["router"]
        pre_forwarded = pre.get("instances_forwarded", 0)
        pre_failures = pre.get("forward_failures", 0)

        log(f"[async] sending {n_chain} instances of study "
            f"{chain_study_uid[:12]}… into router DIMSE "
            f"(router will STOW with X-OmniPACS-Delivery=async)")
        t_send = time.time()
        succeeded, errs = cstore_batch_to_router(chain_instances)
        if succeeded != n_chain:
            log(f"[FAIL] only {succeeded}/{n_chain} C-STOREs accepted: "
                f"{errs}")
            return False

        deadline = t_send + QUIET_PERIOD_SLACK_S
        while time.time() < deadline:
            if all(scp.has(u) for u in chain_sop_uids):
                break
            time.sleep(0.25)

        elapsed = time.time() - t_send
        missing = [u for u in chain_sop_uids if not scp.has(u)]
        if missing:
            post = get_router_status()["router"]
            log(f"[FAIL] async full chain: only "
                f"{n_chain - len(missing)}/{n_chain} instances arrived in "
                f"{elapsed:.1f}s; missing: "
                f"{[u[:12] + '…' for u in missing]}")
            log(f"[FAIL] router stats: forwarded "
                f"{pre_forwarded}->{post.get('instances_forwarded')}, "
                f"forward_failures "
                f"{pre_failures}->{post.get('forward_failures')}")
            return False

        # Duplicate guard for the chain leg too.
        new_arrivals = scp.all()[len(arrivals_before):]
        counts: dict[str, int] = {}
        for uid in new_arrivals:
            counts[uid] = counts.get(uid, 0) + 1
        duplicates = {
            u: c for u, c in counts.items() if u in chain_sop_uids and c > 1
        }
        if duplicates:
            log(f"[FAIL] async chain produced duplicate arrivals at stand-in "
                f"PACS: { {u[:12] + '…': c for u, c in duplicates.items()} }")
            return False

        post = get_router_status()["router"]
        fwd_delta = post.get("instances_forwarded", 0) - pre_forwarded
        fail_delta = post.get("forward_failures", 0) - pre_failures
        log(f"[async] all {n_chain} chain instances arrived at stand-in PACS "
            f"in {elapsed:.1f}s; router instances_forwarded += {fwd_delta}, "
            f"forward_failures += {fail_delta}")

        if fwd_delta < n_chain:
            log(f"[FAIL] router only recorded {fwd_delta} forwarded; "
                f"expected ≥ {n_chain}")
            return False
        if fail_delta != 0:
            log(f"[FAIL] router recorded {fail_delta} forward_failures in "
                f"async chain")
            return False
        log(f"[PASS] async delivery: relay 202 contract honored AND full "
            f"chain delivered all {n_direct + n_chain} instances exactly once")
        return True
    finally:
        # Reset destination back to sync so any later scenario or a
        # second run inside the same process starts from a clean
        # baseline. The outer ``restore_router_destination`` will
        # restore the operator's snapshot on overall exit.
        try:
            put_router_destination({
                "mode": "dicomweb",
                "base_url": RELAY_BASE,
                "bearer_token": token,
                "verify_tls": False,
                "delivery_mode": "sync",
            })
        except Exception as exc:
            log(f"[warn] could not reset router destination to sync: {exc}")


# ---------------------------------------------------------------------------
# Service bootstrap (used when the workflows aren't already running)
# ---------------------------------------------------------------------------
def _service_responding(check) -> bool:
    try:
        check()
        return True
    except Exception:
        return False


def _router_listener_active() -> bool:
    """True iff the router web is reachable AND its DIMSE listener is up.

    A bare web check is not enough: the web app can come up while the
    DIMSE C-STORE SCP fails to bind (port collision, cert error). In
    that case we want to spawn / restart, not silently attach.
    """
    try:
        r = httpx.get(f"{ROUTER_BASE}/api/status", timeout=2.0)
        if r.status_code != 200:
            return False
        data = r.json()
        return bool((data.get("router") or {}).get("running"))
    except Exception:
        return False


def _spawn(
    label: str, args: list[str], env: dict[str, str]
) -> tuple[subprocess.Popen, str]:
    """Start a child service, piping its stdout+stderr to a temp log file.

    Returns (process, log_path) so callers can surface the path in
    diagnostics when boot fails or when the test wants to debug a hang.
    """
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    log_fd, log_path = tempfile.mkstemp(prefix=f"e2e-{label}-", suffix=".log")
    log(f"[boot] starting {label}: {' '.join(args)}  (logs → {log_path})")
    full_env = os.environ.copy()
    full_env.update(env)
    proc = subprocess.Popen(
        args,
        cwd=repo_root,
        env=full_env,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        # New process group so we can SIGTERM the whole tree on exit
        # without killing this Python process if invoked from a shell.
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    os.close(log_fd)
    return proc, log_path


def _tail(path: str, n: int = 40) -> str:
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:]).rstrip()
    except Exception as exc:
        return f"(could not read log: {exc})"


@contextmanager
def ensure_services_running() -> Iterator[None]:
    """Attach to running OmniRouter / OmniPACSRelay if available, else spawn.

    On exit, only the services *we* spawned are terminated — operator
    workflows are left untouched. Spawned children pipe their stdout/
    stderr to temp log files; on boot failure we tail and print them so
    the test surfaces the cause instead of hanging silently.
    """
    children: list[tuple[str, subprocess.Popen, str]] = []

    if not _router_listener_active():
        proc, log_path = _spawn(
            "OmniRouter", [sys.executable, "-m", "omnirouter.main"], {}
        )
        children.append(("OmniRouter", proc, log_path))
    if not _service_responding(wait_for_relay):
        proc, log_path = _spawn(
            "OmniPACSRelay",
            [sys.executable, "-m", "omnipacs_relay.main"],
            {"PORT": "8000"},
        )
        children.append(("OmniPACSRelay", proc, log_path))

    if children:
        # Re-poll; if a child failed to come up, tail its log so the
        # operator sees *why* before we raise.
        try:
            wait_for_router()
            wait_for_relay()
        except Exception as exc:
            for name, p, lp in children:
                log(f"[boot] last 40 lines of {name} (pid={p.pid}):\n{_tail(lp)}\n---")
            raise RuntimeError(f"spawned services did not come up: {exc}") from exc
        log("[boot] all spawned services healthy")

    try:
        yield
    finally:
        for name, p, lp in children:
            log(f"[boot] terminating spawned {name} (pid={p.pid}, log={lp})")
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                else:
                    p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    else:
                        p.kill()
                    p.wait(timeout=5)
            except Exception as exc:
                log(f"[warn] could not cleanly stop {name}: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    log(f"[setup] router={ROUTER_BASE} (DIMSE {ROUTER_DIMSE_AET}@"
        f"{ROUTER_DIMSE_HOST}:{ROUTER_DIMSE_PORT})  relay={RELAY_BASE}")

    results: list[tuple[str, bool]] = []

    with ensure_services_running():
        log("[setup] both services healthy")

        # Pre-flight safety check BEFORE we mutate anything.
        assert_router_safe_to_mutate()

        scp_port = pick_free_port()
        scp = TestSCP(port=scp_port, aet="FULLCHAIN_PACS")
        scp.start()

        label = f"fullchain-{uuid.uuid4().hex[:6]}"
        token: str | None = None

        try:
            # Order matters: the snapshot/restore context managers wrap
            # the body, so when the body finishes (or raises), the
            # destination is restored FIRST and only then do we revoke
            # the test token. That way the router never references a
            # revoked token, even on exception.
            with restore_relay_local_target(), restore_router_destination():
                # Wire the chain: relay → stand-in PACS, router → relay.
                put_relay_local_target("127.0.0.1", scp_port, scp.aet, "sync")
                token = issue_relay_token(label)
                put_router_destination({
                    "mode": "dicomweb",
                    "base_url": RELAY_BASE,
                    "bearer_token": token,
                    "verify_tls": False,  # relay uses a self-signed dev cert
                    "delivery_mode": "sync",
                })
                log(f"[setup] router → STOW-RS {RELAY_BASE} (sync, verify_tls=False)")
                log(f"[setup] relay  → C-STORE {scp.aet}@127.0.0.1:{scp_port}")

                results.append(("happy-path",
                                scenario_happy_path(scp)))
                results.append(("multi-image-sync",
                                scenario_multi_image_sync(scp)))
                # Run the negative scenario BEFORE switching to async so
                # it observes the relay-rejection path it expects (the
                # router's STOW client needs a sync response to surface
                # 00081198 failures as forward_failures).
                results.append(("relay-leg-broken",
                                scenario_negative_relay_target_unreachable(scp)))
                results.append(("async-delivery",
                                scenario_async_delivery(scp, token)))
        finally:
            # The ``with`` above has already swapped the router off the
            # test token by now (either back to the snapshot or onto a
            # DIMSE-TLS placeholder under FORCE=1). Always revoke the
            # token from the relay — even on exception — so we don't
            # leak test tokens into ``tokens.json``.
            if token is not None:
                revoke_relay_token(label)
            scp.stop()

    log("\n=== Summary ===")
    for name, ok in results:
        log(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if results and all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
