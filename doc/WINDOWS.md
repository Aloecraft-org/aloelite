# Running Aloelite on Windows

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) | [WebDAV](/doc/WEBDAV.md) | [S3](/doc/S3.md)
</div>

FUSE is Linux-only, so on Windows the manager **is** the product: `aloelite-web`
serves volumes over the browser UI, WebDAV, and S3 without a kernel mount,
without Docker and without administrator rights.

> **Status: exercised once on a real Windows host (2026-08-31), still not
> covered by CI** — there is no Windows runner, so nothing here is protected
> against regression.
>
> What has actually run, on Windows 11 with CPython 3.14: the manager starts
> as a scheduled task, a WebDAV volume mounts in Explorer as a drive letter,
> and the S3 frontend passes `script/s3_smoke.py` end to end — put/get, a deep
> key's implied prefixes, flat and delimited listing, a multipart upload past
> the 5 MiB part size, and a batch delete.
>
> What has NOT been exercised: sustained load, large files through the mapped
> drive, an encrypted volume over WebDAV, TLS, binding off loopback, and a
> real litestream replica cycle. Treat those as untested.

---

## 0. Check SQLite first. Everything else is downstream.

This is the one thing that will stop you cold, and it takes ten seconds:

```powershell
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

**You need >= 3.45.** Aloelite probes for `jsonb()` and `unixepoch('subsec')`
at open and refuses to start without them — deliberately, because the
alternative is a `NOT NULL` violation or silent zero timestamps much later.

If the version is too old, the Linux rescue does **not** apply:
`aloelite[bundled-sqlite]` resolves to `pysqlite3-binary`, which publishes
manylinux wheels only, so on Windows that extra installs nothing. Your options
are:

1. **Install a newer CPython.** The bundled `sqlite3.dll` tracks the release;
   a current 3.12 or 3.13 installer is the easy fix.
2. **Replace `sqlite3.dll`** in your Python installation's `DLLs\` directory
   with a current build from [sqlite.org](https://sqlite.org/download.html)
   (the `sqlite-dll-win-x64-*.zip` precompiled binary).

## 1. Install

> **On the Python version:** `requires-python` is `>=3.11` and CI covers 3.11
> and 3.12 only. Newer interpreters (3.13, 3.14) install fine and are expected
> to work, but they are genuinely untested — if something behaves oddly, the
> interpreter version is worth mentioning in the report.

```powershell
py -m venv C:\aloelite\venv
C:\aloelite\venv\Scripts\pip install aloelite
```

Do **not** install the `fuse` extra — `pyfuse3` has no Windows build and is
not needed.

## 1b. If `aloelite` is "not recognized"

Expected, and not a broken install. `pip install` puts `aloelite.exe` in the
interpreter's `Scripts\` directory, which on Windows is routinely not on
PATH. Either use the full path:

```powershell
C:\Users\<you>\AppData\Local\Python\pythoncore-3.14-64\Scripts\aloelite-web.exe --version
```

or skip the scripts entirely — every entry point is reachable with `-m`:

```powershell
python -m aloelite --version          # the CLI
python -m aloelite web --webdav       # the manager (same as aloelite-web)
python -m manager.web --webdav        # identical, spelled the other way
```

## 2. Run it

```powershell
$env:ALOELITE_S3            = "1"
$env:ALOELITE_S3_ACCESS_KEY = "AKIALOCALBACKUP"
$env:ALOELITE_S3_SECRET_KEY = "<a long random string>"

C:\aloelite\venv\Scripts\aloelite-web --webdav --host 127.0.0.1 --root C:\aloelite\data
```

Data lands in `--root` (default `%USERPROFILE%\.aloelite`), one `.sqlite` file
per filesystem.

### Why `--host 127.0.0.1`, and what to do about the LAN

Binding off loopback with `--webdav` is **refused**, not warned about: WebDAV
authenticates with HTTP Basic where the password is the volume PIN, so plain
HTTP would put that PIN on the wire in base64 on every request — and one
directory listing is dozens of requests.

That refusal interacts badly with your goal, so pick deliberately:

| You want | Do this |
| --- | --- |
| Browse WebDAV **from the Windows box itself** | Keep `--host 127.0.0.1`. No TLS, no cert trust, nothing to configure. |
| Browse WebDAV **from another machine** | Needs TLS *and* a certificate that machine trusts — see below. |
| S3 reachable **on the LAN/VPN** | See "Splitting the two surfaces". |

**`--tls-self-signed` will not satisfy Windows Explorer.** The WebDAV
redirector refuses untrusted certificates outright (the `--tls-cert` help says
so). To mount from another Windows machine you must import the certificate
into that machine's **Trusted Root Certification Authorities** store, or use a
cert from a CA it already trusts.

Also note: on Windows the private key is **not** written with `0600`
protection. `manager/tls.py` opens it with that mode, but Windows ignores
POSIX permission bits — the key inherits the directory's ACLs instead. Put
`--root` somewhere only your account can read.

### Splitting the two surfaces

The refusal is about WebDAV's Basic auth, not about S3. SigV4 never puts the
secret on the wire — it sends a signature — so an S3-only listener off
loopback is a materially different risk from a WebDAV one. It is still
**plaintext data**, so prefer TLS or a VPN-only interface.

The simplest safe arrangement for what you described is two processes:

```powershell
# 1. WebDAV + UI, loopback only, browsed from this machine.
aloelite-web --webdav --host 127.0.0.1 --root C:\aloelite\data

# 2. S3 only, reachable on the VPN. No --webdav, so no Basic-auth refusal.
$env:ALOELITE_S3 = "1"
aloelite-web --host 0.0.0.0 --port 9000 --root C:\aloelite\data
```

**They must not share a `--root`.** Two manager processes over one
`volumes.json` and one set of `.sqlite` files is not a supported
configuration — the store's own docstring says only one manager process
touches it. Give each its own root, or run a single process and accept
loopback-only WebDAV.

## 3. Point litestream at the S3 surface

Create the bucket first — it is a volume, and there is no `CreateBucket`:

```powershell
curl.exe -X POST http://127.0.0.1:8080/volumes -H "Content-Type: application/json" `
  -d '{\"name\": \"backups\", \"encrypted\": false}'
```

Then, in `litestream.yml` on the machine being backed up:

```yaml
      - url:      s3://backups/df
        endpoint: http://<windows-host>:9000
        region:   us-east-1
        force-path-style: true      # REQUIRED
        access-key-id:     AKIALOCALBACKUP
        secret-access-key: <the same long random string>
```

`force-path-style: true` is not optional — litestream does not infer it from
`endpoint`. See [S3.md](/doc/S3.md) for why.

## 4. Firewall

Windows Firewall blocks inbound on a new listener by default. Allow the S3
port for the private/VPN profile only:

```powershell
New-NetFirewallRule -DisplayName "aloelite S3" -Direction Inbound `
  -LocalPort 9000 -Protocol TCP -Action Allow -Profile Private
```

## Mounting WebDAV from Explorer

**An unencrypted volume needs no credentials at all** — `dav.py`'s
`_session_token` returns `None` for a plain volume — so on loopback it mounts
with no TLS, no certificate, and no registry changes:

### The short version

**File Explorer → This PC → Map network drive**, tick *Reconnect at sign-in*,
and paste:

```
http://127.0.0.1:7081/dav/backups
```

That is the whole thing. The URL takes the volume's **name** — you do not need
its id.

**Set `WebClient` to start automatically first.** It ships as trigger-start and
stops when idle, and a mapping attempted while it is stopped fails with
`System error 67 - The network name cannot be found`, which names nothing
useful. Once, from an elevated prompt:

```powershell
Set-Service WebClient -StartupType Automatic
Start-Service WebClient
```

Starting it by hand works for the moment but not across a reboot, which is the
case that matters for a drive you want reconnected at sign-in.

The command-line equivalent, if you prefer:

```powershell
net use Z: \\127.0.0.1@7081\DavWWWRoot\dav\backups /persistent:yes
```

Pair that with a scheduled task that starts the manager at logon and you are
done — the *Reconnect at sign-in* tick and the task are the two moving parts,
and Windows reconnects the drive on its own once the server answers.

The id still works and is still what `GET /volumes` reports; the name is
simply the form a person can type. Where a name is ambiguous (names are unique
only within a filesystem, so two filesystems may each hold a `backups`), the
server refuses with a 409 naming the ids rather than guessing.

The `@7081` in the UNC form is the port — without it Windows tries the name as
SMB first and takes ~30s to fail over. The Explorer dialog wants the plain URL
instead, which is why it is the easier of the two.

To make it survive a reboot, add `/persistent:yes` — though the drive will
show as disconnected until the manager is running again.

### The 50 MB ceiling, which will bite a filesystem

Windows' WebClient caps a single transfer at **`FileSizeLimitInBytes`, which
defaults to 50,000,000 bytes** (~47 MiB). Past that a copy fails with a
generic error that says nothing about size. For a filesystem that is a low
ceiling, so raise it (4 GB is the maximum) and restart the service:

```powershell
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\WebClient\Parameters" `
  -Name FileSizeLimitInBytes -Value 0xFFFFFFFF -Type DWord
Restart-Service WebClient
```

This is a Windows client limit, not an aloelite one — the same cap applies to
any WebDAV server.

An **encrypted** volume authenticates with HTTP Basic where the password is
the PIN, and Windows refuses Basic over plain HTTP by default
(`BasicAuthLevel` defaults to SSL-only). So an encrypted volume needs either
TLS with a certificate the machine trusts, or that registry value raised —
which is a worse trade than simply using an unencrypted volume on a machine
you already trust, or browsing it through the web UI instead.

### Two traps, both costing a round trip

**A drive mapped from an elevated prompt is invisible to Explorer.** Windows
maps drives per *logon session*, and an elevated session is a different one
from your normal token. `net start WebClient` genuinely needs admin; `net use`
must be run as your ordinary user or Explorer will never see it. (Setting
`EnableLinkedConnections` to 1 under
`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System` shares
mappings between the two contexts, but that is a broad change for one drive.)

**"New connections will be remembered" means the mapping comes back at logon
— before the manager is listening.** Windows then leaves it marked
disconnected with a red X, and Explorer stalls probing it. Map test drives
with `/persistent:no` and let the logon task below do the real thing.

To undo a mapping, delete it in *both* sessions — the live mapping is
per-session even though the remembered record is shared:

```powershell
net use Z: /delete          # in each window that mapped it
net use                     # should list nothing
Get-ChildItem HKCU:\Network  # a leftover 'Z' key is the remembered record
```

## Starting at boot

`script/windows/Install-AloeliteTasks.ps1` registers two scheduled tasks. Two
rather than one, because they answer to different owners:

- **The manager** must run whether or not anyone is logged in — backups arrive
  on their own schedule — so it runs **at system startup as SYSTEM**. That is
  why `-Root` is mandatory: as SYSTEM, the default `~/.aloelite` resolves into
  SYSTEM's profile, not yours.
- **The drive mapping** is per-logon-session by construction, so SYSTEM cannot
  create one for you. It runs **at logon, as you, unelevated**, and waits for
  the manager's `/health` to answer before mapping — which is what stops the
  dead-drive-at-logon problem above.

From an elevated PowerShell:

```powershell
# Keep the secret out of the task definition and the environment.
"a long random string" | Set-Content C:\aloelite\s3-secret.txt

.\script\windows\Install-AloeliteTasks.ps1 `
    -Python C:\aloelite\venv\Scripts\python.exe `
    -Root C:\aloelite\data `
    -VolumeId <volume-id> `
    -Port 7081 `
    -AccessKey AKIALOCALBACKUP `
    -SecretFile C:\aloelite\s3-secret.txt
```

Then, without rebooting:

```powershell
Start-ScheduledTask -TaskName Aloelite-Manager
Start-ScheduledTask -TaskName Aloelite-MapDrive
```

### Letting other machines ship backups to it

The default binds loopback, which means S3 is reachable **only from this
machine** — fine for a local test, useless for catching backups. To let other
hosts reach it, one manager must bind off loopback:

```powershell
.\script\windows\Install-AloeliteTasks.ps1 ... -BindHost 10.x.x.x -Insecure
```

One manager, not two. Splitting S3 and WebDAV into separate processes would
need two `--root`s (the store supports a single writer), which means two
separate volume sets — so backups would land somewhere the mapped drive cannot
see them.

`-Insecure` is required because `--webdav` off loopback without TLS is refused.
The refusal's reason is specific and worth understanding before overriding it:
WebDAV authenticates with HTTP Basic where the password is the volume PIN.
**An unencrypted volume issues no Basic challenge at all** (`dav.py`'s
`_session_token` returns `None` for a plain volume), so there is no PIN to
leak and `-Insecure` costs nothing the transport did not already cost. On an
**encrypted** volume it puts the PIN on the wire in base64 on every request —
use `--tls-cert` with a trusted certificate instead.

Object data still crosses the network in the clear either way, so use a VPN
address rather than `0.0.0.0` where you can, and remember the firewall rule
(the installer prints it when you bind off loopback).

### If PowerShell refuses to run the script

`running scripts is disabled on this system` is the default execution policy,
not a problem with the file. Run it for this one invocation without changing
anything system-wide:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\Install-AloeliteTasks.ps1 -Python ... 
```

or loosen it for the current window only (reverts when you close it):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

If the file came from another machine it may also carry the mark-of-the-web:
`Unblock-File .\Install-AloeliteTasks.ps1`.

It generates the two scripts it runs into `C:\aloelite\bin` rather than
shipping them, so the paths inside are yours and you can read exactly what
starts. Omit `-AccessKey`/`-SecretFile` for a WebDAV-only manager. Remove
everything with:

```powershell
Unregister-ScheduledTask -TaskName Aloelite-Manager,Aloelite-MapDrive -Confirm:$false
```

## What to watch on the first run

These are the places a Linux-only assumption would surface. None of them is
known broken; none has been exercised on a real Windows host either.

- **Startup.** `aloelite-web` defaults to direct-only mode, which skips every
  FUSE precondition — `/dev/fuse`, `CAP_SYS_ADMIN`, `/proc/self/mountinfo`,
  `fusermount3` are never probed. `os.geteuid` is guarded (it crashed the
  entrypoint before 0.3.6). A crash *before the socket binds* is the signature
  of a POSIX call we missed.
- **Path separators.** Volume paths inside a volume are always `/`; only
  `--root` and the `.sqlite` files touch Windows paths. A `\` appearing inside
  a volume listing is a bug.
- **File locking.** SQLite WAL works on Windows, but Windows locks files more
  eagerly than POSIX. Deleting or exporting a volume while it is mounted is
  the likeliest place to see a difference.
- **Ctrl-C.** Port release on shutdown is handled explicitly; a port left in
  `TIME_WAIT` that blocks a restart is worth reporting.
- **Long paths.** Windows caps paths at 260 characters unless long-path
  support is enabled. Litestream's keys are deep
  (`generations/<id>/wal/<index>/<offset>.wal.lz4`) but they live *inside* the
  `.sqlite` file, not on disk, so only `--root` counts against the limit. Keep
  it short — `C:\aloelite\data`, not somewhere under `Documents`.

If any of this bites, the fix belongs in `manager/test_portability.py` first —
that file exists so a Linux CI run can catch the next one.
