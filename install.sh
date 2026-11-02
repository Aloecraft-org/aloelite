#!/bin/sh
# Aloelite's native binaries: start, install and get, in one script.
#
#   curl -fsSL https://software.aloecraft.org/releases/aloelite/latest/start.sh | sh
#   curl -fsSL https://github.com/Aloecraft-org/aloelite/releases/latest/download/start.sh | sh
#
# Every release ships this file eight times, as start.sh, start-aloelite.sh,
# install.sh, install-aloelite.sh, get.sh, get-aloelite.sh, help.sh and
# help-aloelite.sh. release.yml (`installers`) stamps two lines in each
# copy: the release that shipped it, and its mode. A script piped to `sh`
# cannot see the URL it came from, so the stamp is the only way a copy can
# know what it is. The checkout's copy is an unstamped start script.
#
# It installs the Rust CLI, `aloelite`, and on Linux the FUSE daemon,
# `aloelite-fuse`. The Python package is `pip install aloelite` instead;
# both answer to `aloelite`, and `check` says which one PATH finds.
#
# The same script for Windows is install.ps1. The port it follows is
# Aloecraft-org/diluvium-drt's install.sh.
#
# POSIX sh: no arrays, no `local`, no bashisms. It runs on busybox ash and on
# macOS's /bin/sh. curl or wget, tar, and sha256sum, shasum or openssl.
set -eu

# ---------------------------------------------------------------- surface
# Entry points. With no command, MODE decides:
#   start    install the latest stable release
#   install  install the release that shipped this copy
#   get      download and verify that release into the current directory
#   help     print the usage below
usage() {
    cat <<EOF
usage: $SELF [command] [--version VERSION] [--prefix DIR] [--libc gnu|musl]

  (none)            $(mode_says)
  install [VERSION] install VERSION, or the default above, and use it
  get [VERSION]     download and verify VERSION into the current directory
  latest            as VERSION: the newest stable release
  dev               as VERSION: the newest dev build
  --version, -r, --release [VERSION]
                    with VERSION, the release to act on; alone, print the
                    release this copy came from
  list              stable releases available
  list-all          every release, candidates and dev builds included
  check             what is installed, and which one is in use
  use VERSION       switch to an installed release
  remove [VERSION]  uninstall VERSION, or every release
  help, --help      this

  --prefix DIR      install under DIR/bin and DIR/share/aloelite
                    (default /usr/local if writable, else ~/.local)
  --libc gnu|musl   on Linux, which build to take (default: the host's;
                    on glibc, gnu first and musl if the release has no gnu)

An install puts aloelite in DIR/bin, and aloelite-fuse beside it on Linux.

Env: ALOELITE_VERSION, ALOELITE_PREFIX, ALOELITE_LIBC, ALOELITE_MIRROR
(file:// URLs work) and ALOELITE_MIRROR_ONLY=1 (never fall back to GitHub).

Piped from curl, a command goes after 'sh -s --':
  curl -fsSL https://software.aloecraft.org/releases/aloelite/latest/start.sh | sh -s -- list
EOF
}

# Stamped by release.yml. Empty means this copy came from no release.
STAMPED_VERSION=
STAMPED_MODE=start

# Configurable values.
NAME=aloelite
REPO=Aloecraft-org/aloelite
MIRROR="${ALOELITE_MIRROR:-https://software.aloecraft.org/releases/aloelite}"
MIRROR_ONLY="${ALOELITE_MIRROR_ONLY:-0}"
GITHUB="https://github.com/$REPO"
GITHUB_API="https://api.github.com/repos/$REPO"
RAW="https://raw.githubusercontent.com/$REPO"
SELFTEST=--version
# Every executable a release may carry, the CLI first. Only these are
# linked into DIR/bin; BUILDINFO.txt stays in the version directory.
BINS="aloelite aloelite-fuse"
PREFIX="${ALOELITE_PREFIX:-}"
LIBC="${ALOELITE_LIBC:-}"

# Fan-out: the command picks the action.
dispatch() {
    case "$ACTION" in
        print)    say "${STAMPED_VERSION:-none (a checkout copy, which installs latest)}" ;;
        help)     usage ;;
        install)  do_install ;;
        get)      do_get ;;
        list)     do_list stable ;;
        list-all) do_list all ;;
        check)    do_check ;;
        use)      do_use ;;
        remove)   do_remove ;;
    esac
}

# What a release carries for this host. Sets
#   PLATFORMS  the doc/ALIGNMENT.md §4 platform names to try, best first
#   TRIPLES    the same platforms as Rust triples, for releases before 0.5.0,
#              which shipped one tarball per triple
#   EXTRAS     optional components beside the CLI: aloelite_<extra>_<platform>
platform() {
    EXTRAS=""
    case "$(uname -s)" in
        Linux)  os=linux; EXTRAS=fuse ;;
        Darwin) os=darwin ;;
        MINGW*|MSYS*|CYGWIN*)
            die "on Windows, use install.ps1: irm $MIRROR/latest/start.ps1 | iex" ;;
        *) die "$(uname -s) has no prebuilt aloelite; build it from rust/ with 'cargo build --release -p aloelite-cli', or 'pip install aloelite'" ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64)  arch=x86_64; rarch=x86_64 ;;
        arm64|aarch64) arch=arm64;  rarch=aarch64 ;;
        *) die "$(uname -m) has no prebuilt aloelite; 'pip install aloelite' runs anywhere Python does" ;;
    esac
    if [ "$os" = darwin ]; then
        PLATFORMS="darwin_$arch"; TRIPLES="$rarch-apple-darwin"
        return
    fi
    if [ -z "$LIBC" ]; then
        LIBC=gnu
        for f in /lib/ld-musl-*; do [ -e "$f" ] && LIBC=musl; done
        # A glibc host runs the static musl build too; a musl host cannot
        # run the gnu one.
        [ "$LIBC" = musl ] || LIBC="gnu musl"
    fi
    PLATFORMS=""; TRIPLES=""
    for l in $LIBC; do
        case "$l" in gnu|musl) ;; *) die "--libc is gnu or musl, not $l" ;; esac
        # Releases build musl for x86_64 only.
        [ "$l:$arch" != musl:arm64 ] || continue
        PLATFORMS="$PLATFORMS linux_${arch}_$l"
        TRIPLES="$TRIPLES $rarch-unknown-linux-$l"
    done
    [ -n "$PLATFORMS" ] || die "no $LIBC build of aloelite for $arch Linux; 'pip install aloelite' runs anywhere Python does"
}

# ---------------------------------------------------------------- arguments
SELF=$(basename "$0" 2>/dev/null || echo "$NAME")
case "$SELF" in sh|bash|dash|ash|-*) SELF="$STAMPED_MODE.sh" ;; esac

say() { printf '%s\n' "$*"; }
die() { printf '%s: %s\n' "$SELF" "$*" >&2; exit 1; }

mode_says() {
    case "$STAMPED_MODE" in
        start)   echo "install the latest stable release" ;;
        install) echo "install ${STAMPED_VERSION:-latest}, the release this copy came from" ;;
        get)     echo "get ${STAMPED_VERSION:-latest}, the release this copy came from" ;;
        help)    echo "print this help" ;;
    esac
}

# A word after --version is its value unless it is an option or a command.
is_value() {
    case "$1" in
        -*|install|get|list|list-all|check|use|remove|help) return 1 ;;
    esac
    return 0
}

ACTION=""; TARGET=""; PRINT=0
while [ $# -gt 0 ]; do
    case "$1" in
        install|get|list|list-all|check|use|remove|help)
            [ -z "$ACTION" ] || die "one command at a time: $ACTION, then $1"
            ACTION="$1" ;;
        --version|-r|--release)
            if [ $# -gt 1 ] && is_value "$2"; then TARGET="$2"; shift; else PRINT=1; fi ;;
        --version=*|--release=*) TARGET="${1#*=}" ;;
        --prefix)   [ $# -gt 1 ] || die "--prefix needs a directory"; PREFIX="$2"; shift ;;
        --prefix=*) PREFIX="${1#*=}" ;;
        --libc)     [ $# -gt 1 ] || die "--libc needs gnu or musl"; LIBC="$2"; shift ;;
        --libc=*)   LIBC="${1#*=}" ;;
        -h|--help)  usage; exit 0 ;;
        -*)         die "unknown option: $1 (try --help)" ;;
        *)
            [ -z "$TARGET" ] || die "two versions given: $TARGET and $1"
            TARGET="$1" ;;
    esac
    shift
done

if [ -z "$ACTION" ]; then
    if [ "$PRINT" = 1 ] && [ -z "$TARGET" ]; then
        ACTION=print
    else
        case "$STAMPED_MODE" in get|help) ACTION="$STAMPED_MODE" ;; *) ACTION=install ;; esac
    fi
fi
if [ -z "$TARGET" ]; then
    case "$STAMPED_MODE" in
        start) TARGET="${ALOELITE_VERSION:-latest}" ;;
        *)     TARGET="${ALOELITE_VERSION:-${STAMPED_VERSION:-latest}}" ;;
    esac
    # `remove` with no version means every version, not the default one.
    [ "$ACTION" != remove ] || TARGET=""
fi
[ "$ACTION" != use ] || [ -n "$TARGET" ] || die "use needs a version (see '$SELF check')"

# ---------------------------------------------------------------- depth: fetching and verifying
have() { command -v "$1" >/dev/null 2>&1; }

if have curl; then
    fetch()    { curl -fsSL --retry 2 -o "$2" "$1" 2>/dev/null; }
    fetchout() { curl -fsSL --retry 2 "$1" 2>/dev/null; }
elif have wget; then
    fetch()    { wget -q -O "$2" "$1"; }
    fetchout() { wget -q -O - "$1"; }
else
    die "neither curl nor wget is available"
fi

# Where a release's files are: the mirror, then GitHub Releases.
bases() {
    printf '%s\n' "$MIRROR/$1"
    [ "$MIRROR_ONLY" = 1 ] || printf '%s\n' "$GITHUB/releases/download/$1"
}

sha256_of() {
    if have sha256sum; then sha256sum "$1" | cut -d' ' -f1
    elif have shasum;  then shasum -a 256 "$1" | cut -d' ' -f1
    elif have openssl; then openssl dgst -sha256 "$1" | sed 's/.*= *//'
    fi
}

json_latest() {
    sed -n 's/^[[:space:]]*"latest"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
}

# Releases as "tag<TAB>channel", newest first, from the mirror's
# releases.json ("tag", "stable") or GitHub's release list ("tag_name",
# "prerelease"). Both are pretty-printed with one key per line, each release
# names its tag first, and neither key appears anywhere but a release.
json_releases() {
    awk '
        function emit() {
            if (tag != "" && stable != "") {
                ch = (stable == "true") ? "stable" : "rc"
                if (tag ~ /-dev\./) ch = "dev"
                printf "%s\t%s\n", tag, ch
            }
            tag = ""; stable = ""
        }
        function val(s) { sub(/^[^:]*:[ \t]*/, "", s); sub(/[ \t,]*$/, "", s); gsub(/"/, "", s); return s }
        /^[ \t]*"(tag|tag_name)"[ \t]*:/ { emit(); tag = val($0) }
        /^[ \t]*"stable"[ \t]*:/         { stable = val($0) }
        /^[ \t]*"prerelease"[ \t]*:/     { stable = (val($0) == "true") ? "false" : "true" }
        END { emit() }
    '
}

# Every release, dev builds included: the mirror's index lists them, and
# GitHub's release list is the fallback.
releases() {
    rows=$(fetchout "$MIRROR/releases.json" | json_releases || true)
    if [ -z "$rows" ] && [ "$MIRROR_ONLY" != 1 ]; then
        rows=$(fetchout "$GITHUB_API/releases?per_page=100" | json_releases || true)
    fi
    [ -n "$rows" ] || die "cannot list releases: the mirror and GitHub both failed"
    printf '%s\n' "$rows"
}

# Every source that can name the newest stable release: the mirror, then
# what GitHub Releases can actually serve, and the changelog on main last,
# since it can call a release out before its assets are published.
resolve_latest() {
    tag=$(fetchout "$MIRROR/releases.json" | json_latest || true)
    if [ -z "$tag" ] && [ "$MIRROR_ONLY" != 1 ]; then
        if have curl; then
            # GitHub answers /releases/latest with a redirect to the tag.
            tag=$(curl -fsSI "$GITHUB/releases/latest" 2>/dev/null \
                | sed -n 's|^[Ll]ocation:.*/releases/tag/\([^[:space:]]*\).*|\1|p' | head -1 || true)
        fi
        [ -n "$tag" ] || tag=$(fetchout "$RAW/main/changelog.json" | json_latest || true)
    fi
    [ -n "$tag" ] || die "cannot tell which release is latest: the mirror and GitHub both failed"
    printf '%s\n' "$tag"
}

resolve() {
    case "$1" in
        latest) resolve_latest ;;
        dev)
            tag=$(releases | awk -F'\t' '$2 == "dev" { print $1; exit }')
            [ -n "$tag" ] || die "no dev build is published (see '$SELF list-all')"
            printf '%s\n' "$tag" ;;
        v*) printf '%s\n' "$1" ;;
        *)  printf 'v%s\n' "$1" ;;
    esac
}

# Download the release's files for this platform into $1, verified, under
# their installed names. Sets SOURCE to the base URL that served them.
# Bare binaries first (0.5.0 on); failing those, the tarball a release
# before 0.5.0 shipped per Rust triple.
download() {
    dir="$1"
    platform
    SOURCE=""; got=""
    for base in $(bases "$TAG"); do
        for p in $PLATFORMS; do
            if fetch "$base/${NAME}_$p" "$dir/$NAME"; then
                SOURCE="$base"; got="$p"; break 2
            fi
        done
    done
    if [ -n "$SOURCE" ]; then
        sums "$dir"
        verify "$dir/$NAME" "${NAME}_$got"
        for e in $EXTRAS; do
            if fetch "$SOURCE/${NAME}_${e}_$got" "$dir/$NAME-$e"; then
                verify "$dir/$NAME-$e" "${NAME}_${e}_$got"
            else
                say "note: $TAG has no ${NAME}_${e}_$got"
            fi
        done
        if fetch "$SOURCE/BUILDINFO.txt" "$dir/BUILDINFO.txt"; then
            verify "$dir/BUILDINFO.txt" BUILDINFO.txt
        fi
    else
        download_tarball "$dir"
    fi
    rm -f "$dir/.sums"
    for b in $BINS; do [ ! -f "$dir/$b" ] || chmod 755 "$dir/$b"; done
}

# depth: releases before 0.5.0 (doc/RELEASING.md, "the old scheme")
download_tarball() {
    dir="$1"; ver="${TAG#v}"
    for base in $(bases "$TAG"); do
        for t in $TRIPLES; do
            if fetch "$base/$NAME-$ver-$t.tar.gz" "$dir/.tgz"; then
                SOURCE="$base"; got="$NAME-$ver-$t.tar.gz"; break 2
            fi
        done
    done
    if [ -z "$SOURCE" ]; then
        want=""; for p in $PLATFORMS; do want="$want ${NAME}_$p"; done
        die "no${want} for $TAG in $(bases "$TAG" | tr '\n' ' ')(see '$SELF list-all')"
    fi
    sums "$dir"
    verify "$dir/.tgz" "$got"
    mkdir "$dir/.x"
    tar xzf "$dir/.tgz" -C "$dir/.x"
    for b in $BINS; do
        f=$(find "$dir/.x" -type f -name "$b" | head -1)
        [ -z "$f" ] || mv "$f" "$dir/$b"
    done
    rm -rf "$dir/.x" "$dir/.tgz"
    [ -f "$dir/$NAME" ] || die "$got from $SOURCE holds no $NAME"
}

# The checksum manifest beside SOURCE, as $1/.sums. Before 0.5.0 it had no
# extension.
sums() {
    fetch "$SOURCE/SHA256SUMS.txt" "$1/.sums" || fetch "$SOURCE/SHA256SUMS" "$1/.sums" || : > "$1/.sums"
}

# A missing sums file, or an asset it does not list, warns rather than
# refuses: that is a release older than its manifest, and refusing would
# strand whoever pinned it. A mismatch always refuses.
verify() {
    want=$(grep " \*\{0,1\}$2\$" "$(dirname "$1")/.sums" | cut -d' ' -f1 || true)
    if [ -z "$want" ]; then
        say "warning: $SOURCE has no checksum for $2; not verified" >&2
        return 0
    fi
    have_sum=$(sha256_of "$1")
    [ -n "$have_sum" ] || { say "warning: no sha256 tool; $2 not verified" >&2; return 0; }
    [ "$want" = "$have_sum" ] || die "checksum mismatch for $2 from $SOURCE
  expected $want
  got      $have_sum"
}

# ---------------------------------------------------------------- depth: the install layout
# DIR/share/aloelite/versions/<tag>/ holds each release; DIR/bin/aloelite
# (and aloelite-fuse) is a link to the one in use. A plain file there is an
# install from somewhere else, and `use` replaces it.
default_prefix() {
    if [ -n "$PREFIX" ]; then return; fi
    if [ -w /usr/local/bin ] || [ "$(id -u)" = 0 ]; then PREFIX=/usr/local; else PREFIX="$HOME/.local"; fi
}

# For use, remove and check: the prefixes that already hold installs.
installed_prefixes() {
    if [ -n "$PREFIX" ]; then printf '%s\n' "$PREFIX"; return; fi
    for p in /usr/local "$HOME/.local"; do
        [ -d "$p/share/$NAME/versions" ] && printf '%s\n' "$p"
    done
    return 0
}

active_in() {
    link=$(readlink "$1/bin/$NAME" 2>/dev/null || true)
    case "$link" in
        */share/$NAME/versions/*) printf '%s\n' "$link" | sed "s|.*/share/$NAME/versions/\([^/]*\)/.*|\1|" ;;
    esac
}

link_version() {
    mkdir -p "$PREFIX/bin"
    for b in $BINS; do
        if [ ! -f "$PREFIX/share/$NAME/versions/$TAG/$b" ]; then
            # A link into another release for a binary this one lacks would
            # leave half of the previous release in use.
            case "$(readlink "$PREFIX/bin/$b" 2>/dev/null || true)" in
                */share/$NAME/versions/*) rm -f "$PREFIX/bin/$b" ;;
            esac
            continue
        fi
        if [ -f "$PREFIX/bin/$b" ] && [ ! -L "$PREFIX/bin/$b" ]; then
            say "replacing $PREFIX/bin/$b, which this script did not install"
        fi
        # A link made beside the target and renamed over it: replacing a
        # running binary in place fails with ETXTBSY, and rename does not.
        ln -sf "../share/$NAME/versions/$TAG/$b" "$PREFIX/bin/.$b.new"
        mv -f "$PREFIX/bin/.$b.new" "$PREFIX/bin/$b"
    done
}

# ---------------------------------------------------------------- depth: the commands
do_install() {
    TAG=$(resolve "$TARGET")
    default_prefix
    vdir="$PREFIX/share/$NAME/versions/$TAG"
    mkdir -p "$PREFIX/share/$NAME/versions" 2>/dev/null && [ -w "$PREFIX/share/$NAME/versions" ] \
        || die "$PREFIX is not writable; pass --prefix \$HOME/.local, or run as root"
    tmp=$(mktemp -d "$PREFIX/share/$NAME/versions/.$TAG.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT INT TERM
    # mktemp -d makes the folder 0700, and it becomes the version folder the
    # bin link points into: under a root install no other user could run it.
    chmod 755 "$tmp"
    download "$tmp"
    "$tmp/$NAME" $SELFTEST >/dev/null 2>&1 || die "the downloaded $NAME does not run here ($SOURCE)"
    rm -rf "$vdir"
    mv "$tmp" "$vdir"
    trap - EXIT INT TERM
    link_version
    say "installed $("$PREFIX/bin/$NAME" $SELFTEST | head -1) to $PREFIX/bin/$NAME"
    [ ! -f "$vdir/$NAME-fuse" ] || say "  and $NAME-fuse beside it"
    say "  source: $SOURCE"
    [ ! -f "$vdir/BUILDINFO.txt" ] || sed 's/^/  /' "$vdir/BUILDINFO.txt"
    case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) say "note: $PREFIX/bin is not on your PATH" ;; esac
}

do_get() {
    TAG=$(resolve "$TARGET")
    tmp=$(mktemp -d "./.$NAME-get.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT INT TERM
    download "$tmp"
    for f in "$tmp"/*; do
        mv -f "$f" "./$(basename "$f")"
        say "got ./$(basename "$f") ($TAG, from $SOURCE)"
    done
}

do_list() {
    rows=$(releases)
    if [ "$1" = stable ]; then
        printf '%s\n' "$rows" | awk -F'\t' '$2 == "stable" { print $1 }'
    else
        printf '%s\n' "$rows" | awk -F'\t' '{ printf "%-24s %s\n", $1, $2 }'
    fi
}

do_check() {
    found=0
    for p in $(installed_prefixes); do
        active=$(active_in "$p")
        for v in "$p/share/$NAME/versions"/*; do
            [ -d "$v" ] || continue
            found=1
            t=$(basename "$v")
            if [ "$t" = "$active" ]; then say "$t  $p  (in use)"; else say "$t  $p"; fi
        done
    done
    [ "$found" = 1 ] || say "no release installed by this script"
    on_path=$(command -v "$NAME" 2>/dev/null || true)
    if [ -n "$on_path" ]; then
        say "on PATH: $on_path ($("$on_path" $SELFTEST 2>/dev/null | head -1 || echo "does not run"))"
        case "$(readlink "$on_path" 2>/dev/null || true)" in
            */share/$NAME/versions/*) ;;
            *) say "  not managed by this script (pip's aloelite is the Python one)" ;;
        esac
    fi
}

do_use() {
    TAG=$(resolve "$TARGET")
    for p in $(installed_prefixes); do
        if [ -d "$p/share/$NAME/versions/$TAG" ]; then
            PREFIX="$p"; link_version
            say "using $TAG: $PREFIX/bin/$NAME"
            return
        fi
    done
    die "$TAG is not installed (see '$SELF check'; '$SELF install $TAG' installs it)"
}

# The links in $1/bin into release $2, or into any release when $2 is empty.
unlink_from() {
    for b in $BINS; do
        link=$(readlink "$1/bin/$b" 2>/dev/null || true)
        case "$link" in */share/$NAME/versions/*) ;; *) continue ;; esac
        if [ -z "$2" ]; then rm -f "$1/bin/$b"; continue; fi
        case "$link" in */share/$NAME/versions/"$2"/*) rm -f "$1/bin/$b" ;; esac
    done
}

do_remove() {
    removed=0
    for p in $(installed_prefixes); do
        active=$(active_in "$p")
        if [ -n "$TARGET" ]; then
            TAG=$(resolve "$TARGET")
            [ -d "$p/share/$NAME/versions/$TAG" ] || continue
            rm -rf "$p/share/$NAME/versions/$TAG"
            say "removed $TAG from $p"
            removed=1
            if [ "$TAG" = "$active" ]; then
                unlink_from "$p" "$TAG"
                say "$TAG was in use; '$SELF use VERSION' picks another"
            fi
        else
            unlink_from "$p" ""
            rm -rf "$p/share/$NAME"
            say "removed every $NAME release from $p"
            removed=1
        fi
    done
    [ "$removed" = 1 ] || die "${TARGET:-no release} is not installed (see '$SELF check')"
}

dispatch
