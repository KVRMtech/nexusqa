# EKS cluster + managed node groups + core add-ons + OIDC provider.
#
# Design:
#   - Private endpoint, public endpoint allowed from var.public_access_cidrs
#     (empty list = fully private, requires VPN access).
#   - Secrets envelope-encrypted with the caller-supplied KMS key (M0.7.d).
#   - Cluster log groups enabled for audit + authenticator + api so a
#     security incident has the data to investigate.
#   - OIDC provider is created here because every IRSA role depends on it.
#   - One "general" node group for CPU services + one optional "gpu" node
#     group with nvidia.com/gpu taints for ears/eyes/heart/brain.
#   - Add-ons: vpc-cni, kube-proxy, coredns, aws-ebs-csi-driver.
#     Pinned-version add-on management is delegated to gitops; we only
#     install the EKS-managed flavor that the cluster requires.

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

locals {
  partition      = data.aws_partition.current.partition
  account_id     = data.aws_caller_identity.current.account_id
  common_tags    = merge(var.tags, {
    "nexus-platform/env" = var.env
  })
}

# ── Cluster IAM role ─────────────────────────────────────────────

data "aws_iam_policy_document" "cluster_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "nexus-${var.env}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_trust.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "cluster_managed" {
  for_each = toset([
    "AmazonEKSClusterPolicy",
    "AmazonEKSVPCResourceController",
  ])
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.key}"
  role       = aws_iam_role.cluster.name
}

# ── Cluster ──────────────────────────────────────────────────────

resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = length(var.public_access_cidrs) > 0
    public_access_cidrs     = var.public_access_cidrs
    security_group_ids      = var.cluster_security_group_ids
  }

  encryption_config {
    provider {
      key_arn = var.secrets_kms_key_arn
    }
    resources = ["secrets"]
  }

  enabled_cluster_log_types = [
    "api",
    "audit",
    "authenticator",
    "controllerManager",
    "scheduler",
  ]

  # Make sure the IAM policy attachments are in place before the
  # cluster is created — order matters.
  depends_on = [aws_iam_role_policy_attachment.cluster_managed]

  tags = local.common_tags
}

# ── OIDC provider (IRSA) ─────────────────────────────────────────

data "tls_certificate" "cluster_oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "this" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = data.tls_certificate.cluster_oidc.certificates[*].sha1_fingerprint

  tags = local.common_tags
}

# ── Node IAM role (shared across node groups) ────────────────────

data "aws_iam_policy_document" "node_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "nexus-${var.env}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.node_trust.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "node_managed" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryReadOnly",
    "AmazonSSMManagedInstanceCore",
  ])
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.key}"
  role       = aws_iam_role.node.name
}

# ── Node groups ──────────────────────────────────────────────────

resource "aws_eks_node_group" "general" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "general"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids

  ami_type       = "AL2023_x86_64_STANDARD"
  capacity_type  = "ON_DEMAND"
  instance_types = var.general_instance_types
  disk_size      = 100

  scaling_config {
    desired_size = var.general_desired_size
    min_size     = var.general_min_size
    max_size     = var.general_max_size
  }

  update_config {
    max_unavailable_percentage = 25
  }

  labels = {
    "nexus-platform/role" = "general"
  }

  tags = local.common_tags

  depends_on = [aws_iam_role_policy_attachment.node_managed]

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}

resource "aws_eks_node_group" "gpu" {
  count = var.gpu_enabled ? 1 : 0

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "gpu"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids

  # GPU AMI with the nvidia drivers pre-baked.
  ami_type       = "AL2023_x86_64_NVIDIA"
  capacity_type  = "ON_DEMAND"
  instance_types = var.gpu_instance_types
  disk_size      = 200

  scaling_config {
    desired_size = var.gpu_desired_size
    min_size     = var.gpu_min_size
    max_size     = var.gpu_max_size
  }

  update_config {
    max_unavailable = 1
  }

  taint {
    key    = "nvidia.com/gpu"
    value  = "present"
    effect = "NO_SCHEDULE"
  }

  labels = {
    "nvidia.com/gpu.present" = "true"
    "nexus-platform/role"    = "gpu"
  }

  tags = local.common_tags

  depends_on = [aws_iam_role_policy_attachment.node_managed]

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}

# Optional GPU spot pool — engine pods opt in via the
# `nexus-platform/capacity=spot` toleration. The eviction notice handler
# in the worker SDK drains in-flight envelopes back to the queue before
# the spot instance is reclaimed, so a spot kill is just an orphan
# event that the orchestrator's sweeper recovers.
resource "aws_eks_node_group" "gpu_spot" {
  count = var.gpu_enabled && var.gpu_spot_enabled ? 1 : 0

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "gpu-spot"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids

  ami_type       = "AL2023_x86_64_NVIDIA"
  capacity_type  = "SPOT"
  instance_types = var.gpu_spot_instance_types
  disk_size      = 200

  scaling_config {
    desired_size = var.gpu_spot_min_size
    min_size     = var.gpu_spot_min_size
    max_size     = var.gpu_spot_max_size
  }

  update_config {
    max_unavailable = 1
  }

  # NoSchedule taints — pods need both tolerations to land here.
  taint {
    key    = "nvidia.com/gpu"
    value  = "present"
    effect = "NO_SCHEDULE"
  }
  taint {
    key    = "nexus-platform/capacity"
    value  = "spot"
    effect = "NO_SCHEDULE"
  }

  labels = {
    "nvidia.com/gpu.present"     = "true"
    "nexus-platform/role"        = "gpu"
    "nexus-platform/capacity"    = "spot"
  }

  tags = local.common_tags

  depends_on = [aws_iam_role_policy_attachment.node_managed]

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}

# ── Cluster add-ons ──────────────────────────────────────────────

resource "aws_eks_addon" "core" {
  for_each = toset(["vpc-cni", "kube-proxy", "coredns"])

  cluster_name = aws_eks_cluster.this.name
  addon_name   = each.key
  # EKS picks the version matching the cluster. We rely on EKS-managed
  # update behaviour rather than pinning here.
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = local.common_tags

  depends_on = [aws_eks_node_group.general]
}

# EBS CSI driver — needed by the model-cache PVC (the only retained PVC
# under the M0.4 stateless-engine design).
resource "aws_iam_role" "ebs_csi" {
  name = "nexus-${var.env}-ebs-csi"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.this.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${replace(aws_iam_openid_connect_provider.this.url, "https://", "")}:sub" =
            "system:serviceaccount:kube-system:ebs-csi-controller-sa"
          "${replace(aws_iam_openid_connect_provider.this.url, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
  role       = aws_iam_role.ebs_csi.name
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name             = aws_eks_cluster.this.name
  addon_name               = "aws-ebs-csi-driver"
  service_account_role_arn = aws_iam_role.ebs_csi.arn

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = local.common_tags

  depends_on = [aws_eks_node_group.general]
}
