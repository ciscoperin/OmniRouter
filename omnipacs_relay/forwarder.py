"""Background forwarder worker.

Drains the on-disk spool and re-emits each instance to the configured
local PACS as a DICOM C-STORE. The worker is a single dedicated thread —
it pulls one C-STORE association per pass and reuses it for the whole
batch found on that pass.

Retry policy:
- A spool entry that fails to forward is left in the inbox with an
  in-memory attempts counter.
- After ``MAX_ATTEMPTS`` consecutive failures the entry is moved to the
  quarantine directory with a sidecar ``.error`` file recording the
  reason. Quarantined entries can be requeued from the dashboard.
- Backoff between worker passes grows linearly when the local PACS is
  unreachable, capped at ``MAX_PASS_INTERVAL_S``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
from pynetdicom import AE

from .config import RELAY_AET, get_local_target
from .spool import SpoolEntry, spool

log = logging.getLogger("omnipacs_relay.forwarder")


# Per-instance bounded retries before quarantine. Matches the OmniRouter
# value (MAX_ATTEMPTS=2) plus a small extra margin since the relay's
# downstream is on the LAN and transient blips are common.
MAX_ATTEMPTS = 4

# Worker pass intervals.
IDLE_INTERVAL_S = 1.0
MAX_PASS_INTERVAL_S = 30.0


class Forwarder:
    """Background C-STORE worker."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running = threading.Event()
        # Per-(study, sop) attempt counters — reset on success / requeue.
        self._attempts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        # Sync waiters by SOP UID — used by sync STOW mode to await
        # per-instance forward outcomes from the worker.
        self._waiters: dict[str, _SopWaiter] = {}

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="omnipacs-relay-forwarder",
            daemon=True,
        )
        self._thread.start()
        self._running.set()
        log.info("Forwarder worker started → %s", get_local_target().describe())

    def stop(self) -> None:
        self._stop.set()
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._thread = None
        log.info("Forwarder worker stopped")

    def is_running(self) -> bool:
        return self._running.is_set() and bool(
            self._thread and self._thread.is_alive()
        )

    # ---------------------------------------------------------------
    # External nudge (called by STOW handler in async mode so the
    # worker doesn't have to wait for its idle interval)
    # ---------------------------------------------------------------
    def kick(self) -> None:
        # Just abuse the stop event's wait() to wake early.
        # The worker uses self._stop.wait(interval); setting then clearing
        # would also break the loop, so instead we use a separate event.
        self._wake.set()

    # ---------------------------------------------------------------
    # Sync STOW support — register a waiter that the worker fulfils
    # when it processes the matching SOP.
    # ---------------------------------------------------------------
    def register_waiter(self, sop_uid: str) -> "_SopWaiter":
        waiter = _SopWaiter(sop_uid)
        with self._lock:
            self._waiters[sop_uid] = waiter
        return waiter

    def discard_waiter(self, sop_uid: str) -> None:
        with self._lock:
            self._waiters.pop(sop_uid, None)

    # ---------------------------------------------------------------
    # Worker loop
    # ---------------------------------------------------------------
    def _loop(self) -> None:
        backoff = IDLE_INTERVAL_S
        while not self._stop.is_set():
            try:
                made_progress = self._pass()
            except Exception:
                log.exception("Forwarder pass crashed; will retry")
                made_progress = False

            if made_progress:
                # We forwarded something this pass — drain quickly.
                interval = IDLE_INTERVAL_S
                backoff = IDLE_INTERVAL_S
            else:
                # Either the queue was empty OR every entry failed (the
                # local PACS is unreachable / rejecting). Either way,
                # back off exponentially so we don't burn through a
                # MAX_ATTEMPTS budget in MAX_ATTEMPTS seconds.
                backoff = min(backoff * 2, MAX_PASS_INTERVAL_S)
                interval = backoff
            self._wake.clear()
            # Sleep but wake up early if kick()'d.
            self._wake.wait(interval)

    def _pass(self) -> bool:
        """One worker pass — drains every spool entry through ONE association.

        Returns True iff at least one entry was successfully forwarded
        this pass. The loop uses that signal to apply exponential
        backoff when the local PACS is unhappy, so transient downstream
        outages don't exhaust the per-instance retry budget within a
        few seconds.
        """
        entries = list(spool.iter_pending())
        if not entries:
            return False

        target = get_local_target()
        log.info(
            "Forwarder pass: %d instance(s) → %s",
            len(entries),
            target.describe(),
        )

        # Build the AE with all SOP classes seen this pass.
        ae = AE(ae_title=RELAY_AET)
        ae.requested_contexts = []
        added: set[str] = set()
        # We have to read the dataset to discover its SOPClassUID — do it
        # once and pass the dataset down to send_c_store.
        prepared: list[tuple[SpoolEntry, "Dataset"]] = []  # type: ignore[name-defined]
        for entry in entries:
            try:
                ds = entry.read_dataset()
            except Exception as exc:
                self._record_failure(entry, f"could not read spool file: {exc}")
                continue
            sop_class = getattr(ds, "SOPClassUID", None)
            if sop_class is None:
                self._record_failure(entry, "missing SOPClassUID")
                continue
            if sop_class not in added:
                ae.add_requested_context(
                    sop_class, [ExplicitVRLittleEndian, ImplicitVRLittleEndian]
                )
                added.add(sop_class)
            prepared.append((entry, ds))

        if not prepared:
            # Every entry failed to even parse — that's a hard error,
            # not a transient condition. Don't keep the loop tight on
            # nothing.
            return False

        try:
            assoc = ae.associate(target.host, target.port, ae_title=target.aet)
        except Exception as exc:
            log.error(
                "Could not open association to %s: %s",
                target.describe(), exc,
            )
            for entry, _ds in prepared:
                self._record_failure(entry, f"association error: {exc}")
            return False

        if not assoc.is_established:
            log.error("Association rejected by %s", target.describe())
            for entry, _ds in prepared:
                self._record_failure(entry, "association rejected")
            return False

        any_ok = False
        try:
            for entry, ds in prepared:
                try:
                    status = assoc.send_c_store(ds)
                    code = getattr(status, "Status", 0xFFFF) if status else 0xFFFF
                    if code == 0x0000:
                        self._record_success(entry)
                        any_ok = True
                    else:
                        self._record_failure(
                            entry, f"C-STORE status 0x{code:04X}"
                        )
                except Exception as exc:
                    self._record_failure(entry, f"send_c_store raised: {exc}")
        finally:
            try:
                assoc.release()
            except Exception:
                pass

        return any_ok

    # ---------------------------------------------------------------
    # Per-instance bookkeeping
    # ---------------------------------------------------------------
    def _record_success(self, entry: SpoolEntry) -> None:
        with self._lock:
            self._attempts.pop((entry.study_uid, entry.sop_uid), None)
            waiter = self._waiters.pop(entry.sop_uid, None)
        spool.mark_forwarded(entry)
        log.info("Forwarded study=%s sop=%s", entry.study_uid, entry.sop_uid)
        if waiter is not None:
            waiter.set_success()

    def _record_failure(self, entry: SpoolEntry, reason: str) -> None:
        key = (entry.study_uid, entry.sop_uid)
        with self._lock:
            n = self._attempts.get(key, 0) + 1
            self._attempts[key] = n
            # If a sync STOW caller is blocked waiting on this SOP, signal
            # them on the FIRST failure rather than letting them stall
            # through MAX_ATTEMPTS. The caller (OmniRouter) will retry the
            # whole request itself, so we also drop the entry from the
            # spool below to avoid double-delivery.
            sync_waiter = self._waiters.pop(entry.sop_uid, None) if n == 1 else None
        spool.mark_failed()
        log.warning(
            "Forward failed (attempt %d/%d) study=%s sop=%s reason=%s",
            n, MAX_ATTEMPTS, entry.study_uid, entry.sop_uid, reason,
        )
        if sync_waiter is not None:
            # Drop the entry — sync caller owns the retry from here.
            with self._lock:
                self._attempts.pop(key, None)
            spool.discard_pending(entry)
            sync_waiter.set_failure(reason)
            return
        if n >= MAX_ATTEMPTS:
            spool.quarantine(entry, reason)
            with self._lock:
                self._attempts.pop(key, None)
                waiter = self._waiters.pop(entry.sop_uid, None)
            if waiter is not None:
                waiter.set_failure(f"quarantined: {reason}")


class _SopWaiter:
    """Used by the sync STOW handler to block on the worker's outcome."""

    def __init__(self, sop_uid: str) -> None:
        self.sop_uid = sop_uid
        self._event = threading.Event()
        self._success = False
        self._reason = ""

    def set_success(self) -> None:
        self._success = True
        self._event.set()

    def set_failure(self, reason: str) -> None:
        self._success = False
        self._reason = reason
        self._event.set()

    def wait(self, timeout: float) -> tuple[bool, str]:
        ok = self._event.wait(timeout)
        if not ok:
            return False, "timed out waiting for local forward"
        return self._success, self._reason


# Module-level singleton used by the web layer.
forwarder = Forwarder()
