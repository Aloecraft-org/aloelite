ROOT_DIR:=$(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
__TECHNO_PROJECT_FILE:=${ROOT_DIR}/.technoproj

-include ${ROOT_DIR}/script/version.mk
-include ${ROOT_DIR}/script/python.mk

echo:
	@echo VERSION: ${__VERSION_FULL}
	@echo TAG: ${__TAG}

clean:

	rm -rf dist/ build/ *.egg-info .ruff_cache .pytest_cache .html_doc __pycache__

build:

	python3 -m build

twine-upload:

	twine upload dist/*

docgen:
	mkdir -p .html_doc
	pandoc README.md -o .html_doc/readme.html -f markdown+emoji
	python3 script/pydocgen.py aloelite/ .html_doc --title "aloelite" --readme .html_doc/readme.html
	(cd .html_doc && python3 -m http.server)