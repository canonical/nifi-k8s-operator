variable "app_name" {
  type        = string
  description = "Name of the deployed application"
  default     = "nifi-k8s"
}

variable "channel" {
  type        = string
  description = "Charmhub channel to deploy the charm from"
  default     = "2.10/edge"
}

variable "config" {
  type        = map(string)
  description = "Configuration to deploy this application with"
  default     = {}
}

# CC008: default constraints should be null for charm modules.
variable "constraints" {
  type        = string
  description = "Constraints to be used when deploying this application"
  default     = null
}

variable "model_uuid" {
  type        = string
  description = "UUID of Juju model where the application is to be deployed"
}

variable "revision" {
  type        = number
  description = "Revision of the charm to deploy"
  default     = null
}

variable "units" {
  type        = number
  description = "Number of units to deploy with this name and configuration"
  default     = 1
}
