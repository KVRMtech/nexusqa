variable "env" {
  description = "Environment name (dev | staging | production)."
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name (used in subnet tags for ELB discovery)."
  type        = string
}

variable "cidr_block" {
  description = "VPC CIDR. /16 is the default; subnets are carved as /20."
  type        = string
  default     = "10.40.0.0/16"
}

variable "availability_zones" {
  description = "List of AZs to use. Length defines subnet count."
  type        = list(string)
}

variable "nat_gateway_per_az" {
  description = "True in production for HA. False in dev/staging for cost."
  type        = bool
  default     = false
}

variable "flow_log_destination_arn" {
  description = "CloudWatch Log Group ARN for VPC flow logs. Empty disables logging."
  type        = string
  default     = ""
}

variable "flow_log_role_arn" {
  description = "IAM role ARN that allows VPC to write flow logs to the destination."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Common tags applied to every resource."
  type        = map(string)
  default     = {}
}
