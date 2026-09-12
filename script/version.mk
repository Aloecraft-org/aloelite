# The version, in the three spellings doc/ALIGNMENT.md §1 derives from one
# source. `.technoproj` holds the only numbers a human edits; everything
# below is mechanical, so no two spellings can drift.
#
#   .technoproj pre      tag             PEP 440       SemVer / Cargo
#   null                 v0.5.0          0.5.0         0.5.0
#   {dev, 7}             v0.5.0-dev.7    0.5.0.dev7    0.5.0-dev.7
#   {alpha, 1}           v0.5.0-alpha.1  0.5.0a1       0.5.0-alpha.1
#   {beta, 2}            v0.5.0-beta.2   0.5.0b2       0.5.0-beta.2
#   {rc, 1}              v0.5.0-rc.1     0.5.0rc1      0.5.0-rc.1
#
# The dot before the number is load-bearing and not decoration: SemVer
# compares dot-separated identifiers, so `dev.10` sorts after `dev.2` while
# `dev10` sorts before `dev2`. ALIGNMENT.md §1 has the worked case.
__VER_MAJ:=$(shell jq -r '.TECHNO_VERSION.major' ${__TECHNO_PROJECT_FILE})
__VER_MIN:=$(shell jq -r '.TECHNO_VERSION.minor' ${__TECHNO_PROJECT_FILE})
__VER_PAT:=$(shell jq -r '.TECHNO_VERSION.patch' ${__TECHNO_PROJECT_FILE})
__VER_PRE_KIND:=$(shell jq -r '.TECHNO_VERSION.pre.kind // ""' ${__TECHNO_PROJECT_FILE})
__VER_PRE_N:=$(shell jq -r '.TECHNO_VERSION.pre.n // ""' ${__TECHNO_PROJECT_FILE})

__VERSION:=${__VER_MAJ}.${__VER_MIN}.${__VER_PAT}

# PEP 440's marker per kind, as a table rather than a chain of ifs. `dev`
# takes a dot; the rest are glued straight on.
__PEP440_dev:=.dev
__PEP440_alpha:=a
__PEP440_beta:=b
__PEP440_rc:=rc

__VERSION_PEP440:=${__VERSION}$(if ${__VER_PRE_KIND},${__PEP440_${__VER_PRE_KIND}}${__VER_PRE_N})
__VERSION_SEMVER:=${__VERSION}$(if ${__VER_PRE_KIND},-${__VER_PRE_KIND}.${__VER_PRE_N})
__VERSION_FULL:=${__VERSION_PEP440}

# The git tag is canonical and every spelling above derives from it.
__TAG:=v${__VERSION_SEMVER}

_sync_version:
	@echo "$$(tq -f pyproject.toml '.' -o=json | jq '.package.version="${__VERSION_PEP440}"' | jyt jt)" > pyproject.toml

inc_maj:
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.major += 1' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

inc_min:
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.minor += 1' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

inc_pat:
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.patch += 1' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

# `make pre_set KIND=rc N=1`, `make pre_bump`, `make pre_clear`. These replace
# the old `inc_build`, whose single number meant three different things in
# three repositories (ALIGNMENT.md §2).
pre_set:
	@test -n "${KIND}" -a -n "${N}" || { echo "usage: make pre_set KIND=dev|alpha|beta|rc N=<n>" >&2; exit 2; }
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.pre = {"kind":"${KIND}","n":${N}}' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

pre_bump:
	@test -n "${__VER_PRE_KIND}" || { echo "no prerelease to bump; use make pre_set" >&2; exit 2; }
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.pre.n += 1' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

pre_clear:
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.pre = null' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}
