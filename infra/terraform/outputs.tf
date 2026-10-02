output "evidence_bucket" {
  description = "S3 bucket holding clips and evidence bundles"
  value       = aws_s3_bucket.evidence.bucket
}

output "ecr_repository_url" {
  description = "ECR repository for the Argus image"
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
  description = "CloudWatch log group"
  value       = aws_cloudwatch_log_group.argus.name
}
