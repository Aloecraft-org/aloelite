# ./aloelite/transfer.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Volume transfer: copy a volume's tree between mounts, including across
files. Wrapper-level orchestration over the Mount API (two connections,
one per side — db.cipher is connection-scoped), NOT a flat operation:
nothing here touches SQL directly, so the conformance surface is
unchanged. v1 is plaintext-recrypt: bytes are decrypted under the source
cipher and re-encrypted under the destination's own key ladder. A
future `--sealed` (ciphertext-verbatim) variant is documented in the
admin roadmap.
"""

from __future__ import annotations

from .aloelite import Aloelite, Mount

_CHUNK = 1 << 20


def copy_tree(src: Mount, dst: Mount) -> tuple[int, int]:
    """Copy src's whole tree into dst's root. Streams content (bounded
    memory), carries metadata, and restores mtimes last (writes bump them).
    Returns (containers, entries) copied. dst is assumed empty; existing
    same-named nodes raise the normal engine errors rather than merging.
    """
    containers = 0
    entries = 0
    mtimes: list[tuple[str, int]] = []
    stack = [src.path("/")]
    while stack:
        d = stack.pop()
        for child in d.iterdir():
            sp = str(child)
            st = child.stat()
            if child.is_dir():
                dst.path(sp).mkdir()
                containers += 1
                stack.append(child)
            else:
                with child.open("rb") as r, dst.path(sp).open("wb") as w:
                    while chunk := r.read(_CHUNK):
                        w.write(chunk)
                entries += 1
            md = child.metadata
            if md:
                dst.path(sp).set_metadata(md)
            mtimes.append((sp, st.modified_at))
    for sp, ts in mtimes:
        if ts is not None:
            dst.set_mtime(dst.stat(sp).id, ts)
    return containers, entries


def export_volume(
    src_file: str,
    volume,
    dest_file: str,
    *,
    src_pin: bytes | None = None,
    dest_pin: bytes | None = None,
    dest_name: str | None = None,
    enc_mode: str = "convergent",
) -> tuple[str, int, int]:
    """Copy one volume from src_file into dest_file (created if missing).

    The destination volume gets its own fresh key ladder: encrypted iff
    `dest_pin` is given (fresh K_v — this is also a re-encryption).
    Returns (dest_volume_id, containers, entries).
    """
    with Aloelite(src_file) as sfs, Aloelite(dest_file) as dfs:
        name = dest_name
        if name is None:
            src_v = next((v for v in sfs.list_volumes() if v.id == volume), None)
            name = src_v.name if src_v is not None else None
        dv = dfs.create_volume(
            name, pin=dest_pin, ensure_unique=True, enc_mode=enc_mode
        )
        with sfs.mount(volume, pin=src_pin) as s, dfs.mount(dv.id, pin=dest_pin) as d:
            c, e = copy_tree(s, d)
    return str(dv.id), c, e


def snapshot_volume(
    file: str,
    volume,
    name: str,
    *,
    pin: bytes | None = None,
) -> tuple[str, int, int]:
    """Fork a volume within its own file as a new volume `name`.

    The snapshot mirrors the source's encryption choice: if `pin` is given
    (required for an encrypted source) the snapshot is encrypted under that
    same PIN — but with its OWN fresh K_v, so a later change_pin on either
    side diverges them cleanly. Plain-volume snapshots are near-free
    (identical bytes dedup to shared pool rows); encrypted snapshots are a
    full copy (chunk ciphers are domain-separated per volume id, so
    ciphertext never aliases across volumes — see the admin docs).
    Returns (snapshot_volume_id, containers, entries).
    """
    return export_volume(file, volume, file, src_pin=pin, dest_pin=pin, dest_name=name)


__all__ = ["copy_tree", "export_volume", "snapshot_volume"]
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
