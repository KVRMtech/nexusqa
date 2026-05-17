variable "env" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "kubernetes_version" {
  type    = string
  default = "1.30"
}

variable "subnet_ids" {
  description = "Private subnet IDs for nodes (and ENIs for control-plane)."
  type        = list(string)
}

variable "cluster_security_group_ids" {
  description = "Additional security groups for the control plane ENIs."
  type        = list(string)
  default     = []
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to reach the public Kubernetes API endpoint. Empty list = endpoint fully private (kubectl requires VPN)."
  type        = list(string)
  default     = []
}

variable "secrets_kms_key_arn" {
  description = "KMS key ARN for EKS secret envelope encryption."
  type        = string
}

variable "general_instance_types" {
  type    = list(string)
  default = ["m6i.large", "m6i.xlarge"]
}

variable "general_desired_size" {
  type    = number
  default = 3
}

variable "general_min_size" {
  type    = number
  default = 3
}

variable "general_max_size" {
  type    = number
  default = 12
}

variable "gpu_enabled" {
  type    = bool
  default = true
}

variable "gpu_instance_types" {
  type    = list(string)
  default = ["g5.xlarge"]
}

variable "gpu_desired_size" {
  type    = number
  default = 1
}

variable "gpu_min_size" {
  type    = number
  default = 1
}

variable "gpu_max_size" {
  type    = number
  default = 4
}

variable "gpu_spot_enabled" {
  description = "Provision an additional GPU node group on Spot capacity for canonical-workflow GPU lanes. Engine pods opt in via spot toleration."
  type        = bool
  default     = false
}

variable "gpu_spot_instance_types" {
  description = "Spot instance choices — broaden the list to maximise availability."
  type        = list(string)
  default     = ["g5.xlarge", "g5.2xlarge", "g4dn.xlarge", "g4dn.2xlarge"]
}

variable "gpu_spot_min_size" {
  type    = number
  default = 0
}

variable "gpu_spot_max_size" {
  type    = number
  default = 8
}

variable "tags" {
  type    = map(string)
  default = {}
}
