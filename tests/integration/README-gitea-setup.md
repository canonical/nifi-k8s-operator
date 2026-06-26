# Gitea Setup for NiFi Git-Registry Integration Testing

This guide sets up a local Gitea server in your Kubernetes cluster for testing the NiFi charm's git-registry integration.

## Prerequisites

- Juju model deployed in Kubernetes
- `kubectl` configured to access the cluster
- NiFi charm deployed or ready to deploy

## Setup Steps

### 1. Deploy Gitea in the same namespace as your Juju model

```bash
# Get your Juju model's namespace
MODEL_NS=$(juju status --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['model']['name'])")
echo "Deploying Gitea in namespace: $MODEL_NS"

# Deploy Gitea
kubectl apply -n "$MODEL_NS" -f tests/integration/gitea-manifest.yaml

# Wait for Gitea to be ready
kubectl wait -n "$MODEL_NS" deployment/gitea --for=condition=Available --timeout=180s

# Verify it's running
kubectl get pods -n "$MODEL_NS" -l app=gitea
```

### 2. Complete Gitea Initial Setup

```bash
# Port-forward to access Gitea UI locally
kubectl port-forward -n "$MODEL_NS" svc/gitea 3000:3000 &
PF_PID=$!

# Wait a moment for port-forward to establish
sleep 3

# Open browser to http://localhost:3000/install
# Or use curl to check if it's ready:
curl -s http://localhost:3000/api/healthz
```

**In the Gitea web UI** (http://localhost:3000/install):

1. **Database Settings**: Leave defaults (SQLite3)
2. **General Settings**:
   - Site Title: `NiFi Test Git Registry`
   - Server Domain: `gitea` (the K8s service name)
   - Gitea Base URL: `http://gitea:3000/`
   - SSH Server Port: `22`
3. **Administrator Account**:
   - Username: `nifi-admin`
   - Password: `nifi-test-password`
   - Email: `nifi@example.com`
4. Click **Install Gitea**

### 3. Create a Test Repository

```bash
# Log in to Gitea UI at http://localhost:3000
# User: nifi-admin / Password: nifi-test-password

# Create a new repository:
#   - Name: nifi-flows
#   - Description: Test repository for NiFi flow versioning
#   - Visibility: Public
#   - Initialize: ✓ (with README)
#   - Click "Create Repository"
```

**Or use the Gitea API to create the repo programmatically:**

```bash
# Create the repository via API
curl -X POST http://localhost:3000/api/v1/user/repos \
  -u nifi-admin:nifi-test-password \
  -H "Content-Type: application/json" \
  -d '{
    "name": "nifi-flows",
    "description": "Test repository for NiFi flow versioning",
    "private": false,
    "auto_init": true
  }'
```

### 4. Generate Personal Access Token

**Via Web UI:**
1. Navigate to http://localhost:3000/user/settings/applications
2. Under "Manage Access Tokens", enter:
   - Token Name: `nifi-integration`
   - Click "Generate Token"
3. **Copy the token** (it won't be shown again)

**Or via API:**

```bash
# Generate token via API
TOKEN_RESPONSE=$(curl -X POST http://localhost:3000/api/v1/users/nifi-admin/tokens \
  -u nifi-admin:nifi-test-password \
  -H "Content-Type: application/json" \
  -d '{
    "name": "nifi-integration",
    "scopes": ["write:repository", "read:repository"]
  }')

# Extract the token
GITEA_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['sha1'])")
echo "Gitea Token: $GITEA_TOKEN"

# Save it for later use
echo "$GITEA_TOKEN" > .gitea-token
```

### 5. Deploy and Configure git-integrator

```bash
# Deploy git-integrator if not already deployed
juju deploy git-integrator --channel edge

# Configure it to point at the in-cluster Gitea service
# The repository URL uses the Kubernetes service DNS name
juju config git-integrator \
  repository_url="http://gitea.${MODEL_NS}.svc.cluster.local:3000/nifi-admin/nifi-flows.git" \
  tracking_ref="main" \
  authentication_method="credentials" \
  credentials_username="nifi-admin" \
  credentials_personal_access_token="$(cat .gitea-token)"

# Wait for git-integrator to be ready
juju wait-for application git-integrator --query='status=="active"'
```

### 6. Integrate with NiFi

```bash
# Integrate NiFi with git-integrator
juju integrate nifi-k8s git-integrator

# Wait for the relation to be established
juju wait-for application nifi-k8s --query='status=="active"'

# Verify the integration
juju status nifi-k8s git-integrator
```

### 7. Verify the Registry Client was Created in NiFi

```bash
# Check that NiFi has the registry client configured
juju exec --unit nifi-k8s/0 -- \
  curl -s http://localhost:8080/nifi-api/controller/registry-clients | \
  python3 -m json.tool

# Expected output should show a registry client named "juju-git-registry"
# with properties pointing to the Gitea repository
```

### 8. Test Flow Versioning (Optional)

```bash
# Port-forward to NiFi UI
kubectl port-forward -n "$MODEL_NS" svc/nifi-k8s 8080:8080 &

# Open http://localhost:8080/nifi
# 1. Create a Process Group (drag from toolbar)
# 2. Right-click the Process Group → Version → Start version control
# 3. Select "juju-git-registry" from the Registry dropdown
# 4. Enter flow name and comments
# 5. Click Save

# Verify the flow was committed to Gitea
curl -s http://localhost:3000/api/v1/repos/nifi-admin/nifi-flows/contents \
  -u nifi-admin:nifi-test-password | python3 -m json.tool
```

### 9. Test Relation Removal

```bash
# Remove the relation
juju remove-relation nifi-k8s git-integrator

# Wait for it to process
sleep 10

# Verify the registry client was deleted from NiFi
juju exec --unit nifi-k8s/0 -- \
  curl -s http://localhost:8080/nifi-api/controller/registry-clients | \
  python3 -m json.tool

# Expected: "registries" should be an empty array

# Verify NiFi is still Active (git-registry is optional)
juju status nifi-k8s
```

## Cleanup

```bash
# Kill port-forwards
pkill -f "kubectl port-forward.*gitea"
pkill -f "kubectl port-forward.*nifi"

# Remove the git-integrator integration (if still connected)
juju remove-relation nifi-k8s git-integrator --force || true

# Remove git-integrator application
juju remove-application git-integrator --force

# Delete Gitea from Kubernetes
kubectl delete -n "$MODEL_NS" -f tests/integration/gitea-manifest.yaml

# Clean up saved token
rm -f .gitea-token
```

## Integration Test Automation

For automated integration tests, you can script the entire setup:

```bash
#!/bin/bash
# tests/integration/test-git-registry.sh

set -euo pipefail

MODEL_NS=$(juju status --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['model']['name'])")

# Deploy Gitea
kubectl apply -n "$MODEL_NS" -f tests/integration/gitea-manifest.yaml
kubectl wait -n "$MODEL_NS" deployment/gitea --for=condition=Available --timeout=180s

# Port-forward in background
kubectl port-forward -n "$MODEL_NS" svc/gitea 3000:3000 &
PF_PID=$!
trap "kill $PF_PID" EXIT
sleep 5

# Create admin user via API (after initial install screen is shown)
# Note: First access auto-creates admin if INSTALL_LOCK=false
curl -s http://localhost:3000 > /dev/null

# Create repository
curl -X POST http://localhost:3000/api/v1/user/repos \
  -u nifi-admin:nifi-test-password \
  -H "Content-Type: application/json" \
  -d '{"name":"nifi-flows","auto_init":true}' || true

# Generate token
TOKEN=$(curl -X POST http://localhost:3000/api/v1/users/nifi-admin/tokens \
  -u nifi-admin:nifi-test-password \
  -H "Content-Type: application/json" \
  -d '{"name":"nifi-test"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['sha1'])")

# Configure git-integrator
juju config git-integrator \
  repository_url="http://gitea.${MODEL_NS}.svc.cluster.local:3000/nifi-admin/nifi-flows.git" \
  tracking_ref="main" \
  authentication_method="credentials" \
  credentials_username="nifi-admin" \
  credentials_personal_access_token="$TOKEN"

# Integrate
juju integrate nifi-k8s git-integrator
juju wait-for application nifi-k8s --query='status=="active"' --timeout=5m

# Verify
REGISTRIES=$(juju exec --unit nifi-k8s/0 -- curl -s http://localhost:8080/nifi-api/controller/registry-clients)
echo "$REGISTRIES" | grep -q "juju-git-registry" && echo "✓ Registry client created" || exit 1

# Test removal
juju remove-relation nifi-k8s git-integrator
sleep 15
REGISTRIES_AFTER=$(juju exec --unit nifi-k8s/0 -- curl -s http://localhost:8080/nifi-api/controller/registry-clients)
echo "$REGISTRIES_AFTER" | grep -q '"registries":\[\]' && echo "✓ Registry client deleted" || exit 1

echo "All tests passed!"
```

## Troubleshooting

### Gitea pod won't start
```bash
kubectl logs -n "$MODEL_NS" deployment/gitea
kubectl describe pod -n "$MODEL_NS" -l app=gitea
```

### Can't access Gitea UI
```bash
# Check if service exists
kubectl get svc -n "$MODEL_NS" gitea

# Check if port-forward is running
ps aux | grep "kubectl port-forward.*gitea"

# Try a fresh port-forward
pkill -f "kubectl port-forward.*gitea"
kubectl port-forward -n "$MODEL_NS" svc/gitea 3000:3000 &
```

### NiFi can't reach Gitea
```bash
# Test connectivity from NiFi pod
juju exec --unit nifi-k8s/0 -- \
  curl -v http://gitea.${MODEL_NS}.svc.cluster.local:3000/api/healthz

# If it fails, check if Gitea service is in the correct namespace
kubectl get svc -n "$MODEL_NS" | grep gitea
```

### Git-integrator relation fails
```bash
# Check git-integrator logs
juju debug-log --include git-integrator

# Check NiFi logs
juju debug-log --include nifi-k8s

# Verify configuration
juju config git-integrator
```

## Service DNS Resolution

The Gitea service is accessible within the cluster at:
- **Service Name**: `gitea`
- **Namespace**: `<your-juju-model-namespace>`
- **Full DNS**: `gitea.<namespace>.svc.cluster.local`
- **Port**: 3000 (HTTP), 22 (SSH)

From within the same namespace, you can use just `http://gitea:3000`.
From other namespaces, use the full DNS name: `http://gitea.<namespace>.svc.cluster.local:3000`.
