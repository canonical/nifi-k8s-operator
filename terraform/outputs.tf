# CC008: charm modules must output the deployed application object.
output "application" {
  value = juju_application.nifi_k8s
}

output "requires" {
  value = {
    git                = "git-registry"
  }
}
