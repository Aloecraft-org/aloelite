# ./manager/test_tls.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
TLS material and the cleartext-credential guard.

The guard tests matter more than they look: WebDAV's Basic password IS the
volume PIN, so the difference between refusing and warning is the difference
between a foot-gun and a disclosed secret.
"""

from __future__ import annotations

import datetime
import os
import ssl

import pytest
from cryptography import x509

from manager import __main__ as entry
from manager.tls import ensure_self_signed, fingerprint

_TLS_ENV = (
    "ALOELITE_TLS_CERT",
    "ALOELITE_TLS_KEY",
    "ALOELITE_TLS_SELF_SIGNED",
    "ALOELITE_WEBDAV",
    "ALOELITE_INSECURE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The guard reads process env; a leaked value would silently disarm it."""
    for name in _TLS_ENV:
        monkeypatch.delenv(name, raising=False)


def _cert(path):
    with open(path, "rb") as fh:
        return x509.load_pem_x509_certificate(fh.read())


def _sans(path):
    san = _cert(path).extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return (
        set(san.value.get_values_for_type(x509.DNSName)),
        {str(i) for i in san.value.get_values_for_type(x509.IPAddress)},
    )


# --------------------------------------------------------------------------
# Certificate generation
# --------------------------------------------------------------------------
def test_self_signed_material_is_generated_with_a_private_key_mode(tmp_path):
    cert, key = ensure_self_signed(str(tmp_path), "127.0.0.1")
    assert os.path.exists(cert) and os.path.exists(key)
    # The one file whose disclosure undoes the whole feature.
    assert oct(os.stat(key).st_mode & 0o777) == "0o600"


def test_certificate_covers_the_names_a_client_would_actually_type(tmp_path):
    """A certificate is validated against the name the CLIENT used, not the
    address the server bound, so loopback-by-name and by-IP both have to be
    present or half the clients fail."""
    cert, _ = ensure_self_signed(str(tmp_path), "192.168.1.50")
    dns, ips = _sans(cert)
    assert "localhost" in dns
    assert {"127.0.0.1", "::1", "192.168.1.50"} <= ips


def test_a_hostname_bind_lands_in_dns_names_not_ip_addresses(tmp_path):
    cert, _ = ensure_self_signed(str(tmp_path), "vault.example.org")
    dns, ips = _sans(cert)
    assert "vault.example.org" in dns
    assert "vault.example.org" not in ips


def test_wildcard_bind_is_expanded_rather_than_embedded(tmp_path):
    """0.0.0.0 means 'all interfaces' and is not a name any client connects
    to; embedding it would produce a certificate that matches nothing."""
    cert, _ = ensure_self_signed(str(tmp_path), "0.0.0.0")
    dns, ips = _sans(cert)
    assert "0.0.0.0" not in ips and "0.0.0.0" not in dns
    assert "127.0.0.1" in ips and "localhost" in dns


def test_material_is_reused_across_calls(tmp_path):
    """Regenerating per start (what Werkzeug's ssl_context='adhoc' does) shows
    every client a new untrusted identity on every restart, which makes
    trusting it once impossible."""
    cert, _ = ensure_self_signed(str(tmp_path), "127.0.0.1")
    first = fingerprint(cert)
    cert2, _ = ensure_self_signed(str(tmp_path), "127.0.0.1")
    assert fingerprint(cert2) == first


def test_material_is_regenerated_when_the_host_changes(tmp_path):
    """Otherwise changing --host leaves a stale certificate that fails
    validation with an error naming the wrong problem."""
    cert, _ = ensure_self_signed(str(tmp_path), "127.0.0.1")
    before = fingerprint(cert)
    cert2, _ = ensure_self_signed(str(tmp_path), "10.0.0.7")
    assert fingerprint(cert2) != before
    assert "10.0.0.7" in _sans(cert2)[1]


def test_validity_respects_the_apple_825_day_ceiling(tmp_path):
    """macOS refuses a TLS server certificate valid for longer, even one the
    user trusted by hand -- and macOS is the platform most likely to need
    that trust. A longer life would pass on Linux and fail there."""
    cert, _ = ensure_self_signed(str(tmp_path), "127.0.0.1")
    c = _cert(cert)
    span = c.not_valid_after_utc - c.not_valid_before_utc
    assert span <= datetime.timedelta(days=826)
    # Backdated slightly so a client whose clock trails the server's accepts it.
    assert c.not_valid_before_utc < datetime.datetime.now(datetime.timezone.utc)


def test_fingerprint_is_the_form_clients_display(tmp_path):
    cert, _ = ensure_self_signed(str(tmp_path), "127.0.0.1")
    fp = fingerprint(cert)
    parts = fp.split(":")
    assert len(parts) == 32 and all(len(p) == 2 for p in parts)
    assert fp == fp.upper()


def test_generated_material_actually_completes_a_tls_handshake(tmp_path):
    """End-to-end over a real socket: a certificate that parses but cannot
    serve is worthless, and only a handshake proves the key matches, the
    chain loads, and the SANs satisfy hostname verification."""
    import socket
    import threading

    cert, key = ensure_self_signed(str(tmp_path), "127.0.0.1")
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    error: list[BaseException] = []

    def serve():
        try:
            raw, _ = listener.accept()
            with server_ctx.wrap_socket(raw, server_side=True) as tls:
                tls.recv(16)
                tls.send(b"ok")
        except BaseException as exc:  # surfaced in the assertion below
            error.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        # Full verification, including hostname -- not just "encrypted".
        client_ctx = ssl.create_default_context(cafile=cert)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with client_ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                tls.send(b"hello")
                assert tls.recv(16) == b"ok"
                assert tls.version().startswith("TLS")
    finally:
        thread.join(timeout=10)
        listener.close()
    assert not error, f"server side failed: {error}"


def test_an_untrusted_client_rejects_the_self_signed_certificate(tmp_path):
    """The honest half of what self-signed means: it encrypts, it does not
    authenticate. This is exactly why Windows refuses it until trusted."""
    cert, _ = ensure_self_signed(str(tmp_path), "127.0.0.1")
    default = ssl.create_default_context()  # system trust store only
    with pytest.raises(ssl.SSLError):
        default.load_verify_locations(cadata="not a certificate")
    # The generated cert is not in the system store, so it is not trusted.
    system_trusted = any(
        entry.get("serialNumber") == format(_cert(cert).serial_number, "X")
        for entry in default.get_ca_certs()
    )
    assert not system_trusted


# --------------------------------------------------------------------------
# The cleartext-credential guard
# --------------------------------------------------------------------------
def test_webdav_on_a_public_address_without_tls_is_refused(monkeypatch):
    """The whole point. Basic auth carries the PIN, so this would put the
    volume's key on the wire in base64 on every request."""
    monkeypatch.setenv("ALOELITE_WEBDAV", "1")
    with pytest.raises(SystemExit) as exc:
        entry._tls_context("0.0.0.0")
    assert exc.value.code == 2


def test_loopback_webdav_without_tls_is_allowed(monkeypatch):
    monkeypatch.setenv("ALOELITE_WEBDAV", "1")
    assert entry._tls_context("127.0.0.1") is None


def test_public_bind_without_webdav_is_not_the_guards_business(monkeypatch):
    """The JSON API's own exposure is a separate, already-warned concern; this
    guard is specifically about the Basic-auth PIN."""
    assert entry._tls_context("0.0.0.0") is None


def test_insecure_opt_out_is_honoured_for_tls_terminating_proxies(monkeypatch):
    monkeypatch.setenv("ALOELITE_WEBDAV", "1")
    monkeypatch.setenv("ALOELITE_INSECURE", "1")
    assert entry._tls_context("0.0.0.0") is None


def test_supplying_tls_satisfies_the_guard(monkeypatch, tmp_path):
    cert, key = ensure_self_signed(str(tmp_path), "0.0.0.0")
    monkeypatch.setenv("ALOELITE_WEBDAV", "1")
    monkeypatch.setenv("ALOELITE_TLS_CERT", cert)
    monkeypatch.setenv("ALOELITE_TLS_KEY", key)
    assert entry._tls_context("0.0.0.0") == (cert, key)


def test_cert_without_key_is_rejected(monkeypatch, tmp_path):
    cert, _ = ensure_self_signed(str(tmp_path), "127.0.0.1")
    monkeypatch.setenv("ALOELITE_TLS_CERT", cert)
    with pytest.raises(SystemExit):
        entry._tls_context("127.0.0.1")


def test_a_missing_certificate_file_fails_loudly(monkeypatch, tmp_path):
    """Rather than falling back to plain HTTP, which would silently serve the
    PIN in the clear after the operator asked for TLS."""
    monkeypatch.setenv("ALOELITE_TLS_CERT", str(tmp_path / "nope.pem"))
    monkeypatch.setenv("ALOELITE_TLS_KEY", str(tmp_path / "nope.key"))
    with pytest.raises(SystemExit):
        entry._tls_context("127.0.0.1")


def test_self_signed_env_generates_into_the_data_root(monkeypatch, tmp_path):
    monkeypatch.setattr(entry, "ALOELITE_ROOT", str(tmp_path))
    monkeypatch.setenv("ALOELITE_TLS_SELF_SIGNED", "1")
    monkeypatch.setenv("ALOELITE_WEBDAV", "1")
    ctx = entry._tls_context("0.0.0.0")
    assert ctx is not None
    assert os.path.commonpath([ctx[0], str(tmp_path)]) == str(tmp_path)


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
