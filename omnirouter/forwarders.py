"""Egress strategies for forwarding cached studies.

Three concrete forwarders implement a single ``Forwarder.forward`` interface:
    - ``DicomForwarder``    — DIMSE C-STORE (plain or TLS)
    - ``DicomWebForwarder`` — DICOMweb STOW-RS over HTTPS

The router calls :func:`make_forwarder` with the current ``Destination`` and
hands it the cached datasets for one study. Each forwarder returns a
:class:`ForwardResult` describing how many instances succeeded / failed.
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Iterable

import httpx
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
from pynetdicom import AE

from .config import LOCAL_AET, Destination
from .tls_util import build_client_ssl_context

log = logging.getLogger("omnirouter.forwarders")


@dataclass
class ForwardResult:
    forwarded: int = 0
    failed: int = 0
    note: str = ""
    succeeded_assoc: bool = True  # False when the *association* itself failed
    error_messages: list[str] = field(default_factory=list)


class Forwarder(ABC):
    """Abstract egress strategy."""

    def __init__(self, dest: Destination) -> None:
        self.dest = dest

    @abstractmethod
    def forward(
        self,
        study_uid: str,
        datasets: list[tuple[Path, Dataset]],
    ) -> ForwardResult:
        """Send all datasets for one study; return per-instance counts."""

    @abstractmethod
    def describe(self) -> str:
        """Short human-readable description used for log lines."""


# ---------------------------------------------------------------------------
# DIMSE C-STORE (plain or TLS)
# ---------------------------------------------------------------------------
class DicomForwarder(Forwarder):
    """Classic DIMSE forwarder — pynetdicom association + C-STORE per file."""

    def describe(self) -> str:
        proto = "TLS" if self.dest.mode == "dicom_tls" else "plain"
        return f"{self.dest.aet}@{self.dest.host}:{self.dest.port} ({proto})"

    def forward(
        self,
        study_uid: str,
        datasets: list[tuple[Path, Dataset]],
    ) -> ForwardResult:
        result = ForwardResult()
        if not datasets:
            return result

        ae = AE(ae_title=LOCAL_AET)
        ae.requested_contexts = []

        # Add storage contexts based on the actual SOP classes in the study.
        added_contexts: set[str] = set()
        for _, ds in datasets:
            if ds.SOPClassUID not in added_contexts:
                ae.add_requested_context(
                    ds.SOPClassUID,
                    [ExplicitVRLittleEndian, ImplicitVRLittleEndian],
                )
                added_contexts.add(ds.SOPClassUID)

        tls_args = None
        if self.dest.mode == "dicom_tls":
            try:
                ctx = build_client_ssl_context()
                tls_args = (ctx, self.dest.host)
            except Exception as exc:
                log.exception("Failed to build TLS context")
                result.succeeded_assoc = False
                result.failed = len(datasets)
                result.error_messages.append(f"TLS context error: {exc}")
                return result

        try:
            assoc = ae.associate(
                self.dest.host,
                self.dest.port,
                ae_title=self.dest.aet,
                tls_args=tls_args,
            )
        except Exception as exc:
            log.exception("Association attempt raised")
            result.succeeded_assoc = False
            result.failed = len(datasets)
            result.error_messages.append(f"Association error: {exc}")
            return result

        if not assoc.is_established:
            log.error(
                "Association rejected/failed: %s@%s:%s",
                self.dest.aet,
                self.dest.host,
                self.dest.port,
            )
            result.succeeded_assoc = False
            result.failed = len(datasets)
            result.error_messages.append("Association rejected")
            return result

        try:
            for f, ds in datasets:
                status = assoc.send_c_store(ds)
                if status and getattr(status, "Status", 0xFFFF) == 0x0000:
                    result.forwarded += 1
                else:
                    code = getattr(status, "Status", "n/a")
                    log.error("C-STORE for %s returned status %s", f.name, code)
                    result.failed += 1
                    result.error_messages.append(
                        f"{f.name}: C-STORE status {code}"
                    )
        finally:
            try:
                assoc.release()
            except Exception:
                pass

        return result


# ---------------------------------------------------------------------------
# DICOMweb STOW-RS (HTTPS multipart/related)
# ---------------------------------------------------------------------------
class DicomWebForwarder(Forwarder):
    """STOW-RS forwarder — POSTs all instances as multipart/related."""

    # Per-request timeout (connect, read, write, pool). Generous to allow
    # large studies; aggressive enough that an unresponsive relay surfaces.
    REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=120.0, pool=10.0)

    # Bounded transport retry: each POST is retried once on transient
    # network errors or 5xx responses (each study gets at most
    # MAX_ATTEMPTS attempts per POST). 4xx responses are never retried.
    MAX_ATTEMPTS = 2

    # In addition to transport retry, we also do **bounded per-instance
    # retry** in sync mode: after the first POST, any instances reported
    # in the PS3.18 FailedSOPSequence (00081198) are re-sent once as a
    # subset POST. So each individual SOP instance gets at most one retry.
    PER_INSTANCE_RETRY = True

    # Custom header to signal the OmniPACS Relay's delivery semantics.
    DELIVERY_HEADER = "X-OmniPACS-Delivery"

    def describe(self) -> str:
        return f"STOW-RS → {self.dest.base_url} ({self.dest.delivery_mode})"

    def forward(
        self,
        study_uid: str,
        datasets: list[tuple[Path, Dataset]],
    ) -> ForwardResult:
        result = ForwardResult()
        if not datasets:
            return result

        url = _build_stow_url(self.dest.base_url, study_uid)

        # First pass — send the full study.
        forwarded, failed, failed_sop_uids, terminal_err = self._post_batch(
            url, datasets
        )

        if terminal_err is not None:
            # Transport / 4xx / 5xx exhausted — entire batch counted as failed.
            result.succeeded_assoc = False
            result.failed = len(datasets)
            result.error_messages.append(terminal_err)
            return result

        result.forwarded = forwarded
        result.failed = failed

        # Per-instance retry — only meaningful in sync mode, where the
        # server reports per-instance status. In async mode, the SCP has
        # already taken ownership of the whole batch (202), so there's
        # nothing to retry from our side.
        if (
            self.PER_INSTANCE_RETRY
            and self.dest.delivery_mode == "sync"
            and failed_sop_uids
        ):
            retry_subset = [
                (p, ds)
                for (p, ds) in datasets
                if str(getattr(ds, "SOPInstanceUID", "")) in failed_sop_uids
            ]
            if retry_subset:
                log.info(
                    "STOW-RS per-instance retry: re-sending %d failed instance(s) "
                    "for study %s",
                    len(retry_subset),
                    study_uid,
                )
                r_fwd, r_fail, _r_uids, r_err = self._post_batch(url, retry_subset)
                if r_err is not None:
                    # Retry POST itself blew up — the original failure count stands.
                    log.warning(
                        "STOW-RS per-instance retry POST failed: %s — "
                        "leaving %d instance(s) marked failed",
                        r_err,
                        len(retry_subset),
                    )
                    result.error_messages.append(f"retry failed: {r_err}")
                else:
                    # Recompute counts: the retried instances were previously
                    # in `failed`. After retry, r_fwd of them succeeded.
                    result.forwarded += r_fwd
                    result.failed = result.failed - r_fwd
                    if result.failed < 0:
                        # Defensive — should not happen if server is consistent.
                        result.failed = 0

        return result

    def _post_batch(
        self,
        url: str,
        datasets: list[tuple[Path, Dataset]],
    ) -> tuple[int, int, set[str], str | None]:
        """Execute one STOW-RS POST with transport-level retry.

        Returns ``(forwarded, failed, failed_sop_uids, terminal_error)``.
        ``terminal_error`` is non-None only when transport/HTTP failed
        permanently — in that case the caller treats the whole batch as
        failed and skips per-instance retry.
        """
        boundary = f"omnirouter-{uuid.uuid4().hex}"
        body = _build_multipart_body(datasets, boundary)
        headers = {
            "Content-Type": (
                f'multipart/related; type="application/dicom"; boundary={boundary}'
            ),
            "Accept": "application/dicom+json",
            "Authorization": f"Bearer {self.dest.bearer_token}",
            self.DELIVERY_HEADER: self.dest.delivery_mode,
            "User-Agent": "OmniPACS-OmniRouter/1.0",
        }

        last_error: str | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            t0 = time.monotonic()
            try:
                with httpx.Client(
                    verify=self.dest.verify_tls,
                    timeout=self.REQUEST_TIMEOUT,
                    follow_redirects=False,
                ) as client:
                    resp = client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                elapsed = time.monotonic() - t0
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "STOW-RS POST failed (attempt %d/%d) after %.2fs: %s",
                    attempt,
                    self.MAX_ATTEMPTS,
                    elapsed,
                    last_error,
                )
                if attempt < self.MAX_ATTEMPTS:
                    time.sleep(min(2.0 * attempt, 5.0))
                    continue
                return 0, len(datasets), set(), last_error

            elapsed = time.monotonic() - t0

            # 2xx success — parse per-instance status.
            if 200 <= resp.status_code < 300:
                forwarded, failed, failed_uids = _parse_stow_response(
                    resp, len(datasets), self.dest.delivery_mode
                )
                log.info(
                    "STOW-RS POST %s → HTTP %d in %.2fs "
                    "(forwarded=%d, failed=%d, mode=%s)",
                    _safe_url_for_log(url),
                    resp.status_code,
                    elapsed,
                    forwarded,
                    failed,
                    self.dest.delivery_mode,
                )
                return forwarded, failed, failed_uids, None

            # 4xx — auth / bad request — don't retry, surface clearly.
            if 400 <= resp.status_code < 500:
                msg = f"HTTP {resp.status_code}: {_safe_resp_body(resp)}"
                log.error(
                    "STOW-RS POST %s rejected after %.2fs: %s",
                    _safe_url_for_log(url),
                    elapsed,
                    msg,
                )
                return 0, len(datasets), set(), msg

            # 5xx — retry once.
            last_error = f"HTTP {resp.status_code}: {_safe_resp_body(resp)}"
            log.warning(
                "STOW-RS POST %s → HTTP %d after %.2fs (attempt %d/%d): %s",
                _safe_url_for_log(url),
                resp.status_code,
                elapsed,
                attempt,
                self.MAX_ATTEMPTS,
                last_error,
            )
            if attempt < self.MAX_ATTEMPTS:
                time.sleep(min(2.0 * attempt, 5.0))
                continue
            return 0, len(datasets), set(), last_error or "STOW-RS POST failed"

        # Should not reach here.
        return 0, len(datasets), set(), last_error or "STOW-RS POST failed"


def _build_multipart_body(
    datasets: Iterable[tuple[Path, Dataset]],
    boundary: str,
) -> bytes:
    """Build a ``multipart/related; type=application/dicom`` body."""
    crlf = b"\r\n"
    boundary_b = boundary.encode("ascii")
    buf = BytesIO()
    for f, ds in datasets:
        # Encode the dataset back to a DICOM file in memory.
        part = BytesIO()
        ds.save_as(part, write_like_original=False)
        part_bytes = part.getvalue()

        buf.write(b"--" + boundary_b + crlf)
        buf.write(b"Content-Type: application/dicom" + crlf)
        buf.write(crlf)
        buf.write(part_bytes)
        buf.write(crlf)
    buf.write(b"--" + boundary_b + b"--" + crlf)
    return buf.getvalue()


def _build_stow_url(base_url: str, study_uid: str) -> str:
    """Build the STOW-RS POST target URL for a given study.

    PS3.18 allows two endpoint shapes for STOW:
    - ``/studies``                          (server picks/uses StudyInstanceUID from the dataset)
    - ``/studies/{StudyInstanceUID}``       (study-specific endpoint)

    Different stock deployments expose different roots in their config:
    - Orthanc DICOMweb plugin uses ``/dicom-web``
    - dcm4chee uses ``/dcm4chee-arc/aets/<AET>/rs``
    - the OmniPACS Relay (Task #2) uses ``/`` as its base

    To accommodate this, we let the operator paste either the server root
    (``https://relay/``, ``https://orthanc/dicom-web``) or the studies
    endpoint directly (``https://relay/studies``). If the base URL already
    ends with ``/studies`` (case-insensitive), we just append the study
    UID; otherwise we append ``/studies/<uid>``. Trailing slashes are
    tolerated either way.
    """
    base = base_url.rstrip("/")
    if base.lower().endswith("/studies"):
        return f"{base}/{study_uid}"
    return f"{base}/studies/{study_uid}"


def _parse_stow_response(
    resp: httpx.Response,
    expected_count: int,
    delivery_mode: str = "sync",
) -> tuple[int, int, set[str]]:
    """Return (forwarded, failed, failed_sop_uids) from a STOW-RS response.

    Sync vs async semantics:
      - In **async** mode (``X-OmniPACS-Delivery: async``), a ``202`` means
        the SCP has taken ownership of the whole batch — count all as
        forwarded.
      - In **sync** mode, a ``202`` is unexpected — the server isn't
        reporting per-instance status the way we asked. We log a warning
        and count the batch as failed so it surfaces on the dashboard.

    For 200-class responses (sync), the body must be DICOM JSON with
    ``00081199`` (ReferencedSOPSequence, successes) and/or ``00081198``
    (FailedSOPSequence, failures). The third element of the tuple is the
    set of ``SOPInstanceUID`` strings that failed — used by the caller to
    drive bounded per-instance retry. Anything we can't parse is
    conservatively counted as **failed** with an empty retry set (so a
    malformed/non-conforming server doesn't silently look like success
    *and* doesn't trigger a useless full-batch retry).
    """
    content_type = resp.headers.get("content-type", "").lower()

    if resp.status_code == 202:
        if delivery_mode == "async":
            # Async accept — server took ownership of all instances.
            return expected_count, 0, set()
        # 202 in sync mode is a delivery-semantics mismatch.
        log.warning(
            "STOW-RS sync request got HTTP 202 (async accept) — server is "
            "not honoring sync delivery; counting %d instance(s) as failed",
            expected_count,
        )
        return 0, expected_count, set()

    # 2xx with body. Per PS3.18 the response is DICOM JSON.
    if "json" not in content_type:
        log.warning(
            "STOW-RS sync response has non-JSON Content-Type %r; "
            "cannot verify per-instance status — counting %d instance(s) as failed",
            content_type or "<missing>",
            expected_count,
        )
        return 0, expected_count, set()

    try:
        body = resp.json()
    except Exception:
        log.warning(
            "STOW-RS sync response JSON parse failed — "
            "counting %d instance(s) as failed",
            expected_count,
        )
        return 0, expected_count, set()

    if isinstance(body, list):
        body = body[0] if body else {}
    if not isinstance(body, dict):
        log.warning(
            "STOW-RS sync response has unexpected JSON shape (%s) — "
            "counting %d instance(s) as failed",
            type(body).__name__,
            expected_count,
        )
        return 0, expected_count, set()

    succeeded = body.get("00081199", {}).get("Value", []) or []
    failed = body.get("00081198", {}).get("Value", []) or []
    s_count = len(succeeded) if isinstance(succeeded, list) else 0
    f_count = len(failed) if isinstance(failed, list) else 0

    # Neither sequence present — server returned 200 with an empty/unknown
    # JSON body. Per PS3.18 a sync 200 response should report at least one
    # of these sequences. Treat as failed so it surfaces on the dashboard.
    if s_count == 0 and f_count == 0:
        log.warning(
            "STOW-RS 200 response had no ReferencedSOPSequence (00081199) "
            "or FailedSOPSequence (00081198) — counting %d instance(s) as failed",
            expected_count,
        )
        return 0, expected_count, set()

    # Extract SOPInstanceUIDs of failed instances for bounded per-instance
    # retry. Each FailedSOPSequence item carries 00081155
    # (ReferencedSOPInstanceUID) per PS3.18.
    failed_uids: set[str] = set()
    if isinstance(failed, list):
        for item in failed:
            if not isinstance(item, dict):
                continue
            uid_field = item.get("00081155", {})
            if not isinstance(uid_field, dict):
                continue
            uid_values = uid_field.get("Value") or []
            if isinstance(uid_values, list) and uid_values:
                uid = uid_values[0]
                if isinstance(uid, str) and uid:
                    failed_uids.add(uid)

    return s_count, f_count, failed_uids


def _safe_url_for_log(url: str) -> str:
    """Truncate very long URLs for cleaner log lines."""
    if len(url) <= 120:
        return url
    return url[:60] + "…" + url[-50:]


def _safe_resp_body(resp: httpx.Response) -> str:
    """Return a short, single-line body excerpt for logging."""
    try:
        text = resp.text
    except Exception:
        return "<unreadable body>"
    if not text:
        return "<empty body>"
    text = " ".join(text.split())  # collapse whitespace
    if len(text) > 200:
        text = text[:200] + "…"
    return text


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def make_forwarder(dest: Destination) -> Forwarder:
    """Pick the right forwarder for the destination's mode."""
    if dest.mode == "dicomweb":
        return DicomWebForwarder(dest)
    # ``dicom`` and ``dicom_tls`` both use the DIMSE forwarder.
    return DicomForwarder(dest)
