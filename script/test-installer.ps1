# Tests for install.ps1, offline, on Windows: a fake mirror under a
# temporary directory, reached through ALOELITE_MIRROR as a file:// URL,
# with ALOELITE_MIRROR_ONLY=1 so nothing falls back to GitHub. The binaries
# are one tiny console program, compiled here by Windows PowerShell's
# Add-Type, that answers any arguments with exit 0; each copy has its tag
# appended so every file has its own bytes.
#
#   pwsh -File script/test-installer.ps1                     under PowerShell 7
#   pwsh -File script/test-installer.ps1 -Shell powershell   under 5.1
#
# CI runs both (.github/workflows/main.yml, installer-windows).
param([string]$Shell = 'pwsh')
$ErrorActionPreference = 'Stop'

$Script = Join-Path (Split-Path -Parent $PSScriptRoot) 'install.ps1'
$Name = 'aloelite'
$Asset = 'aloelite_windows_x86_64.exe'
$Triple = 'x86_64-pc-windows-msvc'

# ---------------------------------------------------------------- depth: the fixture
$T = Join-Path ([IO.Path]::GetTempPath()) ("installer-test-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$M = Join-Path $T 'mirror'
New-Item -ItemType Directory -Force -Path $M | Out-Null

# PowerShell 7's Add-Type cannot write an executable; 5.1's can.
$Fake = Join-Path $T 'fake.exe'
$compile = "Add-Type -OutputType ConsoleApplication -OutputAssembly '$Fake' -TypeDefinition 'class P { static int Main(string[] a) { System.Console.WriteLine(""fake""); return 0; } }'"
& powershell -NoProfile -EncodedCommand ([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($compile)))
if (-not (Test-Path $Fake)) { throw 'could not compile the fake binary' }

function WriteSums([string]$d, [string]$file) {
    $sums = @(Get-ChildItem -LiteralPath $d -File | ForEach-Object { "$((Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLowerInvariant())  $($_.Name)" })
    [IO.File]::WriteAllLines((Join-Path $d $file), $sums)
}
# The 0.5.0-on layout: a bare binary and BUILDINFO.txt.
function Release([string]$tag) {
    $d = Join-Path $M $tag
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    $f = Join-Path $d $Asset
    Copy-Item $Fake $f
    [IO.File]::AppendAllText($f, $tag)
    [IO.File]::WriteAllText((Join-Path $d 'BUILDINFO.txt'), "tag: $tag`n")
    WriteSums $d 'SHA256SUMS.txt'
}
# The layout before 0.5.0: one zip per triple, SHA256SUMS with no extension.
function OldRelease([string]$tag) {
    $v = $tag.TrimStart('v')
    $d = Join-Path $M $tag
    $stage = Join-Path $T "build\$Name-$v-$Triple"
    New-Item -ItemType Directory -Force -Path $d, $stage | Out-Null
    Copy-Item $Fake (Join-Path $stage "$Name.exe")
    [IO.File]::AppendAllText((Join-Path $stage "$Name.exe"), $tag)
    Compress-Archive -Path $stage -DestinationPath (Join-Path $d "$Name-$v-$Triple.zip")
    WriteSums $d 'SHA256SUMS'
}
Release 'v1.0.0'; Release 'v1.1.0'; Release 'v1.2.0-dev.1'; Release 'v1.0.1'; OldRelease 'v0.4.0'
# v1.0.1's sums name other bytes: the install must refuse it.
[IO.File]::AppendAllText((Join-Path $M "v1.0.1\$Asset"), 'tampered')

@'
{
  "latest": "v1.1.0",
  "releases": [
    { "tag": "v1.2.0-dev.1", "stable": false },
    { "tag": "v1.1.0", "stable": true },
    { "tag": "v1.1.0rc1", "stable": false },
    { "tag": "v1.0.0", "stable": true },
    { "tag": "v0.4.0", "stable": true }
  ]
}
'@ | Set-Content -Encoding ASCII (Join-Path $M 'releases.json')

$env:ALOELITE_MIRROR = ([Uri]$M).AbsoluteUri
$env:ALOELITE_MIRROR_ONLY = '1'
Remove-Item Env:ALOELITE_VERSION, Env:ALOELITE_PREFIX -ErrorAction SilentlyContinue
$P = Join-Path $T 'prefix'

function Stamp([string]$mode, [string]$tag) {
    $out = Join-Path $T "$mode-$tag.ps1"
    (Get-Content -Raw $Script) -replace "(?m)^\`$StampedVersion = .*$", "`$StampedVersion = '$tag'" `
                               -replace "(?m)^\`$StampedMode = .*$", "`$StampedMode = '$mode'" |
        Set-Content -Encoding UTF8 $out
    $out
}

# Runs a copy in its own process, as `irm | iex` would on a user's machine.
function Run([string]$file) {
    $out = & $Shell -NoProfile -ExecutionPolicy Bypass -File $file @args 2>&1 | ForEach-Object { "$_" }
    $script:Code = $LASTEXITCODE
    ($out -join "`n").Trim()
}

# ---------------------------------------------------------------- depth: the cases
$fails = 0
function Ok([string]$n) { Write-Host "ok   $n" }
function Bad([string]$n, [string]$why) { Write-Host "FAIL $n"; if ($why) { Write-Host "  $why" }; $script:fails++ }
function Same([string]$n, $got, $want) { if ("$got" -eq "$want") { Ok $n } else { Bad $n "want: $want`n  got:  $got" } }
function Has([string]$n, $got, $want) { if ("$got".Contains($want)) { Ok $n } else { Bad $n "want it to contain: $want`n  got: $got" } }
# Runs a step that must succeed, and shows its output when it does not.
function Step([string]$file) {
    $out = Run $file @args
    if ($Code -ne 0) { Bad "step: $(Split-Path -Leaf $file) $($args -join ' ')" $out }
}
function FailsWith([string]$n, $got, $want) { if ($Code -ne 0) { Has $n $got $want } else { Bad "$n (it succeeded)" } }
function InUse { (Get-Content -Raw (Join-Path $P "share\$Name\active")).Trim() }
function BinIs([string]$tag) {
    (Get-FileHash (Join-Path $P "bin\$Name.exe")).Hash -eq (Get-FileHash (Join-Path $P "share\$Name\versions\$tag\$Name.exe")).Hash
}

try {
    Same 'checkout copy --version' (Run $Script --version) 'none (a checkout copy, which installs latest)'
    Same 'stamped --version' (Run (Stamp 'install' 'v1.0.0') --version) 'v1.0.0'
    Same 'stamped -r' (Run (Stamp 'get' 'v1.0.0') -r) 'v1.0.0'
    Has  'help mode prints usage' (Run (Stamp 'help' 'v1.0.0')) 'list-all'
    Has  '--help in any mode' (Run $Script --help) 'remove [VERSION]'
    $sb = [scriptblock]::Create((Get-Content -Raw $Script))
    Has  'piped, through a script block' ((& $sb --help) -join "`n") 'usage:'
    FailsWith 'unknown option refuses' (Run $Script --bogus) 'unknown option'

    Same 'list: stable only, newest first' ((Run $Script list) -replace "`r?`n", ' ') 'v1.1.0 v1.0.0 v0.4.0'
    Has  'list-all: dev builds too' (Run $Script list-all) 'v1.2.0-dev.1'
    Has  'list-all: candidates too' (Run $Script list-all) 'v1.1.0rc1                rc'

    Step $Script --prefix $P
    Same 'start installs latest' (InUse) 'v1.1.0'
    if (BinIs 'v1.1.0') { Ok 'bin holds the release in use' } else { Bad 'bin holds the release in use' }
    if (Test-Path (Join-Path $P "share\$Name\versions\v1.1.0\BUILDINFO.txt")) { Ok 'BUILDINFO.txt is kept with the release' } else { Bad 'BUILDINFO.txt is kept with the release' }
    if (-not (Test-Path (Join-Path $P 'bin\BUILDINFO.txt'))) { Ok 'and not copied into bin' } else { Bad 'and not copied into bin' }

    Step (Stamp 'install' 'v1.0.0') --prefix $P
    Same 'install.ps1 installs its own release' (InUse) 'v1.0.0'

    Step (Stamp 'get' 'v1.0.0') install latest --prefix $P
    Same 'get.ps1 install latest behaves as install' (InUse) 'v1.1.0'

    Step $Script --prefix $P -r 1.2.0-dev.1
    Same '-r without the v' (InUse) 'v1.2.0-dev.1'
    Step $Script --prefix $P use 1.0.0
    Step $Script --prefix $P install dev
    Same "dev resolves through the mirror's index" (InUse) 'v1.2.0-dev.1'

    Step $Script --prefix $P install 0.4.0
    Same 'a release before 0.5.0 installs from its zip' (InUse) 'v0.4.0'
    if (BinIs 'v0.4.0') { Ok 'and bin holds it' } else { Bad 'and bin holds it' }

    $check = Run $Script --prefix $P check
    Has 'check lists every release' $check 'v1.0.0'
    Has 'check marks the one in use' $check "v0.4.0  $P  (in use)"

    Step $Script --prefix $P use v1.0.0
    if (BinIs 'v1.0.0') { Ok 'use switches' } else { Bad 'use switches' }
    FailsWith 'use refuses what is not installed' (Run $Script --prefix $P use v9.9.9) 'not installed'
    FailsWith 'a release that does not exist refuses' (Run $Script --prefix $P -r v9.9.9) "no $Asset"

    FailsWith 'a checksum mismatch refuses' (Run $Script --prefix $P -r v1.0.1) 'checksum mismatch'
    Same 'a refused install changes nothing' (InUse) 'v1.0.0'

    Step $Script --prefix $P remove v1.0.0
    if (-not (Test-Path (Join-Path $P "bin\$Name.exe"))) { Ok 'removing the one in use removes its binary' } else { Bad 'removing the one in use removes its binary' }
    Has 'the others stay' (Run $Script --prefix $P check) 'v1.1.0'

    Step $Script --prefix $P remove
    if (-not (Test-Path (Join-Path $P "share\$Name"))) { Ok 'remove with no version removes all' } else { Bad 'remove with no version removes all' }
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $user -or ($user -split ';') -notcontains (Join-Path $P 'bin')) { Ok 'and takes bin off the PATH' } else { Bad 'and takes bin off the PATH' }

    $here = Join-Path $T 'here'
    New-Item -ItemType Directory -Path $here | Out-Null
    Push-Location $here
    try { Step (Stamp 'get' 'v1.0.0') } finally { Pop-Location }
    if ((Get-FileHash (Join-Path $here "$Name.exe")).Hash -eq (Get-FileHash (Join-Path $M "v1.0.0\$Asset")).Hash) { Ok 'get.ps1 saves its release here' } else { Bad 'get.ps1 saves its release here' }
} finally {
    Remove-Item -Recurse -Force $T -ErrorAction SilentlyContinue
}

if ($fails -eq 0) { Write-Host "PASS ($Shell)" } else { Write-Host "$fails failed ($Shell)"; exit 1 }
