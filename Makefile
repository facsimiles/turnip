# Copyright 2005-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

ENV := $(CURDIR)/env
PIP_CACHE = $(CURDIR)/pip-cache

PYTHON := $(ENV)/bin/python
PSERVE := $(ENV)/bin/pserve
CELERY := $(ENV)/bin/celery
PIP := $(ENV)/bin/pip
VIRTUALENV := /usr/bin/virtualenv
VENV_ARGS := -p python3

DEPENDENCIES_URL := https://git.launchpad.net/~canonical-launchpad-branches/turnip/+git/dependencies
PIP_SOURCE_DIR := dependencies

# virtualenv and pip fail if setlocale fails, so force a valid locale.
PIP_ENV := LC_ALL=C.UTF-8
# "make PIP_QUIET=0" causes pip to be verbose.
PIP_QUIET := 1
PIP_ENV += PIP_QUIET=$(PIP_QUIET)
PIP_FIND_LINKS := file://$(PIP_CACHE)/
ifneq ($(PIP_SOURCE_DIR),)
PIP_ENV += PIP_NO_INDEX=1
PIP_FIND_LINKS += file://$(shell readlink -f $(PIP_SOURCE_DIR))/
endif
PIP_ENV += PIP_FIND_LINKS="$(PIP_FIND_LINKS)"

# Create archives in labelled directories (e.g.
# <rev-id>/$(PROJECT_NAME).tar.gz)
TARBALL_BUILD_LABEL ?= $(shell git rev-parse HEAD)-$(shell lsb_release -cs)
TARBALL_FILE_NAME = turnip.tar.gz
TARBALL_BUILDS_DIR ?= build
TARBALL_BUILD_DIR = $(TARBALL_BUILDS_DIR)/$(TARBALL_BUILD_LABEL)
TARBALL_BUILD_PATH = $(TARBALL_BUILD_DIR)/$(TARBALL_FILE_NAME)

SWIFT_CONTAINER_NAME ?= turnip-builds
# This must match the object path used by install_payload in the turnip-base
# charm layer.
SWIFT_OBJECT_PATH = turnip-builds/$(TARBALL_BUILD_LABEL)/$(TARBALL_FILE_NAME)

build: $(ENV)

$(PIP_SOURCE_DIR):
	git clone $(DEPENDENCIES_URL) $(PIP_SOURCE_DIR)

bootstrap:
	if [ -d dependencies ]; then \
		git -C dependencies pull; \
	else \
		git clone $(DEPENDENCIES_URL) dependencies; \
	fi
	$(MAKE) PIP_SOURCE_DIR=dependencies

turnip/version_info.py:
	echo 'version_info = {"revision_id": "$(TARBALL_BUILD_LABEL)"}' >$@

$(ENV): turnip/version_info.py
ifeq ($(PIP_SOURCE_DIR),)
	@echo "Set PIP_SOURCE_DIR to the path of a clone of" >&2
	@echo "$(DEPENDENCIES_URL)." >&2
	@exit 1
endif
	mkdir -p $(ENV)
	(echo '[easy_install]'; \
	 echo 'find_links = file://$(realpath $(PIP_SOURCE_DIR))/') \
		>$(ENV)/.pydistutils.cfg
	$(PIP_ENV) $(VIRTUALENV) $(VENV_ARGS) --never-download $(ENV)
	$(PIP_ENV) $(PIP) install -r bootstrap-requirements.txt
	$(PIP_ENV) $(PIP) install -c requirements.txt \
		-e '.[test,deploy]'

bootstrap-test: PATH := /usr/sbin:/sbin:$(PATH)
bootstrap-test:
	-sudo rabbitmqctl delete_vhost turnip-test-vhost
	-sudo rabbitmqctl add_vhost turnip-test-vhost
	-sudo rabbitmqctl set_permissions -p "turnip-test-vhost" "guest" ".*" ".*" ".*"

COVERAGE := $(ENV)/bin/coverage

test: $(ENV) bootstrap-test
	$(COVERAGE) run -m unittest discover $(ARGS) turnip
	$(COVERAGE) report

# XXX jugmac00 2022-01-13:
# this is a temporary solution to enable selecting single tests more easily
# test setup will be redone in https://warthogs.atlassian.net/browse/LP-598
# sample command:
# make runpytest ARGS="-k expression"
runpytest: $(ENV) bootstrap-test
	$(PYTHON) -m pip install pdbpp pytest
	$(PYTHON) -m pytest $(ARGS)

clean:
	find turnip -name '*.py[co]' -exec rm '{}' \;
	rm -rf $(ENV) $(PIP_CACHE)
	rm -f turnip/version_info.py

dist:
	python3 ./setup.py sdist

TAGS:
	ctags -e -R turnip

tags:
	ctags -R turnip

pip-check: $(ENV)
	$(PIP) check

check: pip-check test

run-api: $(ENV)
	$(PSERVE) api.ini --reload

run-pack: $(ENV)
	$(PYTHON) turnipserver.py

run-worker: $(ENV)
	$(CELERY) -A turnip.tasks worker -n default-worker \
		--loglevel=debug \
		--concurrency=20 \
		--pool=gevent \
		--prefetch-multiplier=1 \
		--queue=celery

run-repack-worker: $(ENV)
	$(CELERY) -A turnip.tasks worker -n repack-worker \
		--loglevel=debug \
		--concurrency=1 \
		--prefetch-multiplier=1 \
		--queue=repacks \
		-O=fair

run:
	make run-api &\
	make run-pack &\
	make run-repack-worker&\
	make run-worker&\
	wait;

stop:
	-pkill -f 'make run-api'
	-pkill -f 'make run-pack'
	-pkill -f 'make run-worker'
	-pkill -f 'make run-repack-worker'	
	-pkill -f '$(CELERY) -A turnip.tasks worker default-worker'
	-pkill -f '$(CELERY) -A turnip.tasks worker repack-worker'

$(PIP_CACHE): $(ENV)
	mkdir -p $(PIP_CACHE)
	$(PIP_ENV) $(PIP) install -d $(PIP_CACHE) \
		-r bootstrap-requirements.txt \
		-r requirements.txt

# XXX cjwatson 2015-10-16: limit to only interesting files
build-tarball: $(PIP_SOURCE_DIR)
	@echo "Creating deployment tarball at $(TARBALL_BUILD_PATH)"
	rm -rf $(PIP_CACHE)
	$(MAKE) $(PIP_CACHE)
	mkdir -p $(TARBALL_BUILD_DIR)
	tar -czf $(TARBALL_BUILD_PATH) \
		--exclude-vcs \
		--exclude build \
		--exclude charm \
		--exclude dist \
		--exclude env \
		./

publish-tarball: build-tarball
	[ ! -e ~/.config/swift/turnip ] || . ~/.config/swift/turnip; \
	./publish-to-swift --debug \
		$(SWIFT_CONTAINER_NAME) $(SWIFT_OBJECT_PATH) \
		$(TARBALL_BUILD_PATH) turnip=$(TARBALL_BUILD_LABEL)

copy-certificates:
	mkdir -p /var/lib/haproxy
	cat turnip.crt turnip.key > /var/lib/haproxy/default.pem

copy-haproxy-turnip-http-config:
	sed -i '/^\# BEGIN TURNIP$$/,$$d' /etc/haproxy/haproxy.cfg
	echo '# BEGIN TURNIP' >> /etc/haproxy/haproxy.cfg
	cat haproxy-turnip-http.cfg >> /etc/haproxy/haproxy.cfg

reload-haproxy: copy-certificates copy-haproxy-turnip-http-config
	systemctl reload haproxy

trust-lp-dev-cert:
	cp launchpad-test.crt /usr/local/share/ca-certificates/
	update-ca-certificates

install-cgit: reload-haproxy trust-lp-dev-cert

# Build wheels from vendored sdists that cannot be installed directly by
# pip 9.x (e.g. packages using pyproject.toml with hatchling/flit build
# systems).  Uses only system Python and vendored sdists — no internet
# access, no pre-built binaries from PyPI.
#
# Trust chain:
#   system python3 (Ubuntu) → pip from vendored sdist → build tools from
#   vendored sdists (--no-build-isolation) → wheel built from vendored sdist
#
# To rebuild:  make build-testtools-wheel
# To verify:   compare the .whl contents against the corresponding sdist

WHEEL_BUILD_ENV := $(CURDIR)/.testtools-wheel-build-env
WHEEL_BUILD_SDISTS := $(PIP_SOURCE_DIR)/testtools-wheel-build
WHEEL_PIP := $(WHEEL_BUILD_ENV)/bin/pip
WHEEL_PYTHON := $(WHEEL_BUILD_ENV)/bin/python
WHEEL_FLAGS := --no-build-isolation --no-index --no-deps --find-links=file://$(shell readlink -f $(WHEEL_BUILD_SDISTS))/

build-testtools-wheel: $(PIP_SOURCE_DIR)
	rm -rf $(WHEEL_BUILD_ENV)
	python3 -m venv $(WHEEL_BUILD_ENV)
	# Bootstrap pip from vendored sdist
	cd /tmp && tar xzf $(CURDIR)/$(WHEEL_BUILD_SDISTS)/pip-22.3.1.tar.gz && \
		cd pip-22.3.1 && $(WHEEL_PYTHON) -m pip install --no-deps . && \
		rm -rf /tmp/pip-22.3.1
	# Layer 0: self-hosting packages (no external build deps)
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/flit_core-3.12.0.tar.gz
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/setuptools-75.3.4.tar.gz
	# Layer 1: packages built with flit_core
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/tomli-2.0.2.tar.gz
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/packaging-26.0.tar.gz
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/pathspec-0.12.1.tar.gz
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/typing_extensions-4.13.2.tar.gz
	# Layer 2: packages built with setuptools
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/calver-2022.6.26.tar.gz
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/trove_classifiers-2026.1.14.14.tar.gz
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/setuptools_scm-9.2.2.tar.gz
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/pluggy-1.5.0.tar.gz
	# Layer 3: hatchling (self-hosting) and hatch-vcs
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/hatchling-1.27.0.tar.gz
	$(WHEEL_PIP) install $(WHEEL_FLAGS) $(WHEEL_BUILD_SDISTS)/hatch_vcs-0.4.0.tar.gz
	# Build testtools wheel from the main dependencies directory
	$(WHEEL_PIP) wheel --no-build-isolation --no-index --no-deps \
		--find-links=file://$(shell readlink -f $(PIP_SOURCE_DIR))/ \
		--find-links=file://$(shell readlink -f $(WHEEL_BUILD_SDISTS))/ \
		--wheel-dir=$(PIP_SOURCE_DIR) \
		$(PIP_SOURCE_DIR)/testtools-2.7.2.tar.gz
	rm -rf $(WHEEL_BUILD_ENV)
	@echo "Built: $(PIP_SOURCE_DIR)/testtools-2.7.2-py3-none-any.whl"

.PHONY: build check clean dist run-api run-pack test build-testtools-wheel
.PHONY: build-tarball publish-tarball
.PHONY: copy-certificates copy-haproxy-turnip-http-config install-cgit reload-haproxy trust-lp-dev-cert
