ROOT_DIR:=$(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
__TECHNO_PROJECT_FILE:=${ROOT_DIR}/.technoproj
TECHNO_VERSION:=$(shell jq -r '.TECHNO_VERSION | "\(.major).\(.minor).\(.patch)" + (if .build != 0 and .build != "0" then "\(.build)" else "" end)' .technoproj)

-include ${ROOT_DIR}/script/version.mk
-include ${ROOT_DIR}/script/python.mk

echo:
	@echo ${TECHNO_VERSION}
	@echo VERSION: ${__VERSION_FULL}
	@echo TAG: ${__TAG}

clean:

	rm -rf dist/ build/ *.egg-info .ruff_cache .pytest_cache .html_doc __pycache__

build:

	python3 -m build

build_container:
	docker build -t aloelite .

tag_container:
	docker build -t aloelite -t aloecraft/aloelite -t aloecraft/aloelite:${TECHNO_VERSION} -t aloecraft/aloelite:latest .

push_container:
	docker push aloecraft/aloelite
	docker push aloecraft/aloelite:${TECHNO_VERSION}
	docker push aloecraft/aloelite:latest

twine-upload:

	twine upload dist/*

docgen:
	mkdir -p .html_doc
	pandoc README.md -o .html_doc/readme.html -f markdown+emoji
	python3 script/pydocgen.py aloelite/ .html_doc --title "aloelite" --readme .html_doc/readme.html
	(cd .html_doc && python3 -m http.server)