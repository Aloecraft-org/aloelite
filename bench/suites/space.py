# ./bench/suites/space.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Where the bytes go: what a volume costs on disk relative to what it holds,
what deduplication actually returns, what a version costs, and what a prune
gives back.

These are the rows that answer "what is aloelite good FOR" rather than "how
fast is it". A content-addressed pool inside a transactional database is a
poor trade for one big unique file and an excellent one for fifty
template-cloned images, and only the space rows show that.

Surface
-------
Entry points
  space           on-disk over logical, for three corpus shapes x three modes
  dedup           within-volume and ACROSS-volume, plain vs encrypted
  versions        bytes added by a small edit to a large file
  prune_reclaim   freed pages vs actual file shrink after VACUUM

Configurable values
  CORPORA         the three corpus shapes and what each stands for
  EDIT_SIZES      the edit widths `versions` sweeps
  RETAIN          versions kept before prune_content may collect

Fan-out points
  CORPORA and corpus.VOLUME_MODES are the two axes; every entry point here
  iterates one or both and nothing else branches.
"""

from __future__ import annotations

from pathlib import Path

from ..corpus import (
    VOLUME_MODES,
    block_source,
    chunk_pool_stats,
    engine_session,
    on_disk_bytes,
    page_accounting,
    payload,
    sparse_image,
)
from ..harness import MiB, Row, Run, timed

# What each corpus is standing in for. The ratio a user gets is entirely
# decided by which of these their data looks like, which is the finding.
CORPORA = {
    "unique": "incompressible, undedupable — the worst case",
    "cloned": "CLONES copies of one template — a template-cloned tree",
    "sparse": "5% live blocks in zeros — a fresh qemu disk image",
}
# How many entries the cloned corpus spreads one template over. The dedup
# ceiling is exactly 1/CLONES, which is what makes the measured ratio
# checkable rather than merely plausible.
CLONES = 8
EDIT_SIZES = (1, 4096, 1 << 20)
RETAIN = 1


def _write_corpus(mount, path: str, kind: str, size: int) -> int:
    """Lay `size` logical bytes of `kind` into the mount; returns the logical
    total. `cloned` is CLONES separate ENTRIES holding one template, not one
    file of repeating bytes: a golden image copied eight times is the
    workload, and it is not the same thing as a file that compresses well.
    """
    if kind == "cloned":
        template = payload(max(1, size // CLONES), kind="unique", seed=77)
        stem = path.rsplit(".", 1)[0]
        for i in range(CLONES):
            mount.create_entry(f"{stem}-{i}.bin", template)
        return len(template) * CLONES

    blocks = sparse_image(size) if kind == "sparse" else block_source(size, "unique")
    written = 0
    with mount.open_write(path) as fh:
        for block in blocks:
            fh.write(block)
            written += len(block)
    return written


def space(run: Run, scratch: Path, cfg: dict) -> None:
    """On-disk bytes over logical bytes. Below 1.0 the pool is winning;
    above it, the pool plus sqlite's own page overhead is the price."""
    size = cfg["space_corpus_mb"] * MiB
    for mode in VOLUME_MODES:
        for kind, why in CORPORA.items():
            fs_file = scratch / f"space-{mode}-{kind}.fs"
            with engine_session(fs_file, mode) as (fs, mount):
                logical = _write_corpus(mount, f"/{kind}.bin", kind, size)
                disk = on_disk_bytes(fs, fs_file)
                pool = chunk_pool_stats(fs)
            run.add(
                Row(
                    suite="space",
                    metric=f"on_disk_over_logical_{kind}",
                    unit="ratio",
                    value=disk / logical if logical else 0.0,
                    frontend="direct",
                    volume=mode,
                    detail={
                        "logical_bytes": float(logical),
                        "on_disk_bytes": float(disk),
                        **{k: float(v) for k, v in pool.items()},
                    },
                    note=why,
                )
            )


def dedup(run: Run, scratch: Path, cfg: dict) -> None:
    """Two questions, and the second is the one people get wrong.

    WITHIN a volume: the same bytes written twice cost once, in every mode
    except `random` — which sacrifices dedup on purpose, so a ratio of 1.0
    there is the feature working, not failing.

    ACROSS volumes in the same file: the chunk pool is one table, and the
    address is taken over the CIPHERTEXT that is actually stored. A plain
    volume's ciphertext is its plaintext, so two plain volumes share chunks.
    An encrypted volume's chunk cipher is domain-separated by volume id, so
    two encrypted volumes CANNOT alias even when they hold identical bytes.
    That is the key-separation property, and this is the row that confirms it
    rather than asserting it in a document.
    """
    size = cfg["space_corpus_mb"] * MiB
    corpus = payload(size, kind="unique", seed=99)

    for mode in VOLUME_MODES:
        spec = VOLUME_MODES[mode]
        fs_file = scratch / f"dedup-{mode}.fs"
        from aloelite.aloelite import Aloelite

        fs = Aloelite(fs_file)
        try:
            vol_a = fs.create_volume("a", pin=spec["pin"], enc_mode=spec["enc_mode"])
            vol_b = fs.create_volume("b", pin=spec["pin"], enc_mode=spec["enc_mode"])

            with fs.mount(vol_a.id, pin=spec["pin"]) as a:
                a.create_entry("/first.bin", corpus)
                after_first = on_disk_bytes(fs, fs_file)
                a.create_entry("/second.bin", corpus)
                after_second = on_disk_bytes(fs, fs_file)
            within = after_second - after_first

            with fs.mount(vol_b.id, pin=spec["pin"]) as b:
                b.create_entry("/same.bin", corpus)
                after_cross = on_disk_bytes(fs, fs_file)
            across = after_cross - after_second
            pool = chunk_pool_stats(fs)
        finally:
            fs.close()

        for metric, grew, note in (
            (
                "dedup_within_volume",
                within,
                "same bytes, second entry, same volume",
            ),
            (
                "dedup_across_volumes",
                across,
                "same bytes, second VOLUME in the same file",
            ),
        ):
            # 1.0 = stored nothing (perfect dedup); 0.0 = stored it all again.
            run.add(
                Row(
                    suite="dedup",
                    metric=metric,
                    unit="ratio",
                    value=max(0.0, 1.0 - grew / size),
                    frontend="direct",
                    volume=mode,
                    detail={
                        "bytes_added": float(grew),
                        "logical_bytes": float(size),
                        **{k: float(v) for k, v in pool.items()},
                    },
                    note=note,
                )
            )


def versions(run: Run, scratch: Path, cfg: dict) -> None:
    """What a small edit to a large file costs.

    write_range carries every untouched chunk into the new version BY
    REFERENCE, so the floor is the chunk that was touched — not the file.
    A one-byte edit to a 1 GB file costing one chunk instead of a gigabyte is
    the whole argument for the manifest design, and it is measurable here.
    """
    size = cfg["space_corpus_mb"] * MiB
    for mode in ("plain", "convergent"):
        fs_file = scratch / f"versions-{mode}.fs"
        with engine_session(fs_file, mode) as (fs, mount):
            chunk = fs.db.chunk_size_of(str(mount.info().volume))
            _write_corpus(mount, "/big.bin", "unique", size)
            base = on_disk_bytes(fs, fs_file)
            for width in EDIT_SIZES:
                if width > size:
                    continue
                edit = payload(width, kind="unique", seed=width)
                elapsed = timed(lambda: mount.write_range("/big.bin", 4096, edit))
                now = on_disk_bytes(fs, fs_file)
                grew, base = now - base, now
                run.add(
                    Row(
                        suite="versions",
                        metric=f"bytes_per_version_edit_{width}B",
                        unit="bytes",
                        value=float(grew),
                        frontend="direct",
                        volume=mode,
                        n=1,
                        detail={
                            "file_bytes": float(size),
                            "chunk_size": float(chunk),
                            "chunks_touched": grew / chunk if chunk else 0.0,
                            "elapsed_ms": elapsed * 1e3,
                        },
                        note=f"{width} B edit in a {cfg['space_corpus_mb']} MiB file",
                    )
                )


def prune_reclaim(run: Run, scratch: Path, cfg: dict) -> None:
    """Prune frees PAGES; only VACUUM returns BYTES.

    Reported as two numbers on purpose. A user who prunes and then runs `ls`
    sees no change and concludes nothing happened; what happened is that the
    pages went on the freelist and will be reused by the next write. The
    file only shrinks when it is rewritten.
    """
    size = cfg["space_corpus_mb"] * MiB
    for mode in ("plain", "convergent"):
        fs_file = scratch / f"prune-{mode}.fs"
        with engine_session(fs_file, mode) as (fs, mount):
            volume = str(mount.info().volume)
            _write_corpus(mount, "/churn.bin", "unique", size)
            mount.set_retention("/churn.bin", RETAIN)
            # Rewrite the whole file a few times so there is genuinely
            # superseded content to collect, not just a bookkeeping row.
            for r in range(3):
                with mount.open_write("/churn.bin") as fh:
                    for block in block_source(size, kind="unique", seed=500 + r):
                        fh.write(block)
            before = on_disk_bytes(fs, fs_file)
            before_pages = page_accounting(fs)

            nodes = timed(lambda: fs.prune(volume))
            content_report = None

            def _prune_content():
                nonlocal content_report
                content_report = fs.prune_content(volume)

            content = timed(_prune_content)

            after_pages = page_accounting(fs)
            after_prune = on_disk_bytes(fs, fs_file)
            vacuum = timed(lambda: fs.db.connection.execute("VACUUM"))
            after_vacuum = on_disk_bytes(fs, fs_file)

        page_size = after_pages["page_size"]
        freed_pages = after_pages["freelist_count"] - before_pages["freelist_count"]
        run.add(
            Row(
                suite="prune",
                metric="prune_freed_pages",
                unit="bytes",
                value=float(freed_pages * page_size),
                frontend="direct",
                volume=mode,
                detail={
                    "freelist_pages": float(after_pages["freelist_count"]),
                    "page_size": float(page_size),
                    "versions_pruned": float(content_report.versions_pruned),
                    "chunks_pruned": float(content_report.chunks_pruned),
                    "prune_ms": nodes * 1e3,
                    "prune_content_ms": content * 1e3,
                },
                note="space made reusable inside the file — `ls` will not show it",
            )
        )
        run.add(
            Row(
                suite="prune",
                metric="vacuum_file_shrink",
                unit="bytes",
                value=float(before - after_vacuum),
                frontend="direct",
                volume=mode,
                detail={
                    "before_bytes": float(before),
                    "after_prune_bytes": float(after_prune),
                    "after_vacuum_bytes": float(after_vacuum),
                    "vacuum_ms": vacuum * 1e3,
                },
                note="bytes actually returned to the filesystem",
            )
        )


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
