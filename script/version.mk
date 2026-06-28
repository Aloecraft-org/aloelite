__VER_MAJ:=$(shell jq '.TECHNO_VERSION.major' ${__TECHNO_PROJECT_FILE})
__VER_MIN:=$(shell jq '.TECHNO_VERSION.minor' ${__TECHNO_PROJECT_FILE})
__VER_PAT:=$(shell jq '.TECHNO_VERSION.patch' ${__TECHNO_PROJECT_FILE})
__VER_BLD:=$(shell jq '.TECHNO_VERSION.build' ${__TECHNO_PROJECT_FILE})
__VERSION:=${__VER_MAJ}.${__VER_MIN}.${__VER_PAT}
__VERSION_FULL:=${__VERSION} build ${__VER_BLD}

__TAG:=v${__VERSION}

_sync_version:
	@echo "$$(tq -f pyproject.toml '.' -o=json | jq '.package.version="${__VERSION}"' | jyt jt)" > pyproject.toml

inc_maj:
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.major += 1' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

inc_min:
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.minor += 1' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

inc_pat:
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.patch += 1' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}

inc_build:
	tmp=$$(mktemp) && jq '.TECHNO_VERSION.build += 1' ${__TECHNO_PROJECT_FILE} > "$$tmp" && mv "$$tmp" ${__TECHNO_PROJECT_FILE}