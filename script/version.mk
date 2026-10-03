# version.mk -- one version, derived into every spelling that needs one.
#
# GENERATED. This file is placed by `technoproj sync` and verified by
# `technoproj check`. Do not edit it in a consuming repository -- change it
# in Aloecraft-org/technoproj and re-sync, or the next check will fail.
#
# Shared across Aloecraft repositories. `.technoproj` holds the only numbers
# a human edits; everything below is derived from them, so no two files can
# drift. Include it after setting __TECHNO_PROJECT_FILE:
#
#   ROOT_DIR:=$(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
#   __TECHNO_PROJECT_FILE:=${ROOT_DIR}/.technoproj
#   -include ${ROOT_DIR}/script/version.mk
#
# TECHNO_VERSION is {major, minor, patch, pre}, where `pre` is null for a
# release or {"kind": "dev|alpha|beta|rc", "n": <int>} for a prerelease.
# This replaces the old `build` field, which meant a PEP 440 suffix in
# aloelite, an off switch in xtrshow and a build counter in diluvium.

__JQ:=jq -r
__VER_MAJ:=$(shell ${__JQ} '.TECHNO_VERSION.major' ${__TECHNO_PROJECT_FILE})
__VER_MIN:=$(shell ${__JQ} '.TECHNO_VERSION.minor' ${__TECHNO_PROJECT_FILE})
__VER_PAT:=$(shell ${__JQ} '.TECHNO_VERSION.patch' ${__TECHNO_PROJECT_FILE})
__PRE_KIND:=$(shell ${__JQ} '.TECHNO_VERSION.pre.kind // ""' ${__TECHNO_PROJECT_FILE})
__PRE_N:=$(shell ${__JQ} '.TECHNO_VERSION.pre.n // ""' ${__TECHNO_PROJECT_FILE})

# The release coordinate, always X.Y.Z.
__VERSION:=${__VER_MAJ}.${__VER_MIN}.${__VER_PAT}

# The canonical git tag. The dot before the number is load-bearing: SemVer
# compares dot-separated identifiers, so `rc.10` sorts after `rc.2` while a
# bare `rc10` sorts before `rc2`.
__PRE_SUFFIX:=$(if ${__PRE_KIND},-${__PRE_KIND}.${__PRE_N},)
__TAG:=v${__VERSION}${__PRE_SUFFIX}

# PEP 440, for pyproject.toml and PyPI. dev/a/b/rc are the four markers, and
# .devN is the one that sorts below every other prerelease.
__PEP_SUFFIX:=$(strip \
  $(if $(filter dev,${__PRE_KIND}),.dev${__PRE_N}, \
  $(if $(filter alpha,${__PRE_KIND}),a${__PRE_N}, \
  $(if $(filter beta,${__PRE_KIND}),b${__PRE_N}, \
  $(if $(filter rc,${__PRE_KIND}),rc${__PRE_N},)))))
__PEP440:=${__VERSION}${__PEP_SUFFIX}

# SemVer, for Cargo. Identical to the tag without its leading v.
__SEMVER:=${__VERSION}${__PRE_SUFFIX}

__VERSION_FULL:=${__VERSION}$(if ${__PRE_KIND}, ${__PRE_KIND} ${__PRE_N},)

# The next free dev number, read from the tags that exist rather than from a
# counter in the tree. Nothing is written back, so a nightly needs no commit
# and two branches cannot collide on the same number.
__NEXT_DEV=$(shell git tag --list 'v*-dev.*' \
    | sed -n 's/.*-dev\.\([0-9][0-9]*\)$$/\1/p' \
    | sort -n | tail -1 | awk '{print $$1+1}' | grep . || echo 1)

.PHONY: version dev-tag inc_maj inc_min inc_pat set_pre clear_pre

version:
	@echo "version: ${__VERSION_FULL}"
	@echo "tag:     ${__TAG}"
	@echo "pep440:  ${__PEP440}"
	@echo "semver:  ${__SEMVER}"

# The tag a dev build would take right now. `make dev-tag` prints it;
# CI reads it to allocate one without touching .technoproj.
dev-tag:
	@echo "v${__VERSION}-dev.${__NEXT_DEV}"

inc_maj:
	@tmp=$$(mktemp) && jq '.TECHNO_VERSION.major += 1 | .TECHNO_VERSION.minor = 0 | .TECHNO_VERSION.patch = 0 | .TECHNO_VERSION.pre = null' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

inc_min:
	@tmp=$$(mktemp) && jq '.TECHNO_VERSION.minor += 1 | .TECHNO_VERSION.patch = 0 | .TECHNO_VERSION.pre = null' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

inc_pat:
	@tmp=$$(mktemp) && jq '.TECHNO_VERSION.patch += 1 | .TECHNO_VERSION.pre = null' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

# make set_pre KIND=rc N=1
set_pre:
	@test -n "${KIND}" || { echo "set_pre: KIND=dev|alpha|beta|rc required" >&2; exit 1; }
	@test -n "${N}" || { echo "set_pre: N=<int> required" >&2; exit 1; }
	@tmp=$$(mktemp) && jq --arg k '${KIND}' --argjson n '${N}' '.TECHNO_VERSION.pre = {kind:$$k, n:$$n}' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

clear_pre:
	@tmp=$$(mktemp) && jq '.TECHNO_VERSION.pre = null' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}
