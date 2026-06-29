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

	: "${GITEA_USER:=nifi}"
	: "${GITEA_PASSWORD:=nifi-integration-password}"
	: "${GITEA_EMAIL:=nifi@example.com}"
	: "${GITEA_REPO:=nifi-flows}"
	: "${GITEA_TIMEOUT:=180s}"

	ns="$(kubectl config view --minify -o 'jsonpath={..namespace}' 2>/dev/null || true)"
	: "${GITEA_NAMESPACE:=${ns:-default}}"

	api() { kubectl -n "${GITEA_NAMESPACE}" exec deployment/gitea -- \
		curl -fsS -u "${GITEA_USER}:${GITEA_PASSWORD}" \
		-H 'Content-Type: application/json' "$@"; }

	echo ">>> Deploying Gitea into namespace ${GITEA_NAMESPACE}..."
	kubectl -n "${GITEA_NAMESPACE}" apply -f tests/integration/gitea.yaml
	kubectl -n "${GITEA_NAMESPACE}" rollout status deployment/gitea --timeout="${GITEA_TIMEOUT}"

	admin_exec() { kubectl -n "${GITEA_NAMESPACE}" exec deployment/gitea -- \
		su git -s /bin/bash -c "gitea $*"; }

	echo ">>> Creating admin user (idempotent)..."
	if ! admin_exec 'admin user list' 2>/dev/null | awk '{print $2}' | grep -qx "${GITEA_USER}"; then
		admin_exec "admin user create \
			--username ${GITEA_USER} \
			--password ${GITEA_PASSWORD} \
			--email ${GITEA_EMAIL} \
			--admin --must-change-password=false"
	fi

	echo ">>> Creating repository ${GITEA_REPO} (idempotent)..."
	api -o /dev/null -w '' \
		-d "{\"name\":\"${GITEA_REPO}\",\"auto_init\":true,\"private\":false}" \
		"http://localhost:3000/api/v1/user/repos" 2>/dev/null || true

	echo ">>> Generating personal access token..."
	token_name="nifi-integration-$(date +%s)"
	token="$(api -d "{\"name\":\"${token_name}\",\"scopes\":[\"all\"]}" \
		"http://localhost:3000/api/v1/users/${GITEA_USER}/tokens" \
		| grep -o '"sha1":"[^"]*"' | cut -d'"' -f4)"

	svc_url="http://gitea-http.${GITEA_NAMESPACE}.svc.cluster.local:3000/${GITEA_USER}/${GITEA_REPO}.git"

	echo ""
	echo "=== Gitea ready ==="
	echo "In-cluster repo URL : ${svc_url}"
	echo "Username            : ${GITEA_USER}"
	echo "Access token        : ${token}"
	echo ""
	echo "To open the Gitea UI locally:"
	echo "  kubectl -n ${GITEA_NAMESPACE} port-forward svc/gitea-http 3000:3000"
	echo "  open http://localhost:3000  (login: ${GITEA_USER} / ${GITEA_PASSWORD})"
	echo ""
	echo "To configure git-integrator:"
	echo "  juju deploy git-integrator --channel edge \\"
	echo "    --config repo=${svc_url} \\"
	echo "    --config auth-type=token \\"
	echo "    --config token=${token} \\"
	echo "    --config branch=main"
	echo "  juju integrate nifi-k8s:git-registry git-integrator:git-integrator"
