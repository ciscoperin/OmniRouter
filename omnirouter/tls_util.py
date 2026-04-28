"""TLS helpers for outbound DICOM associations.

For v1 we provide generic, self-signed credentials so the router can
establish a TLS-encrypted association out of the box. Operators can swap
in real CA-issued certificates by setting the OMNI_TLS_* environment
variables.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .config import get_destination

log = logging.getLogger("omnirouter.tls")

CERT_DIR = Path(os.environ.get("OMNI_CACHE_DIR", "omnicache")).resolve() / "tls"


def _generate_self_signed(cert_path: Path, key_path: Path) -> None:
    log.info("Generating self-signed TLS credentials at %s", cert_path.parent)
    cert_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OmniRouter"),
            x509.NameAttribute(NameOID.COMMON_NAME, "omnirouter.local"),
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
                    x509.DNSName("omnirouter.local"),
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


def build_client_ssl_context() -> ssl.SSLContext:
    """Build an SSLContext suitable for DICOM TLS associations.

    Honors OMNI_TLS_* env vars if set; otherwise generates a self-signed
    client cert and disables peer verification (v1 generic defaults).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    dest = get_destination()
    cert = dest.client_cert
    key = dest.client_key

    if not cert or not key:
        cert_path = CERT_DIR / "omnirouter.crt"
        key_path = CERT_DIR / "omnirouter.key"
        if not cert_path.exists() or not key_path.exists():
            _generate_self_signed(cert_path, key_path)
        cert, key = str(cert_path), str(key_path)

    ctx.load_cert_chain(certfile=cert, keyfile=key)

    if dest.verify_peer and dest.ca_file:
        ctx.load_verify_locations(cafile=dest.ca_file)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    return ctx
