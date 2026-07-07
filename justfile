set export
set fallback


[private]
default:
	just --list

# Run lint
lint:
	uv tool run --python 3.12 tox -e lint

# Run format
format:
	uv tool run --python 3.12 tox -e format

# Run unit tests
unit:
	uv tool run --python 3.12 tox -e unit

# Pack the nifi-k8s charm
pack-charm: clean-charm
	charmcraft pack

# Remove built charm artefacts
clean-charm:
	find . -maxdepth 1 -name "*.charm" -delete

# Destroy the integration test Juju model and remove charm artefacts
clean: clean-charm
	#!/usr/bin/env bash
	juju destroy-model --force --destroy-storage --no-prompt "${JUJU_MODEL:-test}" || true

# Pack the charm and run integration tests against a live Juju/K8s environment.
# Requires a bootstrapped Juju controller with a Kubernetes cloud.
# Usage:
#   just integration              # run all integration tests
#   just integration -k test_name # run a specific test
integration *args: clean pack-charm
	#!/usr/bin/env bash
	set -euo pipefail
	charm=$(ls -t nifi-k8s_*.charm | head -n1)
	if [ -n "{{args}}" ]; then
		export JUJU_MODEL=test
	fi
	uv sync --group integration
	uv run tox -e integration -- --charm-path="${charm}" {{args}}
