ROOT_DIR:=$(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
__TECHNO_PROJECT_FILE:=${ROOT_DIR}/.technoproj

-include ${ROOT_DIR}/script/version.mk
-include ${ROOT_DIR}/script/python.mk

echo:
	@echo VERSION: ${__VERSION_FULL}
	@echo TAG: ${__TAG}

clean:

	rm -rf dist/ build/ *.egg-info

build:

	python3 -m build

twine-upload:

	twine upload dist/*