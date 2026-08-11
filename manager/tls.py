# ./manager/tls.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.tls — TLS material for the manager, including self-signed certificates.

TLS matters more here than for a typical local web app, because WebDAV
authenticates with HTTP Basic and the password IS the volume PIN. Over plain
HTTP that PIN crosses the network in base64 on every single request, and a
Finder listing is dozens of requests. Windows knows this and refuses Basic over
plain HTTP by default (`BasicAuthLevel`), so on Windows TLS is not hardening --
it is the difference between working and not working at all.

`cryptography` is already a hard dependency (Argon2id in the engine), so
generating a certificate costs no new dependency.

WHAT A SELF-SIGNED CERTIFICATE DOES AND DOES NOT BUY
----------------------------------------------------
It encrypts the connection, which is the whole point for the PIN. It does NOT
authenticate the server to a client that has not been told to trust it, and
clients differ sharply in how they react:

  * Browsers show an interstitial the user can click through.
  * rclone works with --no-check-certificate.
  * The Windows WebDAV redirector REFUSES an untrusted certificate outright,
    with no override -- so --tls-self-signed alone does not make Windows work.
    The certificate has to be imported into the machine's Trusted Root store.
  * macOS Finder likewise wants the certificate trusted in Keychain, and
    additionally refuses server certificates valid for more than 825 days,
    which is why _MAX_DAYS is what it is rather than "ten years, why not".

So this module prints the SHA-256 fingerprint on generation: that is the value
a user compares when trusting the certificate on another machine, and without
it "just trust it" is indistinguishable from trusting an interceptor.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# macOS rejects a TLS server certificate whose validity exceeds 825 days, even
# a manually trusted one. A longer life would look fine on Linux and silently
# fail on the platform most likely to need a trusted cert.
_MAX_DAYS = 825

_CERT_NAME = "manager-cert.pem"
_KEY_NAME = "manager-key.pem"


def material_dir(root: str) -> str:
    return os.path.join(root, "tls")


def fingerprint(cert_path: str) -> str:
    """SHA-256 fingerprint, colon-separated, as every client UI displays it."""
    with open(cert_path, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()


def _san_entries(host: str) -> list[x509.GeneralName]:
    """Every name a client might legitimately use to reach this manager.

    A certificate is checked against the name the CLIENT typed, not the address
    the server bound, so a cert valid only for the bind address fails for
    anyone connecting by hostname. 0.0.0.0 is not a connectable name at all --
    it means "all interfaces" -- so it is expanded rather than embedded.
    """
    names: list[str] = ["localhost"]
    ips: list[str] = ["127.0.0.1", "::1"]
    if host and host not in ("0.0.0.0", "::", "localhost"):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            names.append(host)
        else:
            ips.append(host)
    try:
        hostname = socket.gethostname()
        if hostname:
            names.append(hostname)
            # The .local form is what Bonjour/mDNS clients actually use.
            if "." not in hostname:
                names.append(f"{hostname}.local")
    except OSError:
        pass

    out: list[x509.GeneralName] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(x509.DNSName(name))
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            try:
                out.append(x509.IPAddress(ipaddress.ip_address(ip)))
            except ValueError:
                pass
    return out


def _covers(cert_path: str, host: str) -> bool:
    """Does an existing certificate still cover this host, and is it unexpired?

    Checked because a cert is generated for the host in use at the time, and
    changing --host would otherwise leave a stale cert that fails validation
    with an error naming the wrong problem.
    """
    try:
        with open(cert_path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
    except (OSError, ValueError):
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    if cert.not_valid_after_utc <= now:
        return False
    if not host or host in ("0.0.0.0", "::"):
        return True
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    try:
        return ipaddress.ip_address(host) in san.get_values_for_type(x509.IPAddress)
    except ValueError:
        return host in san.get_values_for_type(x509.DNSName)


def ensure_self_signed(root: str, host: str = "") -> tuple[str, str]:
    """Return (cert_path, key_path), generating them under <root>/tls if needed.

    Reused across restarts rather than regenerated. Werkzeug's ssl_context
    ="adhoc" mints a fresh certificate every start, which means every client
    sees a new untrusted identity on every restart -- unusable for anything a
    user has to trust once. Persisting is what makes trusting it meaningful.
    """
    directory = material_dir(root)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    cert_path = os.path.join(directory, _CERT_NAME)
    key_path = os.path.join(directory, _KEY_NAME)

    if os.path.exists(cert_path) and os.path.exists(key_path):
        if _covers(cert_path, host):
            return cert_path, key_path

    # P-256 rather than RSA: every client in section 3 of doc/WEBDAV.md
    # supports it, keys generate in microseconds instead of seconds, and the
    # handshake is cheaper on the small hardware this tends to run on.
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "aloelite manager"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "aloelite (self-signed)"),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # Backdated a little: a client whose clock runs behind the server's
        # would otherwise reject a certificate minted seconds ago.
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_MAX_DAYS))
        .add_extension(x509.SubjectAlternativeName(_san_entries(host)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Write the KEY with a restrictive mode from the start, not chmod after:
    # between create and chmod it would be world-readable, and this is the one
    # file whose disclosure undoes the whole feature.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(cert_path, 0o644)
    return cert_path, key_path


__all__ = ["ensure_self_signed", "fingerprint", "material_dir"]
# Copyright Michael Godfrey 2026 | aloecraft.org <michael@aloecraft.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
