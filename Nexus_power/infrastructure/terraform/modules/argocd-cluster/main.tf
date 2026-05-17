# Renders an Argo CD cluster registration Secret as a local file and as
# an output. The file is meant to be committed to a private
# operations repo OR (preferred) applied imperatively by the operator
# during bootstrap with `kubectl apply -f` from the Terraform output.
#
# Why not apply directly here?
#   - The destination cluster is Argo CD's hub cluster (not THIS
#     cluster), which Terraform doesn't have a provider for in this
#     module.
#   - Bootstrapping order matters: this Secret needs to exist before
#     the ApplicationSets generate Applications for the new cluster.
#
# The Secret carries the labels the M0.5 ApplicationSet logic depends
# on: `env`, `cloud`, plus the standard secret-type cluster marker.

locals {
  secret = {
    apiVersion = "v1"
    kind       = "Secret"
    metadata = {
      name      = "cluster-${var.cluster_short_name}"
      namespace = "argocd"
      labels = {
        "argocd.argoproj.io/secret-type" = "cluster"
        "env"                            = var.env
        "cloud"                          = "aws"
        "region"                         = var.region
      }
    }
    type = "Opaque"
    stringData = {
      name   = var.cluster_short_name
      server = var.cluster_endpoint
      config = jsonencode({
        # AWS EKS clusters use exec auth via aws-iam-authenticator or
        # `aws eks get-token`. Argo CD's exec config invokes the AWS CLI
        # inside the argocd-server pod (which has an IRSA role that
        # is allowed eks:DescribeCluster on this cluster).
        awsAuthConfig = {
          clusterName = var.cluster_name
          roleARN     = var.argocd_role_arn
        }
        tlsClientConfig = {
          insecure = false
          caData   = var.cluster_ca_data
        }
      })
    }
  }
}

resource "local_file" "cluster_secret" {
  filename        = "${path.module}/generated/cluster-${var.cluster_short_name}.yaml"
  content         = yamlencode(local.secret)
  file_permission = "0600"
}
