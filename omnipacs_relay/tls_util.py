"""TLS helpers for the OmniPACS Relay.

The relay terminates STOW-RS over HTTPS. Operators can supply real
CA-issued credentials by setting OMNI_RELAY_TLS_CERT / OMNI_RELAY_TLS_KEY;
otherwise the relay generates a self-signed cert on first boot and serves
HTTPS with that. This means a fresh `python -m omnipacs_relay.main` is
HTTPS by default — fine for development and good for production once a
real cert is wired in via env vars.

The generated material is written under ``<spool>/tls/`` with mode 0600.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .config import SPOOL_DIR

log = logging.getLogger("omnipacs_relay.tls")

DEV_CERT_DIR = SPOOL_DIR / "tls"
DEV_CERT_PATH = DEV_CERT_DIR / "omnipacs-relay.crt"
DEV_KEY_PATH = DEV_CERT_DIR / "omnipacs-relay.key"


def _generate_self_signed(cert_path: Path, key_path: Path) -> None:
    log.info("Generating self-signed TLS credentials at %s", cert_path.parent)
    cert_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OmniPACS"),
            x509.NameAttribute(NameOID.COMMON_NAME, "omnipacs-relay.local"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.utcnow() - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.utcnow() + _dt.timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("omnipacs-relay.local"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # Best-effort tighten — the key half is sensitive.
    for p in (cert_path, key_path):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def ensure_dev_self_signed() -> tuple[str, str]:
    """Return (cert_path, key_path), generating a self-signed pair if needed.

    Used when neither OMNI_RELAY_TLS_CERT nor OMNI_RELAY_TLS_KEY is set.
    Subsequent boots reuse the on-disk pair so the relay's HTTPS identity
    is stable across restarts (otherwise every restart would invalidate
    pinned trust on remote OmniRouter installs talking to the dev cert).
    """
    if not DEV_CERT_PATH.exists() or not DEV_KEY_PATH.exists():
        _generate_self_signed(DEV_CERT_PATH, DEV_KEY_PATH)
    else:
        log.info("Reusing self-signed TLS credentials at %s", DEV_CERT_PATH)
    return str(DEV_CERT_PATH), str(DEV_KEY_PATH)
