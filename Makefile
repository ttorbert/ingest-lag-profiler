# Ingest Lag Profiler
#
#   make test                       offline checks: lint, unit tests, quick formula simulation
#   make lab-up                     start the Rancher Desktop lab and install the pack into it
#   make lab-owner                  save the workflow as a low-privilege lab user
#   make lab-down | lab-reset       stop the lab (reset also deletes its data)
#   make lab-traffic                send lab events (Ctrl-C to stop)
#   make check|install|status|uninstall TARGET=targets/prod.env
#   make test-pipelines             run the ingest pipeline fixtures against the lab
#   make validate [RUN=name]        lab validation against real rule alerts (~105 min)
#   make dashboard                  check the dashboard queries and rebuild its JSON
#   make simulate                   full formula simulation (100k trials per point)

SHELL      := /bin/bash
PYTHON     ?= python3
RD_BIN     := $(HOME)/.rd/bin
# Rancher Desktop's docker CLI and its credential helpers live here. macOS make 3.81
# looks up commands with its startup PATH, so DOCKER is a full path; the exported
# PATH lets docker find its credential helpers.
export PATH := $(RD_BIN):$(PATH)
DOCKER     ?= $(shell command -v docker 2>/dev/null || echo $(RD_BIN)/docker)
LAB_DIR    := scripts/lab
COMPOSE    := $(DOCKER) compose -f $(LAB_DIR)/docker-compose.yml --env-file $(LAB_DIR)/.env
TARGET     ?= targets/docker.env
INSTALL_ARGS ?=
RUN        ?= $(shell date +%Y%m%d-%H%M)
# Load lab passwords so targets/docker.env can use ${ELASTIC_PASSWORD}.
WITH_LAB_ENV := set -a; [ -f $(LAB_DIR)/.env ] && . $(LAB_DIR)/.env; set +a;

.PHONY: help test lint unit simulate lab-prep lab-up lab-owner lab-down lab-reset lab-logs lab-traffic \
        check install status uninstall test-pipelines validate dashboard package

help:
	@sed -n '2,12p' Makefile | sed 's/^# \{0,1\}//'

# ---- offline -------------------------------------------------------------------------

test: lint unit
	$(PYTHON) tests/model/simulate_rule_schedule.py --trials 20000

lint:
	$(PYTHON) scripts/lagprof.py lint

unit:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -t .

simulate:
	$(PYTHON) tests/model/simulate_rule_schedule.py --markdown

# ---- lab (Rancher Desktop) -------------------------------------------------------------

lab-prep:
	$(LAB_DIR)/rancher-setup.sh
	$(LAB_DIR)/init-env.sh

lab-up: lab-prep
	$(COMPOSE) up -d
	$(LAB_DIR)/wait-ready.sh
	$(WITH_LAB_ENV) $(PYTHON) scripts/lagprof.py install --target-file targets/docker.env $(INSTALL_ARGS)
	@echo "Kibana: http://localhost:5601  (user elastic, password in $(LAB_DIR)/.env)"

lab-owner:
	$(WITH_LAB_ENV) $(PYTHON) $(LAB_DIR)/lab-owner.py
	$(WITH_LAB_ENV) LAGPROF_WORKFLOW_API_KEY=$$(cat build/lab-owner-key) $(PYTHON) scripts/lagprof.py install --target-file targets/docker.env $(INSTALL_ARGS)

lab-down:
	$(COMPOSE) down

lab-reset:
	$(COMPOSE) down -v
	rm -rf $(LAB_DIR)/certs

lab-logs:
	$(COMPOSE) logs -f --tail 100

lab-traffic:
	$(WITH_LAB_ENV) $(PYTHON) $(LAB_DIR)/generate-events.py --target-file targets/docker.env

# ---- any target ---------------------------------------------------------------------

check install status uninstall:
	$(WITH_LAB_ENV) $(PYTHON) scripts/lagprof.py $@ --target-file $(TARGET) $(if $(filter install,$@),$(INSTALL_ARGS))

validate:
	$(WITH_LAB_ENV) $(PYTHON) $(LAB_DIR)/validate.py setup
	$(WITH_LAB_ENV) caffeinate -i $(PYTHON) -u $(LAB_DIR)/validate.py send --run $(RUN) --per-lag 600 --minutes 90
	@echo "Waiting 16 minutes for the last rule runs..."; caffeinate -i sleep 960
	$(WITH_LAB_ENV) $(PYTHON) $(LAB_DIR)/validate.py score --run $(RUN)

dashboard:
	$(WITH_LAB_ENV) $(PYTHON) kibana/dashboards/build.py --check --target-file $(TARGET)

test-pipelines:
	$(WITH_LAB_ENV) $(PYTHON) tests/pipelines/run_simulate.py --target-file $(TARGET)

# ---- release --------------------------------------------------------------------------

package:
	@mkdir -p dist
	zip -qr dist/ingest-lag-profiler-$$(cat VERSION).zip VERSION README.md elasticsearch esql kibana \
	    scripts/lagprof.py targets/*.example docs
	@echo "dist/ingest-lag-profiler-$$(cat VERSION).zip"
