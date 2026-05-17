output "eso_role_arn" {
  value = aws_iam_role.eso.arn
}

output "engine_role_arn" {
  value = aws_iam_role.engine.arn
}

output "rollouts_role_arn" {
  value = aws_iam_role.rollouts.arn
}
