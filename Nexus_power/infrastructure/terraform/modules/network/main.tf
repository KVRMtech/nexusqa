# VPC + public/private subnets for the platform cluster.
#
# Design:
#   - Three AZs (or as many as `var.availability_zones` length).
#   - One private subnet per AZ for worker nodes.
#   - One public subnet per AZ for the NAT gateway + LBs.
#   - Single NAT GW in dev/staging; one NAT per AZ in production
#     (set `nat_gateway_per_az = true`).
#   - VPC flow logs to the supplied CloudWatch log group ARN; if
#     `flow_log_destination_arn = ""`, flow logs are skipped (dev only).
#
# Subnet tags follow the EKS convention:
#   kubernetes.io/cluster/<cluster_name> = shared
#   kubernetes.io/role/internal-elb = 1   (private subnets)
#   kubernetes.io/role/elb          = 1   (public subnets)
# so the AWS Load Balancer Controller auto-discovers them.

locals {
  azs            = var.availability_zones
  az_count       = length(local.azs)
  nat_count      = var.nat_gateway_per_az ? local.az_count : 1
  cluster_tag    = "kubernetes.io/cluster/${var.cluster_name}"
  common_tags    = merge(var.tags, {
    "nexus-platform/env" = var.env
  })
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, {
    Name              = "nexus-${var.env}"
    (local.cluster_tag) = "shared"
  })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, { Name = "nexus-${var.env}-igw" })
}

# ── Subnets ──────────────────────────────────────────────────────

resource "aws_subnet" "public" {
  count = local.az_count

  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.cidr_block, 4, count.index)

  map_public_ip_on_launch = false

  tags = merge(local.common_tags, {
    Name                              = "nexus-${var.env}-public-${local.azs[count.index]}"
    "kubernetes.io/role/elb"          = "1"
    (local.cluster_tag)               = "shared"
  })
}

resource "aws_subnet" "private" {
  count = local.az_count

  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.cidr_block, 4, count.index + 8)

  tags = merge(local.common_tags, {
    Name                                = "nexus-${var.env}-private-${local.azs[count.index]}"
    "kubernetes.io/role/internal-elb"   = "1"
    (local.cluster_tag)                 = "shared"
  })
}

# ── NAT ──────────────────────────────────────────────────────────

resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"

  tags = merge(local.common_tags, {
    Name = "nexus-${var.env}-nat-${count.index}"
  })

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  count = local.nat_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(local.common_tags, {
    Name = "nexus-${var.env}-nat-${count.index}"
  })

  depends_on = [aws_internet_gateway.this]
}

# ── Route tables ─────────────────────────────────────────────────

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(local.common_tags, { Name = "nexus-${var.env}-public-rt" })
}

resource "aws_route_table_association" "public" {
  count = local.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count = local.az_count

  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    # If single-NAT, every private subnet routes through nat[0].
    nat_gateway_id = aws_nat_gateway.this[var.nat_gateway_per_az ? count.index : 0].id
  }

  tags = merge(local.common_tags, {
    Name = "nexus-${var.env}-private-rt-${local.azs[count.index]}"
  })
}

resource "aws_route_table_association" "private" {
  count = local.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# ── Flow logs (optional but recommended) ─────────────────────────

resource "aws_flow_log" "this" {
  count = var.flow_log_destination_arn == "" ? 0 : 1

  vpc_id                   = aws_vpc.this.id
  log_destination          = var.flow_log_destination_arn
  log_destination_type     = "cloud-watch-logs"
  iam_role_arn             = var.flow_log_role_arn
  traffic_type             = "ALL"
  max_aggregation_interval = 60

  tags = local.common_tags
}
