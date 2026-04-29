"""DICOM SCP listener and outbound forwarder.

Receives studies via C-STORE on ``LOCAL_AET@LISTEN_PORT``, writes them
to the per-study cache directory, and forwards each instance to the
configured destination using the egress strategy selected by the current
``Destination.mode`` (plain DIMSE, DIMSE-TLS, or DICOMweb STOW-RS).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from pydicom import dcmread
from pydicom.dataset import Dataset
from pynetdicom import (
    AE,
    ALL_TRANSFER_SYNTAXES,
    StoragePresentationContexts,
    evt,
)
from pynetdicom.sop_class import Verification

from .config import (
    CACHE_DIR,
    LISTEN_BIND_HOST,
    LISTEN_PORT,
    LOCAL_AET,
    get_destination,
)
from .forwarders import make_forwarder

log = logging.getLogger("omnirouter.router")


@dataclass
class StudyState:
    study_uid: str
    received_count: int = 0
    last_update: float = field(default_factory=time.time)
    forward_thread: Optional[threading.Thread] = None
    forwarded: bool = False
    forward_started: bool = False


class OmniRouter:
    """High-level orchestrator for the DICOM SCP and forwarder."""

    # If no new instance arrives within this many seconds we assume the
    # study is complete and start forwarding.
    STUDY_QUIET_SECONDS = 3.0

    def __init__(self) -> None:
        self.ae = AE(ae_title=LOCAL_AET)
        self._configure_presentation_contexts()
        self._scp = None  # pynetdicom ThreadedAssociationServer
        self._lock = threading.Lock()
        self._studies: Dict[str, StudyState] = {}
        self._stats = defaultdict(int)
        self._started = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._start_time: float | None = None

    # ------------------------------------------------------------------
    # SCP lifecycle
    # ------------------------------------------------------------------
    def _configure_presentation_contexts(self) -> None:
        self.ae.supported_contexts = []
        # Accept verification (C-ECHO).
        self.ae.add_supported_context(Verification, ALL_TRANSFER_SYNTAXES[:])
        # Accept every standard storage SOP class.
        for ctx in StoragePresentationContexts:
            self.ae.add_supported_context(
                ctx.abstract_syntax, ALL_TRANSFER_SYNTAXES[:]
            )

    def start(self) -> None:
        if self._started:
            log.info("Omnirouter listener already running")
            return

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        handlers = [
            (evt.EVT_C_STORE, self._on_c_store),
            (evt.EVT_C_ECHO, self._on_c_echo),
            (evt.EVT_CONN_OPEN, self._on_conn_open),
            (evt.EVT_CONN_CLOSE, self._on_conn_close),
        ]

        log.info(
            "Omnirouter version 1.0.2 initializing (cache=%s)", CACHE_DIR
        )

        self._scp = self.ae.start_server(
            (LISTEN_BIND_HOST, LISTEN_PORT),
            block=False,
            evt_handlers=handlers,
        )
        self._started = True
        self._start_time = time.time()

        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_studies,
            name="omnirouter-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

        log.info("Omnirouter listener started!")

    def stop(self) -> None:
        if not self._started:
            return
        log.info("Stopping Omnirouter listener…")
        self._monitor_stop.set()
        if self._scp is not None:
            try:
                self._scp.shutdown()
            except Exception:
                log.exception("Error shutting down SCP")
            self._scp = None
        self._started = False
        self._start_time = None
        log.info("Omnirouter listener stopped.")

    # ------------------------------------------------------------------
    # Status / metrics for the UI
    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._started,
                "uptime_seconds": (
                    int(time.time() - self._start_time)
                    if self._start_time
                    else 0
                ),
                "studies_in_flight": sum(
                    1 for s in self._studies.values() if not s.forwarded
                ),
                "instances_received": self._stats["received"],
                "instances_forwarded": self._stats["forwarded"],
                "forward_failures": self._stats["forward_failures"],
                "associations_open": self._stats["associations_open"],
                "echoes": self._stats["echoes"],
            }

    # ------------------------------------------------------------------
    # SCP event handlers
    # ------------------------------------------------------------------
    def _on_conn_open(self, event) -> None:
        try:
            addr, port = event.address
        except Exception:
            addr, port = ("?", 0)
        with self._lock:
            self._stats["associations_open"] += 1
        log.info("Connection opened from %s:%s", addr, port)

    def _on_conn_close(self, event) -> None:
        try:
            addr, port = event.address
        except Exception:
            addr, port = ("?", 0)
        log.info("Connection closed from %s:%s", addr, port)

    def _on_c_echo(self, event) -> int:
        with self._lock:
            self._stats["echoes"] += 1
        log.info("C-ECHO received")
        return 0x0000

    def _on_c_store(self, event) -> int:
        try:
            ds: Dataset = event.dataset
            ds.file_meta = event.file_meta
            study_uid = getattr(ds, "StudyInstanceUID", "UNKNOWN")
            sop_uid = getattr(ds, "SOPInstanceUID", "UNKNOWN")

            study_dir = CACHE_DIR / study_uid
            study_dir.mkdir(parents=True, exist_ok=True)
            file_path = study_dir / f"{sop_uid}.dcm"
            ds.save_as(str(file_path), write_like_original=False)

            with self._lock:
                self._stats["received"] += 1
                state = self._studies.get(study_uid)
                if state is None:
                    state = StudyState(study_uid=study_uid)
                    self._studies[study_uid] = state
                    log.info(
                        "Receiving study %s containing 1 file(s)", study_uid
                    )
                else:
                    if state.forwarded:
                        # New series for an already-forwarded study —
                        # treat it as a fresh study group.
                        state = StudyState(study_uid=study_uid)
                        self._studies[study_uid] = state
                        log.info(
                            "Receiving additional study %s", study_uid
                        )
                state.received_count += 1
                state.last_update = time.time()

            return 0x0000
        except Exception:
            log.exception("Unexpected network error!")
            return 0xC001

    # ------------------------------------------------------------------
    # Study completion monitor + forwarder
    # ------------------------------------------------------------------
    def _monitor_studies(self) -> None:
        while not self._monitor_stop.is_set():
            now = time.time()
            to_forward = []
            with self._lock:
                for state in self._studies.values():
                    if (
                        not state.forward_started
                        and not state.forwarded
                        and (now - state.last_update) >= self.STUDY_QUIET_SECONDS
                    ):
                        state.forward_started = True
                        to_forward.append(state)

            for state in to_forward:
                t = threading.Thread(
                    target=self._forward_study,
                    args=(state,),
                    name=f"omnirouter-forward-{state.study_uid[:8]}",
                    daemon=True,
                )
                state.forward_thread = t
                t.start()

            self._monitor_stop.wait(1.0)

    def _forward_study(self, state: StudyState) -> None:
        study_dir = CACHE_DIR / state.study_uid
        files = sorted(study_dir.glob("*.dcm"))
        if not files:
            return

        # Read every cached file once before handing the batch to the
        # forwarder. Files we can't parse are counted as failures up front.
        datasets: list[tuple[Path, Dataset]] = []
        unreadable = 0
        for f in files:
            try:
                ds = dcmread(str(f))
                datasets.append((f, ds))
            except Exception:
                log.exception("Could not read %s for forwarding", f)
                unreadable += 1

        if not datasets:
            with self._lock:
                self._stats["forward_failures"] += unreadable
            return

        dest = get_destination()
        forwarder = make_forwarder(dest)

        log.info(
            "Sending study %s containing %d file(s) → %s",
            state.study_uid,
            len(datasets),
            forwarder.describe(),
        )

        result = forwarder.forward(state.study_uid, datasets)

        with self._lock:
            self._stats["forwarded"] += result.forwarded
            self._stats["forward_failures"] += result.failed + unreadable
            # Mark as forwarded only when we actually transmitted something
            # successfully — leaves the door open for a future retry path.
            if result.forwarded > 0 and result.failed == 0 and unreadable == 0:
                state.forwarded = True
            elif result.forwarded > 0:
                state.forwarded = True

        if result.failed == 0 and unreadable == 0:
            log.info(
                "Study %s sent (%d file(s))",
                state.study_uid,
                result.forwarded,
            )
        else:
            log.error(
                "Study %s partially sent (%d ok, %d failed, %d unreadable)",
                state.study_uid,
                result.forwarded,
                result.failed,
                unreadable,
            )


# Module-level singleton used by the web layer.
router = OmniRouter()
