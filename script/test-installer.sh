#!/bin/sh
# Tests for install.sh, offline: a fake mirror under a temporary directory,
# reached through ALOELITE_MIRROR as a file:// URL, with
# ALOELITE_MIRROR_ONLY=1 so nothing falls back to GitHub. The binaries are
# shell scripts that print a version, so this runs on Linux and macOS with
# no network.
#
#   script/test-installer.sh            the checkout's install.sh, under sh
#   SH=dash script/test-installer.sh    under another shell
#
# CI runs it under dash and bash (.github/workflows/main.yml, installer).
set -eu
cd "$(dirname "$0")/.."

SH="${SH:-sh}"
SCRIPT="$PWD/install.sh"
NAME=aloelite
PLATFORMS="linux_x86_64_gnu linux_x86_64_musl linux_arm64_gnu darwin_x86_64 darwin_arm64"
TRIPLES="x86_64-unknown-linux-gnu x86_64-unknown-linux-musl aarch64-unknown-linux-gnu x86_64-apple-darwin aarch64-apple-darwin"
LINUX=0; [ "$(uname -s)" != Linux ] || LINUX=1

# ---------------------------------------------------------------- depth: the fixture
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
M="$T/mirror"
mkdir -p "$M"

fake() {  # $1 = file, $2 = program name, $3 = version
    printf '#!/bin/sh\necho "%s %s"\n' "$2" "$3" > "$1"
    chmod +x "$1"
}
sums() {  # $1 = directory, $2 = manifest name
    (cd "$1" && { sha256sum -- * > "$2" 2>/dev/null || shasum -a 256 -- * > "$2"; })
}
release() {  # $1 = tag, rest = platforms; the 0.5.0-on layout
    tag="$1"; shift
    mkdir -p "$M/$tag"
    for p in "$@"; do
        fake "$M/$tag/${NAME}_$p" "$NAME" "${tag#v}"
        case "$p" in linux_*) fake "$M/$tag/${NAME}_fuse_$p" "$NAME-fuse" "${tag#v}" ;; esac
    done
    printf 'tag: %s\nversion: %s\n' "$tag" "${tag#v}" > "$M/$tag/BUILDINFO.txt"
    sums "$M/$tag" SHA256SUMS.txt
}
old_release() {  # $1 = tag; the layout before 0.5.0, one tarball per triple
    tag="$1"; v="${tag#v}"
    mkdir -p "$M/$tag" "$T/build"
    for t in $TRIPLES; do
        d="$T/build/$NAME-$v-$t"; mkdir -p "$d"
        fake "$d/$NAME" "$NAME" "$v"
        case "$t" in *linux*) fake "$d/$NAME-fuse" "$NAME-fuse" "$v" ;; esac
        tar czf "$M/$tag/$NAME-$v-$t.tar.gz" -C "$T/build" "$NAME-$v-$t"
    done
    sums "$M/$tag" SHA256SUMS
}
release v1.0.0 linux_x86_64_musl darwin_x86_64 darwin_arm64
release v1.1.0 $PLATFORMS
release v1.2.0-dev.1 $PLATFORMS
release v1.0.1 $PLATFORMS
old_release v0.4.0
# v1.0.1's sums name other bytes: the install must refuse it.
for p in $PLATFORMS; do echo "tampered" >> "$M/v1.0.1/${NAME}_$p"; done

cat > "$M/releases.json" <<'EOF'
{
  "repo": "Aloecraft-org/aloelite",
  "latest": "v1.1.0",
  "latest_prerelease": "v1.2.0-dev.1",
  "releases": [
    {
      "tag": "v1.2.0-dev.1",
      "assets": [
        {
          "name": "SHA256SUMS.txt"
        }
      ],
      "stable": false
    },
    {
      "tag": "v1.1.0",
      "stable": true
    },
    {
      "tag": "v1.1.0rc1",
      "stable": false
    },
    {
      "tag": "v1.0.0",
      "stable": true
    },
    {
      "tag": "v0.4.0",
      "stable": true
    }
  ]
}
EOF

export ALOELITE_MIRROR="file://$M" ALOELITE_MIRROR_ONLY=1
unset ALOELITE_VERSION ALOELITE_PREFIX ALOELITE_LIBC 2>/dev/null || true
P="$T/prefix"

stamp() {  # $1 = mode, $2 = tag; prints the path of a stamped copy
    out="$T/$1-$2.sh"
    sed -e "s|^STAMPED_VERSION=.*|STAMPED_VERSION=$2|" -e "s|^STAMPED_MODE=.*|STAMPED_MODE=$1|" "$SCRIPT" > "$out"
    printf '%s\n' "$out"
}

# ---------------------------------------------------------------- depth: the cases
fails=0
ok()   { printf 'ok   %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
same() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1"; printf '  want: %s\n  got:  %s\n' "$3" "$2"; fi; }
has()  { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1"; printf '  want it to contain: %s\n  got: %s\n' "$3" "$2" ;; esac; }
fails_with() {  # $1 = name, $2 = expected text, rest = command
    n="$1"; want="$2"; shift 2
    if out=$("$@" 2>&1); then bad "$n (it succeeded)"; else has "$n" "$out" "$want"; fi
}
in_use() { readlink "$P/bin/$NAME" | sed "s|.*/versions/\([^/]*\)/.*|\1|"; }
linux_only() { if [ "$LINUX" = 1 ]; then "$@"; else ok "$2 (skipped: not Linux)"; fi; }

same "checkout copy --version" "$($SH "$SCRIPT" --version)" "none (a checkout copy, which installs latest)"
same "stamped --version" "$($SH "$(stamp install v1.0.0)" --version)" "v1.0.0"
same "stamped -r" "$($SH "$(stamp get v1.0.0)" -r)" "v1.0.0"
has  "help mode prints usage" "$($SH "$(stamp help v1.0.0)")" "list-all"
has  "--help in any mode" "$($SH "$SCRIPT" --help)" "remove [VERSION]"
has  "piped, through sh -s --" "$($SH -s -- --help < "$SCRIPT")" "usage:"
fails_with "unknown option refuses" "unknown option" $SH "$SCRIPT" --bogus
fails_with "an unknown libc refuses" "gnu or musl" $SH "$SCRIPT" --prefix "$P" --libc uclibc

same "list: stable only, newest first" "$($SH "$SCRIPT" list | tr '\n' ' ')" "v1.1.0 v1.0.0 v0.4.0 "
has  "list-all: dev builds too" "$($SH "$SCRIPT" list-all)" "v1.2.0-dev.1"
has  "list-all: candidates too" "$($SH "$SCRIPT" list-all)" "v1.1.0rc1                rc"

$SH "$SCRIPT" --prefix "$P" >/dev/null
same "start installs latest" "$(in_use)" "v1.1.0"
same "the link runs" "$("$P/bin/$NAME" --version)" "$NAME 1.1.0"
same "the version directory is readable by everyone" "$(ls -ld "$P/share/$NAME/versions/v1.1.0" | cut -c1-10)" "drwxr-xr-x"
same "BUILDINFO.txt is kept with the release" "$(head -1 "$P/share/$NAME/versions/v1.1.0/BUILDINFO.txt")" "tag: v1.1.0"
[ ! -e "$P/bin/BUILDINFO.txt" ] && ok "and not linked into bin" || bad "and not linked into bin"
linux_only same "the FUSE daemon beside it" "$("$P/bin/$NAME-fuse" --version 2>&1)" "$NAME-fuse 1.1.0"

# v1.0.0 has no gnu Linux build, so on Linux this also takes the musl one.
$SH "$(stamp install v1.0.0)" --prefix "$P" >/dev/null
same "install.sh installs its own release" "$(in_use)" "v1.0.0"

$SH "$(stamp get v1.0.0)" install latest --prefix "$P" >/dev/null
same "get.sh install latest behaves as install" "$(in_use)" "v1.1.0"

$SH "$SCRIPT" --prefix "$P" -r 1.2.0-dev.1 >/dev/null
same "-r without the v" "$(in_use)" "v1.2.0-dev.1"
$SH "$SCRIPT" --prefix "$P" use 1.0.0 >/dev/null
$SH "$SCRIPT" --prefix "$P" install dev >/dev/null
same "dev resolves through the mirror's index" "$(in_use)" "v1.2.0-dev.1"

$SH "$SCRIPT" --prefix "$P" install 0.4.0 >/dev/null
same "a release before 0.5.0 installs from its tarball" "$("$P/bin/$NAME" --version)" "$NAME 0.4.0"
linux_only same "with its FUSE daemon" "$("$P/bin/$NAME-fuse" --version 2>&1)" "$NAME-fuse 0.4.0"

check=$($SH "$SCRIPT" --prefix "$P" check)
has "check lists every release" "$check" "v1.0.0"
has "check marks the one in use" "$check" "v0.4.0  $P  (in use)"

$SH "$SCRIPT" --prefix "$P" use v1.0.0 >/dev/null
same "use switches" "$("$P/bin/$NAME" --version)" "$NAME 1.0.0"
fails_with "use refuses what is not installed" "not installed" $SH "$SCRIPT" --prefix "$P" use v9.9.9
fails_with "a release that does not exist refuses" "no aloelite_" $SH "$SCRIPT" --prefix "$P" -r v9.9.9

fails_with "a checksum mismatch refuses" "checksum mismatch" $SH "$SCRIPT" --prefix "$P" -r v1.0.1
same "a refused install changes nothing" "$(in_use)" "v1.0.0"

$SH "$SCRIPT" --prefix "$P" remove v1.0.0 >/dev/null
[ ! -e "$P/bin/$NAME" ] && ok "removing the one in use unlinks it" || bad "removing the one in use unlinks it"
has "the others stay" "$($SH "$SCRIPT" --prefix "$P" check)" "v1.1.0"

$SH "$SCRIPT" --prefix "$P" use v1.1.0 >/dev/null
$SH "$SCRIPT" --prefix "$P" remove >/dev/null
[ ! -d "$P/share/$NAME" ] && ok "remove with no version removes all" || bad "remove with no version removes all"
left=""; for b in $NAME $NAME-fuse; do [ ! -e "$P/bin/$b" ] && [ ! -L "$P/bin/$b" ] || left="$left $b"; done
same "and every link" "$left" ""

mkdir "$T/here"
(cd "$T/here" && $SH "$(stamp get v1.1.0)" >/dev/null)
same "get.sh saves its release here" "$("$T/here/$NAME" --version)" "$NAME 1.1.0"

if [ "$fails" -eq 0 ]; then echo "PASS ($SH)"; else echo "$fails failed ($SH)"; exit 1; fi
