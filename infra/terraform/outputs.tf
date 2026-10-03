output "evidence_bucket" {
  description = "S3 bucket holding decision records, evidence bundles and the Lambda package"
  value       = aws_s3_bucket.evidence.bucket
}

output "ingress_bucket" {
  description = "S3 bucket whose object-created events trigger the triage Lambda"
  value       = aws_s3_bucket.ingress.bucket
}

output "decisions_prefix" {
  description = "S3 key prefix the API/Lambda write decision records to"
  value       = "${aws_s3_bucket.evidence.id}/${local.decisions_prefix}"
}

output "results_prefix" {
  description = "S3 key prefix for evidence bundles (frames, tool traces)"
  value       = "${aws_s3_bucket.evidence.id}/${local.results_prefix}"
}

output "lambda_trigger_bucket" {
  description = "Bucket to copy a clip into to trigger event-driven triage"
  value       = aws_s3_bucket.ingress.bucket
}

output "ecr_repository_url" {
  description = "ECR repository for the Argus image (immutable tags)"
  value       = aws_ecr_repository.argus.repository_url
}

output "ecs_cluster" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.argus.name
}

output "ecs_service" {
  description = "ECS service name"
  value       = aws_ecs_service.argus.name
}

output "log_group" {
  description = "CloudWatch log group for the ECS service"
  value       = aws_cloudwatch_log_group.argus.name
}

output "lambda_log_group" {
  description = "CloudWatch log group for the triage Lambda"
  value       = aws_cloudwatch_log_group.lambda.name
}

output "lambda_function_name" {
  description = "Name of the S3-triggered triage Lambda"
  value       = aws_lambda_function.triage.function_name
}

output "lambda_function_arn" {
  description = "ARN of the S3-triggered triage Lambda"
  value       = aws_lambda_function.triage.arn
}

output "cloudwatch_namespace" {
  description = "CloudWatch namespace Argus metrics are published to"
  value       = var.cw_namespace
}

output "efs_file_system_id" {
  description = "EFS file system backing ARGUS_OUT_DIR (null when enable_efs = false)"
  value       = var.enable_efs ? aws_efs_file_system.argus.id : null
}

output "service_security_group_id" {
  description = "Security group whose ingress is limited to var.allowed_cidr"
  value       = aws_security_group.service.id
}
