"""Per-daemon identity: Ed25519 keypair + self-signed cert.

On first daemon start we generate `~/.actor/daemon.key` (mode 0600) and
`~/.actor/daemon.pem` (the self-signed cert). The cert isn't *used*
for traffic until Phase 6 brings up the inter-daemon TCP listener with
mTLS — but we mint it now so its fingerprint can ride in the zeroconf
TXT record from day one (Phase 4). TOFU pinning means cert rotation is
a manual reissue + retrust event, not a routine one, so the cert is
issued for a long validity window (10 years).

Fingerprint = `sha256(DER(cert))`. Stable across restarts as long as
the cert files survive — the same identity advertises across daemon
bounces.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class Identity:
    """Daemon's persistent identity."""
    key_path: Path
    cert_path: Path
    fingerprint: str  # "sha256:<hex>"
    common_name: str

    @property
    def short_fingerprint(self) -> str:
        """`sha256:abcd1234` — first 8 hex chars after the prefix.
        Good enough for table display; full fingerprint via --verbose."""
        prefix, _, rest = self.fingerprint.partition(":")
        return f"{prefix}:{rest[:8]}"


def identity_paths(home: Path | None = None) -> tuple[Path, Path]:
    """Return (key_path, cert_path) under `~/.actor/`.

    `home` overrides $HOME — only the test suite uses this. Production
    callers pass nothing and inherit the env."""
    base = home if home is not None else Path(os.path.expanduser("~"))
    actor_dir = base / ".actor"
    return actor_dir / "daemon.key", actor_dir / "daemon.pem"


def cert_fingerprint(cert: x509.Certificate) -> str:
    """`sha256:<hex>` over the DER encoding."""
    der = cert.public_bytes(serialization.Encoding.DER)
    return f"sha256:{hashlib.sha256(der).hexdigest()}"


def load_or_create_identity(home: Path | None = None) -> Identity:
    """Read `daemon.{key,pem}` if present, otherwise mint them.

    Re-reads the existing files on every daemon start (cheap) so the
    fingerprint we advertise matches what's on disk — if the operator
    rotates the cert by hand and bounces the daemon, the new
    fingerprint shows up immediately.

    Key file mode is enforced to 0600 on creation. We don't *fix* a
    bad mode on existing files (could be intentional) — but we'd warn
    if Phase 6 ever cared, which it doesn't yet.
    """
    key_path, cert_path = identity_paths(home)
    if key_path.exists() and cert_path.exists():
        cert = _read_cert(cert_path)
        cn = _common_name(cert)
        return Identity(
            key_path=key_path,
            cert_path=cert_path,
            fingerprint=cert_fingerprint(cert),
            common_name=cn,
        )

    key_path.parent.mkdir(parents=True, exist_ok=True)
    cn = socket.gethostname() or "actord"
    key, cert = _mint(cn)

    # Write key first with 0600 — `os.open` with O_CREAT|O_EXCL forces
    # the mode at create time, avoiding a window where the file exists
    # with the umask default. Use O_TRUNC for the rare case where a
    # stale half-written key from a crashed previous start is sitting
    # there (cert is missing, otherwise we'd have returned above).
    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd = os.open(
        str(key_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, key_bytes)
    finally:
        os.close(fd)

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    return Identity(
        key_path=key_path,
        cert_path=cert_path,
        fingerprint=cert_fingerprint(cert),
        common_name=cn,
    )


def _read_cert(cert_path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(cert_path.read_bytes())


def _common_name(cert: x509.Certificate) -> str:
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return attrs[0].value if attrs else ""


def _mint(common_name: str) -> tuple[Ed25519PrivateKey, x509.Certificate]:
    """Mint a fresh Ed25519 keypair + self-signed cert, CN=<common_name>,
    valid for 10 years. The cert isn't used for traffic in Phase 4 — but
    its fingerprint is the daemon's identity on the network, so it must
    exist + be stable across restarts."""
    key = Ed25519PrivateKey.generate()

    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])

    now = _dt.datetime.now(_dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # self-signed
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=365 * 10))
    )
    cert = builder.sign(private_key=key, algorithm=None)
    return key, cert


__all__ = [
    "Identity",
    "identity_paths",
    "cert_fingerprint",
    "load_or_create_identity",
]
