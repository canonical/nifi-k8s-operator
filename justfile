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

# Set up Gitea in the current Kubernetes namespace for git-integrator/NiFi tests.
setup-gitea:
	#!/usr/bin/env bash
	set -euo pipefail

	for cmd in kubectl curl python3; do
		if ! command -v "${cmd}" >/dev/null 2>&1; then
			echo "Missing required command: ${cmd}" >&2
			exit 1
		fi
	done

	GITEA_NAMESPACE="${GITEA_NAMESPACE:-$(kubectl config view --minify --output 'jsonpath={..namespace}' 2>/dev/null || true)}"
	GITEA_NAMESPACE="${GITEA_NAMESPACE:-default}"
	GITEA_USER="${GITEA_USER:-nifi}"
	GITEA_PASSWORD="${GITEA_PASSWORD:-nifi-integration-password}"
	GITEA_EMAIL="${GITEA_EMAIL:-nifi@example.com}"
	GITEA_REPO="${GITEA_REPO:-nifi-flows}"
	GITEA_LOCAL_PORT="${GITEA_LOCAL_PORT:-3000}"
	GITEA_TIMEOUT="${GITEA_TIMEOUT:-180s}"

	kubectl get namespace "${GITEA_NAMESPACE}" >/dev/null
	kubectl -n "${GITEA_NAMESPACE}" apply -f tests/integration/gitea.yaml
	kubectl -n "${GITEA_NAMESPACE}" rollout status deployment/gitea --timeout="${GITEA_TIMEOUT}"
	kubectl -n "${GITEA_NAMESPACE}" wait --for=condition=Ready pod -l app.kubernetes.io/name=gitea --timeout="${GITEA_TIMEOUT}"

	if ! kubectl -n "${GITEA_NAMESPACE}" exec deployment/gitea -- gitea admin user list | awk '{print $2}' | grep -qx "${GITEA_USER}"; then
		kubectl -n "${GITEA_NAMESPACE}" exec deployment/gitea -- \
			gitea admin user create \
				--username "${GITEA_USER}" \
				--password "${GITEA_PASSWORD}" \
				--email "${GITEA_EMAIL}" \
				--admin \
				--must-change-password=false
	fi

	forward_log="$(mktemp)"
	kubectl -n "${GITEA_NAMESPACE}" port-forward svc/gitea-http "${GITEA_LOCAL_PORT}:3000" >"${forward_log}" 2>&1 &
	forward_pid="$!"
	trap 'kill "${forward_pid}" >/dev/null 2>&1 || true; rm -f "${forward_log}"' EXIT

	for _ in {1..30}; do
		if curl -fsS "http://127.0.0.1:${GITEA_LOCAL_PORT}/api/healthz" >/dev/null 2>&1; then
			break
		fi
		sleep 1
	done
	curl -fsS "http://127.0.0.1:${GITEA_LOCAL_PORT}/api/healthz" >/dev/null

	repo_status="$(curl -sS -o /tmp/gitea-repo-response.json -w '%{http_code}' \
		-u "${GITEA_USER}:${GITEA_PASSWORD}" \
		-H 'Content-Type: application/json' \
		-d "{\"name\":\"${GITEA_REPO}\",\"auto_init\":true,\"private\":false}" \
		"http://127.0.0.1:${GITEA_LOCAL_PORT}/api/v1/user/repos")"
	if [[ "${repo_status}" != "201" && "${repo_status}" != "409" ]]; then
		cat /tmp/gitea-repo-response.json >&2
		exit 1
	fi

	token_name="nifi-integration-$(date +%s)"
	token_response="$(curl -fsS \
		-u "${GITEA_USER}:${GITEA_PASSWORD}" \
		-H 'Content-Type: application/json' \
		-d "{\"name\":\"${token_name}\"}" \
		"http://127.0.0.1:${GITEA_LOCAL_PORT}/api/v1/users/${GITEA_USER}/tokens")"
	token="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["sha1"])' <<<"${token_response}")"

	echo "Gitea is ready."
	echo "Repository URL for workloads in namespace ${GITEA_NAMESPACE}: http://gitea-http.${GITEA_NAMESPACE}.svc.cluster.local:3000/${GITEA_USER}/${GITEA_REPO}.git"
	echo "Repository URL from this machine while port-forwarding: http://127.0.0.1:${GITEA_LOCAL_PORT}/${GITEA_USER}/${GITEA_REPO}.git"
	echo "Username: ${GITEA_USER}"
	echo "Personal access token: ${token}"
