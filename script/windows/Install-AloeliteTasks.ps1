<#
.SYNOPSIS
    Register the two scheduled tasks that make an aloelite instance survive a
    reboot on Windows: the manager at system startup, and the WebDAV drive at
    logon.

.DESCRIPTION
    Two tasks rather than one, because they answer to different owners.

    The MANAGER must run whether or not anybody is logged in -- backups arrive
    on their own schedule -- so it runs at system startup as SYSTEM. That is
    also why -Root is mandatory: as SYSTEM, the default ~/.aloelite resolves
    into SYSTEM's profile, not yours.

    A MAPPED DRIVE is per-logon-session by construction; SYSTEM cannot create
    one for you, and a drive mapped from an elevated prompt is invisible to
    your unelevated Explorer. So the mapping runs at logon, as you, and waits
    for the manager's port before it tries -- a persistent mapping restored
    before the server is up is exactly how you get a dead drive with a red X.

    The S3 secret is never written into the task or this script. It is read
    from -SecretFile through ALOELITE_S3_SECRET_FILE, so it stays out of the
    process environment where anything that can read the task definition (or
    a process listing) would find it.

.EXAMPLE
    .\Install-AloeliteTasks.ps1 `
        -Python C:\aloelite\venv\Scripts\python.exe `
        -Root C:\aloelite\data `
        -VolumeId d1c7975c82bc45d699632c265fbec8c7 `
        -SecretFile C:\aloelite\s3-secret.txt `
        -AccessKey AKIALOCALBACKUP

    Run from an ELEVATED PowerShell: registering a task that runs as SYSTEM
    requires it.
#>
[CmdletBinding()]
param(
    # Full path to the interpreter that has aloelite installed. Use the venv's
    # python.exe, not a bare "python" -- a scheduled task has no PATH worth
    # relying on.
    [Parameter(Mandatory = $true)][string]$Python,

    # Data directory. Mandatory because SYSTEM's profile is not yours.
    [Parameter(Mandatory = $true)][string]$Root,

    # The volume to map as a drive. Find it with:
    #   Invoke-RestMethod http://127.0.0.1:<port>/volumes | Format-Table id, name
    [Parameter(Mandatory = $true)][string]$VolumeId,

    [int]$Port = 7081,
    [string]$Drive = 'Z',

    # Bind address. The default keeps everything on this machine. To let other
    # hosts ship backups to the S3 surface, set this to the VPN address (or
    # 0.0.0.0) AND pass -Insecure -- see the note where the manager script is
    # generated for exactly what that trades away.
    [string]$BindHost = '127.0.0.1',
    [switch]$Insecure,

    # S3 (optional). Omit both to register a WebDAV-only manager.
    [string]$AccessKey,
    [string]$SecretFile,

    # Where the helper scripts get written. They are generated rather than
    # shipped so the paths inside them are yours, not placeholders.
    [string]$ScriptDir = 'C:\aloelite\bin',

    [string]$TaskPrefix = 'Aloelite'
)

$ErrorActionPreference = 'Stop'

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an elevated PowerShell: registering a SYSTEM task requires it."
    }
}

Assert-Elevated

if (-not (Test-Path $Python)) { throw "No interpreter at $Python" }
if ($AccessKey -and -not $SecretFile) { throw "-AccessKey needs -SecretFile" }
if ($SecretFile -and -not (Test-Path $SecretFile)) { throw "No secret file at $SecretFile" }

# Validate the paths HERE rather than letting New-Item fail below. Its message
# ("The path is not of a legal form") names a line in this script instead of
# the parameter that was wrong, which is a bad first experience for the one
# argument nobody can guess for you.
foreach ($pair in @(@('-Root', $Root), @('-ScriptDir', $ScriptDir))) {
    $name, $value = $pair
    if ([string]::IsNullOrWhiteSpace($value) -or -not [System.IO.Path]::IsPathRooted($value) -or
        $value -notmatch '^[A-Za-z]:\\') {
        throw @"
$name must be a full Windows path such as C:\aloelite\data -- got '$value'.

If you do not know where the running manager keeps its data, ask it: the
volume's fs_id names the file.

  Invoke-RestMethod http://127.0.0.1:$Port/volumes | Format-Table id, name, fs_id
  Get-ChildItem `$env:USERPROFILE\.aloelite      # the default root

A manager started without --root uses `$env:USERPROFILE\.aloelite, which is
NOT where this task will look: it runs as SYSTEM, whose profile is different.
Pass that path explicitly, or move the data somewhere neutral first.
"@
    }
}

New-Item -ItemType Directory -Force -Path $ScriptDir, $Root | Out-Null

# -- 1. the manager -------------------------------------------------------
# ONE manager, serving both surfaces from ONE root. Splitting S3 and WebDAV
# into two processes would need two --roots (the store supports a single
# writer), which means two separate volume sets -- so backups would land
# somewhere the mapped drive cannot see them. That defeats the point.
#
# Reaching S3 from another machine therefore means binding off loopback, and
# `--webdav` off loopback without TLS is refused by default. The refusal's
# reason is specific: WebDAV authenticates with HTTP Basic where the password
# is the volume PIN. An UNENCRYPTED volume issues no Basic challenge at all
# (dav.py's _session_token returns None for a plain volume), so on an
# unencrypted volume there is no PIN to leak and -Insecure costs nothing the
# transport did not already cost. On an ENCRYPTED volume it puts the PIN on
# the wire in base64 on every request -- do not do it.
$loopback = @('127.0.0.1', 'localhost', '::1')
if ($BindHost -notin $loopback -and -not $Insecure) {
    throw @"
-BindHost $BindHost is not loopback, and WebDAV off loopback without TLS is
refused. Either keep the default (S3 will only be reachable from this
machine), or pass -Insecure -- which is only appropriate when the volumes are
UNENCRYPTED (no Basic auth, so no PIN on the wire) and the interface is a
trusted/VPN one. For encrypted volumes, use --tls-cert with a certificate
your clients trust instead.
"@
}
if ($BindHost -notin $loopback) {
    Write-Warning "Serving on $BindHost without TLS: object DATA crosses the network in the clear. Use only on a trusted/VPN interface, and only with unencrypted volumes."
}

$serverPs1 = Join-Path $ScriptDir 'aloelite-server.ps1'
$env_lines = @()
if ($AccessKey) {
    $env_lines += "`$env:ALOELITE_S3 = '1'"
    $env_lines += "`$env:ALOELITE_S3_ACCESS_KEY = '$AccessKey'"
    $env_lines += "`$env:ALOELITE_S3_SECRET_FILE = '$SecretFile'"
}
$insecureFlag = if ($Insecure) { ' --insecure' } else { '' }
@"
# Generated by Install-AloeliteTasks.ps1 -- edit the generator, not this.
`$ErrorActionPreference = 'Stop'
$($env_lines -join "`n")
& '$Python' -m manager.web --webdav --host $BindHost --port $Port --root '$Root'$insecureFlag
"@ | Set-Content -Path $serverPs1 -Encoding UTF8

# -- 2. the drive map -----------------------------------------------------
# /persistent:no on purpose. A remembered mapping is restored at logon BEFORE
# this script runs, i.e. before the manager is listening, and Windows leaves it
# marked disconnected. Mapping it here, after the port answers, is the version
# that actually works.
$mapPs1 = Join-Path $ScriptDir 'aloelite-map.ps1'
@"
# Generated by Install-AloeliteTasks.ps1 -- edit the generator, not this.
`$ErrorActionPreference = 'SilentlyContinue'

# WebClient is trigger-start and may not be up yet; a mapping attempted
# without it fails with a misleading "network name cannot be found".
if ((Get-Service WebClient).Status -ne 'Running') {
    Start-Service WebClient
}

# Wait for the manager rather than racing it. 60 x 2s covers a cold boot on a
# slow disk; failing after that is better than mapping a dead drive.
`$ready = `$false
foreach (`$i in 1..60) {
    try {
        `$r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 ``
            -Uri 'http://127.0.0.1:$Port/health'
        if (`$r.StatusCode -in 200, 503) { `$ready = `$true; break }
    } catch { Start-Sleep -Seconds 2 }
}
if (-not `$ready) {
    Write-Error 'aloelite did not answer on :$Port; not mapping the drive.'
    exit 1
}

if (Test-Path '${Drive}:') { net use ${Drive}: /delete /y | Out-Null }
net use ${Drive}: \\127.0.0.1@$Port\DavWWWRoot\dav\$VolumeId /persistent:no
"@ | Set-Content -Path $mapPs1 -Encoding UTF8

# -- 3. register both -----------------------------------------------------
$psExe = (Get-Command powershell.exe).Source
$common = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File'

$serverTask = "$TaskPrefix-Manager"
$mapTask = "$TaskPrefix-MapDrive"
foreach ($t in $serverTask, $mapTask) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
    }
}

# The manager: SYSTEM, at boot, restarted if it dies. StartWhenAvailable is
# off by default for boot triggers, which is what we want -- a missed boot
# trigger should not fire hours later.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $serverTask `
    -Action (New-ScheduledTaskAction -Execute $psExe -Argument "$common `"$serverPs1`"") `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal (New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest) `
    -Settings $settings `
    -Description 'Aloelite manager (WebDAV on loopback, S3 if configured).' | Out-Null

# The drive: you, at logon, unelevated. NOT Highest -- a drive mapped by an
# elevated token is invisible to your unelevated Explorer, which is the whole
# bug this task exists to avoid.
Register-ScheduledTask -TaskName $mapTask `
    -Action (New-ScheduledTaskAction -Execute $psExe -Argument "$common `"$mapPs1`"") `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) `
    -Principal (New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited) `
    -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5)) `
    -Description "Map ${Drive}: to the aloelite WebDAV volume once the manager answers." | Out-Null

Write-Host ""
Write-Host "Registered:" -ForegroundColor Green
Write-Host "  $serverTask   (SYSTEM, at startup)  -> $serverPs1"
Write-Host "  $mapTask   (you, at logon)      -> $mapPs1"
Write-Host ""
Write-Host "Start them now without rebooting:"
Write-Host "  Start-ScheduledTask -TaskName $serverTask"
Write-Host "  Start-ScheduledTask -TaskName $mapTask"
Write-Host ""
Write-Host "Remove:"
Write-Host "  Unregister-ScheduledTask -TaskName $serverTask,$mapTask -Confirm:`$false"
if ($BindHost -notin $loopback) {
    Write-Host ""
    Write-Host "Reachable off this machine -- open the port for the private profile only:" -ForegroundColor Yellow
    Write-Host "  New-NetFirewallRule -DisplayName 'aloelite' -Direction Inbound ``"
    Write-Host "    -LocalPort $Port -Protocol TCP -Action Allow -Profile Private"
}
