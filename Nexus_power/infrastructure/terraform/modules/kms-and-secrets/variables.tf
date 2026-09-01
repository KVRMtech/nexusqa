variable "env" {
  type = string
}

variable "secret_keys" {
  description = <<EOT
The full list of secret keys the chart's ExternalSecret references.
Every name becomes a Secrets Manager entry at
  /nexus-platform/<env>/<key>
prefixed under the ClusterSecretStore.

Keep this list in sync with templates/external-secret.yaml in the chart.
EOT
  type = list(string)
  default = [
    "jwt-secret",
    "postgres-password",
    "redis-password",
    "neo4j-password",
    "minio-access-key",
    "minio-secret-key",
    "grafana-password",
    "shield-encryption-key",
    "ears-hf-token",
    "heart-tier1-api-key",
    "heart-tier2-api-key",
    "brain-tier1-api-key",
    "brain-tier2-api-key",
    "s3-access-key",
    "s3-secret-key",
    "argo-rollouts-slack-token",
    "argo-rollouts-pagerduty-token",
  ]
}

variable "tags" {
  type    = map(string)
  default = {}
}
