#!/bin/bash
# Integration test for NiFi git-registry relation
# This script sets up Gitea, configures git-integrator, and validates the integration

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Get Juju model namespace
log_info "Detecting Juju model namespace..."
MODEL_NS=$(juju status --format json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['model']['name'])" || echo "")

if [ -z "$MODEL_NS" ]; then
    log_error "Could not detect Juju model. Is Juju configured?"
    exit 1
fi

log_info "Using namespace: $MODEL_NS"

# Cleanup function
cleanup() {
    log_info "Cleaning up..."
    pkill -f "kubectl port-forward.*gitea" 2>/dev/null || true
    pkill -f "kubectl port-forward.*nifi" 2>/dev/null || true
}
trap cleanup EXIT

# Deploy Gitea
log_info "Deploying Gitea..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kubectl apply -n "$MODEL_NS" -f "$SCRIPT_DIR/gitea-manifest.yaml"

log_info "Waiting for Gitea to be ready..."
kubectl wait -n "$MODEL_NS" deployment/gitea --for=condition=Available --timeout=180s

# Port-forward to Gitea
log_info "Setting up port-forward to Gitea..."
kubectl port-forward -n "$MODEL_NS" svc/gitea 3000:3000 > /dev/null 2>&1 &
PF_PID=$!
sleep 5

# Wait for Gitea to be healthy
log_info "Waiting for Gitea to be healthy..."
for i in {1..30}; do
    if curl -sf http://localhost:3000/api/healthz > /dev/null 2>&1; then
        log_info "Gitea is healthy"
        break
    fi
    if [ $i -eq 30 ]; then
        log_error "Gitea did not become healthy in time"
        exit 1
    fi
    sleep 2
done

# Note: Gitea auto-install happens on first access when INSTALL_LOCK=false
# We need to manually set it up via web UI once, or use a pre-configured setup
log_warn "Gitea requires manual initial setup at http://localhost:3000/install"
log_warn "Create admin user: nifi-admin / nifi-test-password"
log_warn "Press Enter when setup is complete..."
read -r

# Create test repository
log_info "Creating test repository..."
REPO_CREATE=$(curl -s -X POST http://localhost:3000/api/v1/user/repos \
    -u nifi-admin:nifi-test-password \
    -H "Content-Type: application/json" \
    -d '{
        "name": "nifi-flows",
        "description": "Test repository for NiFi flow versioning",
        "private": false,
        "auto_init": true
    }' 2>&1)

if echo "$REPO_CREATE" | grep -q '"id"'; then
    log_info "Repository created successfully"
elif echo "$REPO_CREATE" | grep -q "already exists"; then
    log_warn "Repository already exists, continuing..."
else
    log_error "Failed to create repository: $REPO_CREATE"
    exit 1
fi

# Generate access token
log_info "Generating access token..."
TOKEN_RESPONSE=$(curl -s -X POST http://localhost:3000/api/v1/users/nifi-admin/tokens \
    -u nifi-admin:nifi-test-password \
    -H "Content-Type: application/json" \
    -d '{
        "name": "nifi-integration-'$(date +%s)'",
        "scopes": ["write:repository", "read:repository"]
    }')

GITEA_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sha1', ''))" 2>/dev/null || echo "")

if [ -z "$GITEA_TOKEN" ]; then
    log_error "Failed to generate token: $TOKEN_RESPONSE"
    exit 1
fi

log_info "Token generated successfully"

# Check if git-integrator is deployed
if ! juju status git-integrator &>/dev/null; then
    log_info "Deploying git-integrator..."
    juju deploy git-integrator --channel edge
    juju wait-for application git-integrator --query='status=="active"' --timeout=5m
fi

# Configure git-integrator
log_info "Configuring git-integrator..."
juju config git-integrator \
    repository_url="http://gitea.${MODEL_NS}.svc.cluster.local:3000/nifi-admin/nifi-flows.git" \
    tracking_ref="main" \
    authentication_method="credentials" \
    credentials_username="nifi-admin" \
    credentials_personal_access_token="$GITEA_TOKEN"

# Integrate with NiFi
log_info "Integrating nifi-k8s with git-integrator..."
if ! juju status nifi-k8s git-integrator --format json 2>/dev/null | grep -q "git-registry"; then
    juju integrate nifi-k8s git-integrator
fi

log_info "Waiting for integration to complete..."
juju wait-for application nifi-k8s --query='status=="active"' --timeout=5m

sleep 10  # Give NiFi time to process the relation

# Verify registry client was created
log_info "Verifying registry client creation..."
REGISTRIES=$(juju exec --unit nifi-k8s/0 -- curl -s http://localhost:8080/nifi-api/controller/registry-clients)

if echo "$REGISTRIES" | grep -q "juju-git-registry"; then
    log_info "✓ Registry client 'juju-git-registry' created successfully"
    
    # Show details
    echo "$REGISTRIES" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for reg in data.get('registries', []):
    comp = reg.get('component', {})
    if comp.get('name') == 'juju-git-registry':
        print(f\"  Type: {comp.get('type', 'unknown')}\")
        print(f\"  Properties: {json.dumps(comp.get('properties', {}), indent=4)}\")
" 2>/dev/null || log_warn "Could not parse registry details"
else
    log_error "Registry client was not created"
    log_error "Response: $REGISTRIES"
    exit 1
fi

# Test relation removal
log_info "Testing relation removal..."
juju remove-relation nifi-k8s git-integrator

log_info "Waiting for relation to be removed..."
sleep 15

# Verify registry client was deleted
log_info "Verifying registry client deletion..."
REGISTRIES_AFTER=$(juju exec --unit nifi-k8s/0 -- curl -s http://localhost:8080/nifi-api/controller/registry-clients)

if echo "$REGISTRIES_AFTER" | grep -q '"registries":\[\]' || ! echo "$REGISTRIES_AFTER" | grep -q "juju-git-registry"; then
    log_info "✓ Registry client deleted successfully"
else
    log_error "Registry client was not deleted"
    log_error "Response: $REGISTRIES_AFTER"
    exit 1
fi

# Verify NiFi is still active
log_info "Verifying NiFi status..."
NIFI_STATUS=$(juju status nifi-k8s --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['applications']['nifi-k8s']['status']['status'])")

if [ "$NIFI_STATUS" = "active" ]; then
    log_info "✓ NiFi is still Active after relation removal (git-registry is optional)"
else
    log_error "NiFi status is $NIFI_STATUS (expected: active)"
    exit 1
fi

log_info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log_info "✓ All integration tests passed!"
log_info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Cleanup instructions
log_info ""
log_info "To clean up Gitea:"
log_info "  kubectl delete -n $MODEL_NS -f $SCRIPT_DIR/gitea-manifest.yaml"
log_info ""
log_info "To remove git-integrator:"
log_info "  juju remove-application git-integrator --force"
