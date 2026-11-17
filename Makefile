SHELL := /bin/bash
ENV := source scripts/emulator-env.sh &&

.PHONY: seed up down rebuild sync test report demo lint smoke

REGIONS ?=

up:        ## start the emulator; REGIONS=us-east-1,eu-west-2 gives each region its own emulator
	REGIONS=$(REGIONS) ./scripts/emulator-up.sh

down:      ## stop the emulator(s); state is discarded (the web console is never touched)
	./scripts/emulator-down.sh

rebuild:   ## force-rebuild the image (picks up latest LTS RPM)
	REBUILD=1 ./scripts/emulator-up.sh

sync:
	uv sync

smoke:     ## AWS CLI sanity check across the POC services
	$(ENV) aws sts get-caller-identity && \
		aws dynamodb list-tables && aws s3api list-buckets && aws lambda list-functions --query 'Functions[].FunctionName' && \
		aws apigateway get-rest-apis --query 'items[].name' && aws stepfunctions list-state-machines && \
		aws events list-event-buses --query 'EventBuses[].Name' && aws appsync list-graphql-apis --query 'graphqlApis[].name'

SERVICE ?=
REPORTS := reports

test: sync ## all suites, or one: make test SERVICE=lambda
	mkdir -p $(REPORTS)
	$(ENV) uv run pytest -v tests/$(SERVICE) \
		--junitxml=$(REPORTS)/junit.xml \
		--html=$(REPORTS)/report.html --self-contained-html
	@echo "==> HTML report: $(REPORTS)/report.html   JUnit: $(REPORTS)/junit.xml"

report: ## open the last HTML test report
	xdg-open $(REPORTS)/report.html 2>/dev/null || open $(REPORTS)/report.html

demo: sync ## DynamoDB CaptureRepository walkthrough
	$(ENV) uv run python demo.py

lint: sync
	uv run pre-commit run --all-files

seed: sync ## create linked demo resources in every POC service
	$(ENV) uv run python scripts/seed.py
