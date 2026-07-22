# Terraform module for NiFi K8s

This module deploys the NiFi K8s charm using the [Terraform juju provider](https://github.com/juju/terraform-provider-juju/). For provider details, see the [documentation](https://registry.terraform.io/providers/juju/juju/latest/docs).

## Requirements
- Terraform >= 1.12.2
- Provider: `juju` >= 1.0.0
- A Juju model must exist (see [Usage](#usage))

## API

### Inputs
The module offers the following configurable inputs:

| Name | Type | Description | Required |
| - | - | - | - |
| `app_name` | string | Name of the deployed application | False |
| `channel` | string | Channel that the charm is deployed from | False |
| `config` | map(string) | Map of the charm configuration options | False |
| `constraints` | string | Constraints to deploy the charm with | False |
| `model_uuid` | string | UUID of the model that the charm is deployed on | True |
| `revision` | number | Revision number of the charm name | False |
| `units` | number | Number of units to deploy | False |

**Important:** The `config` map must include a `sensitive-props-key` config option pointing to a Juju secret ID. The secret must contain a field named `sensitive-props-key` with a value of at least 12 characters. Without this, the charm will remain in BlockedStatus.

Example:
```hcl
resource "juju_secret" "nifi_key" {
  model_uuid = var.model_uuid
  name       = "nifi-sensitive-key"
  value = {
    "sensitive-props-key" = "my-secure-key-12345"
  }
}

resource "juju_access_secret" "nifi_key" {
  model_uuid   = var.model_uuid
  secret_id    = juju_secret.nifi_key.secret_id
  applications = [module.nifi_k8s.application.name]
  depends_on   = [module.nifi_k8s]
}

module "nifi_k8s" {
  source     = "<path-to-this-directory>"
  model_uuid = var.model_uuid
  config = {
    "sensitive-props-key" = juju_secret.nifi_key.secret_id
  }
}
```

### Outputs
Upon applied, the module exports the following outputs:

| Name | Description |
| - | - |
| `application` | Deployed application object |
| `provides` | Map of `provides` endpoints |
| `requires` | Map of `requires` endpoints |

## Usage

This module is intended to be used as part of a higher-level module. When defining one, ensure that Terraform is aware of the `juju_model` dependency of the charm module. There are two options to do so:

### Define a `juju_model` resource
Define a `juju_model` resource and pass to the `model_uuid` input a reference to the `juju_model` resource's UUID. For example:

```hcl
resource "juju_model" "testing" {
  name = "nifi"
}

module "nifi-k8s" {
  source     = "<path-to-this-directory>"
  model_uuid = juju_model.testing.uuid
}
```

### Define a `data` source
Define a `data` source and pass to the `model_uuid` input a reference to the `data.juju_model` resource's UUID. This will enable Terraform to look for a `juju_model` resource with a UUID attribute equal to the one provided, and apply only if this is present. Otherwise, it will fail before applying anything.

```hcl
data "juju_model" "testing" {
  uuid = var.model_uuid
}

module "nifi-k8s" {
  source     = "<path-to-this-directory>"
  model_uuid = data.juju_model.testing.uuid
}
```
