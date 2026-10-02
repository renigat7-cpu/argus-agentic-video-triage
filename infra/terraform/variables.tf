variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "argus"
}

variable "image_tag" {
  description = "Container image tag to deploy"
  type        = string
  default     = "0.1.0"
}

variable "container_port" {
  description = "Port the API listens on"
  type        = number
  default     = 8080
}

variable "task_cpu" {
  description = "Fargate task CPU units"
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "Fargate task memory in MiB"
  type        = number
  default     = 2048
}

variable "desired_count" {
  description = "Number of API tasks"
  type        = number
  default     = 1
}

variable "reasoner" {
  description = "Reasoner backend: heuristic or llm"
  type        = string
  default     = "heuristic"
}

variable "vpc_id" {
  description = "VPC to deploy the service into"
  type        = string
}

variable "subnet_ids" {
  description = "Subnets for the Fargate service"
  type        = list(string)
}

variable "evidence_retention_days" {
  description = "Days to keep evidence objects in S3"
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "CloudWatch log retention"
  type        = number
  default     = 30
}
