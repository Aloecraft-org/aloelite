# Getting Started with Aloelite

Aloelite is a filesystem inside a single SQLite file. Files, folders,
metadata, and content all live in one portable file you can copy, back up,
or open anywhere.

Here's the whole idea in a few lines of Python:

```python
from aloelite import Aloelite

with Aloelite("notebook.fs") as fs:
    with fs.mount("myfiles", create=True) as m:
        m.put("/hello.txt", b"hello world")
        print(m.read_all("/hello.txt"))
```

That's it — `notebook.fs` is now a real filesystem with one file in it.
You don't need to touch Python again after this page if you don't want to.

---

## Pick your way in

There are several ways to use Aloelite. They all operate on the same file
format, so you can mix them freely — create a volume in Python, browse it
from the command line, mount it with FUSE for another app.

| You want to... | Use | Jump to |
|---|---|---|
| Script it, or use it inside an app | **Python API** | [Python](#python) |
| Work with files from the shell | **CLI** (`aloelite`) | [CLI](#cli) |
| Let *any* program use it as a normal folder | **FUSE** (`aloelite-fuse`) | [FUSE](#fuse) |
| Provide persistent volumes to Docker/Podman containers | **Volume Manager** | [Volume manager](#volume-manager) |
| Manage volumes from a browser | **Admin panel** (part of the manager) | [Volume manager](#volume-manager) |

A WebDAV interface is planned, for mounting volumes over the network
without FUSE.

**Two words you'll see everywhere:**

- A **volume** is a filesystem tree inside the file. One file can hold
  several volumes, each with its own root (and optionally its own
  encryption).
- A **mount** is your access point into a volume. All reading and writing
  goes through a mount.

---

## Python

Install:

```bash
pip install aloelite
```

Create, write, read:

```python
from aloelite import Aloelite

with Aloelite("notebook.fs") as fs:
    # create=True makes the volume on first run; later runs just open it
    with fs.mount("myfiles", create=True) as m:
        m.mkdir("/docs/2026", parents=True, exist_ok=True)
        m.put("/docs/2026/notes.txt", b"first note\n")
        m.put("/docs/2026/notes.txt", b"more\n", append=True)

        for entry in m.list("/docs/2026"):
            print(entry.path)

        print(m.read_all("/docs/2026/notes.txt"))
```

If you prefer pathlib style, any mount doubles as a path root:

```python
        note = m / "docs" / "2026" / "notes.txt"
        print(note.read_text())
        for txt in m.path("/").rglob("*.txt"):
            print(txt)
```

Large files stream with bounded memory:

```python
        with m.open_write("/big.bin") as w:
            for chunk in produce_chunks():
                w.write(chunk)
```

### Encryption

Pass a PIN when the volume is created, and again whenever you mount it:

```python
PIN = b"correct-horse-battery-staple"

with Aloelite("vault.fs") as fs:
    with fs.mount("vault", pin=PIN, create=True) as m:
        m.put("/secret.txt", b"eyes only")
```

The PIN is only ever used at mount time and is never stored. A volume's
encryption is decided once, at creation — so if you want it encrypted,
make sure the PIN is there on the *first* run.

---

## CLI

The `aloelite` command works on any Aloelite file. One command, one
operation:

```bash
aloelite -f notebook.fs volumes              # what's inside?
aloelite -f notebook.fs ls /
aloelite -f notebook.fs mkdir -p /docs/2026
aloelite -f notebook.fs put report.pdf /docs/report.pdf
aloelite -f notebook.fs get /docs/report.pdf ./copy.pdf
aloelite -f notebook.fs mv /docs/report.pdf /archive/report.pdf
aloelite -f notebook.fs rm -r /archive
```

Pipes work — `-` means stdin or stdout:

```bash
cat access.log | aloelite -f logs.fs put - /today.log --append
aloelite -f logs.fs get /today.log - | grep ERROR
```

If the file has exactly one volume, you're done. If it has several, pick
one with `-v` (by name or id):

```bash
aloelite -f notebook.fs -v vault ls /
```

For an encrypted volume, supply the PIN one of three ways (or just run
interactively and you'll be prompted):

```bash
aloelite -f vault.fs --pin-env VAULT_PIN ls /
aloelite -f vault.fs --pin-file ~/.vaultpin ls /
aloelite -f vault.fs --pin "my secret" ls /      # avoid on shared hosts
```

---

## FUSE

FUSE mounts a volume as a normal directory, so any program — editors,
rsync, your mail server — can use it without knowing Aloelite exists.
Linux only:

```bash
sudo apt install fuse3 libfuse3-dev
pip install aloelite[fuse]
```

Mount and use:

```bash
mkdir -p ~/photos
aloelite-fuse photos.fs photos ~/photos     # file, volume name, mountpoint

cp ~/Pictures/*.jpg ~/photos/               # just a directory now
ls ~/photos
```

Unmount:

```bash
fusermount3 -u ~/photos
```

Encrypted volumes take the same PIN flags as the CLI:

```bash
aloelite-fuse vault.fs vault ~/vault --pin-env VAULT_PIN
```

Large sequential copies stream with bounded memory, and random-access
programs work too — writes are committed in atomic batches.

---

## Volume manager

The manager is a container that hosts multiple volumes and exposes each
as a directory other containers can bind-mount. Use it when you want
Aloelite as a storage backend for services.

One-time host setup, then run:

```bash
sudo mkdir -p /aloelite-root /mnt/aloelite

docker run -d --privileged \
  -v /aloelite-root:/aloelite-root \
  -v /mnt/aloelite:/mnt:rshared \
  --device /dev/fuse \
  -p 8080:8080 \
  aloecraft/aloelite-manager
```

- `/aloelite-root` holds the backing `.sqlite` files (this is what you
  back up).
- `/mnt/aloelite` is where mounted volumes appear on the host.

Create and mount a volume over HTTP:

```bash
curl -s -X POST localhost:8080/volumes \
  -H 'Content-Type: application/json' -d '{"name": "mail"}'
# → {"id": "abc123...", ...}

curl -s -X POST localhost:8080/volumes/<id>/mount -d '{}' \
  -H 'Content-Type: application/json'

ls /mnt/aloelite/<id>              # it's a directory now
```

Consume it from another container like any host path:

```bash
docker run -d -v /mnt/aloelite/<id>:/var/mail my-mail-server
```

### Admin panel

Open `http://localhost:8080/admin` in a browser. You can create, mount,
and delete volumes, browse and upload files in mounted volumes, list the
durable mounts inside each file, and download a snapshot of any volume —
all without touching the API directly.

### Backups

Snapshots work while the volume is mounted and in use:

```bash
curl -s localhost:8080/volumes/<id>/export -o snapshot.sqlite
```

The export is a complete, self-contained SQLite file. A simple sync loop:
poll `GET /volumes/<id>/stat`, and export when `mtime` changes.

---

## Where to go next

- **README.md** — the full API surface, design discussion, and security
  notes (what's encrypted and what isn't).
- **TROUBLESHOOTING.md** — FUSE-in-container checklists, common errors,
  and what to do when a mount misbehaves.