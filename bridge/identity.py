"""The laptop's identity for the phone link: an EC P-256 key and a self-signed TLS certificate.

The phone pins the certificate's SHA-256 fingerprint (from the pairing QR code), so no certificate authority is
involved. The private key file is encrypted; its password is protected with Windows DPAPI (``protect``), so a copy
of the ``data`` folder on another PC or Windows account can't impersonate this laptop.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import socket
import ssl
from pathlib import Path
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)

CERT_FILE = "server.crt"
KEY_FILE = "server.key"
PASS_FILE = "server.key.pass"
VALID_YEARS = 10


def _default_protect() -> tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]:
    from security import token_store

    if token_store.available():
        return (lambda b: token_store.protect(b, "bridge-v1"), lambda b: token_store.unprotect(b, "bridge-v1"))
    log.warning("Windows DPAPI is not available: the phone-link key password is stored unprotected")
    return (lambda b: b, lambda b: b)


class LaptopIdentity:
    def __init__(self, folder: Path, protect: Callable[[bytes], bytes] | None = None,
                 unprotect: Callable[[bytes], bytes] | None = None) -> None:
        self.folder = Path(folder)
        if protect is None or unprotect is None:
            protect, unprotect = _default_protect()
        self._protect, self._unprotect = protect, unprotect

    @property
    def cert_path(self) -> Path:
        return self.folder / CERT_FILE

    def ensure(self) -> None:
        """Create the key and certificate on first use."""
        if (self.folder / CERT_FILE).exists() and (self.folder / KEY_FILE).exists() and \
                (self.folder / PASS_FILE).exists():
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"DUMBOO on {socket.gethostname()}"[:64])])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=365 * VALID_YEARS))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        password = secrets.token_urlsafe(32).encode()
        (self.folder / KEY_FILE).write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(password)))
        (self.folder / PASS_FILE).write_bytes(self._protect(password))
        (self.folder / CERT_FILE).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        log.info("Phone link identity created (fingerprint %s…)", self.fingerprint()[:16])

    def fingerprint(self) -> str:
        """SHA-256 of the certificate (DER), lowercase hex: what the phone pins."""
        cert = x509.load_pem_x509_certificate((self.folder / CERT_FILE).read_bytes())
        return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    def server_context(self) -> ssl.SSLContext:
        self.ensure()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # No TLS 1.3 session tickets: phones re-authenticate with their key anyway, and the tickets the server sends
        # right after the handshake raced with the first message on Windows (both sides then waited forever).
        ctx.num_tickets = 0
        password = self._unprotect((self.folder / PASS_FILE).read_bytes())
        ctx.load_cert_chain(str(self.folder / CERT_FILE), str(self.folder / KEY_FILE), password=password)
        return ctx

    def reset(self) -> None:
        """New identity (every phone has to pair again)."""
        for name in (CERT_FILE, KEY_FILE, PASS_FILE):
            (self.folder / name).unlink(missing_ok=True)
        self.ensure()
