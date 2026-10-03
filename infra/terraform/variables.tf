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
  description = "Reasoner backend: heuristic (default, no AWS calls) or bedrock (Converse tool use)"
  type        = string
  default     = "heuristic"
}

variable "allowed_cidr" {
  description = <<-EOT
    CIDR allowed to reach the API container port. Defaults to the RFC1918 10.0.0.0/8
    private range: the service is NOT world-reachable. Set this to the operator /
    corporate egress CIDR (or the ALB security-group CIDR) that should call the API,
    e.g. -var 'allowed_cidr=203.0.113.0/24'. Do not set 0.0.0.0/0 unless a fronting
    ALB plus TLS and auth is already terminating traffic in front of this service.
  EOT
  type        = string
  default     = "10.0.0.0/8"

  validation {
    condition     = can(cidrhost(var.allowed_cidr, 0))
    error_message = "allowed_cidr must be a valid CIDR block, e.g. 10.0.0.0/8."
  }
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

variable "bedrock_model_id" {
  description = "Bedrock model id the ECS task invokes via the Converse API (empty = reasoner stays heuristic)"
  type        = string
  default     = "anthropic.claude-3-5-sonnet-20240620-v1:0"
}

variable "bedrock_inference_profiles" {
  description = <<-EOT
    Bedrock inference-profile ARNs (or `*`) the task role may invoke via
    bedrock:InvokeModel / bedrock:InvokeModelWithResponseStream (the Converse API).
    `*` matches every model in the account, which is the most permissive option.
  EOT
  type        = list(string)
  default     = ["*"]
}

variable "lambda_handler_module" {
  description = "Handler inside the deployment package, as module:function"
  type        = string
  default     = "argus.lambda_handler:handler"
}

variable "lambda_runtime" {
  description = "Lambda Python runtime"
  type        = string
  default     = "python3.12"
}

variable "lambda_timeout_s" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 60
}

variable "lambda_memory_mb" {
  description = "Lambda memory in MiB"
  type        = number
  default     = 2048
}

variable "lambda_package_path" {
  description = <<-EOT
    Deployment zip for the triage handler, built from src/argus/lambda_handler.py.
    It must exist before `terraform plan`. Build it from the repo root with:

      make lambda-package    # (or: infra/terraform/build-lambda.sh)

    See infra/terraform/README.md for the full build step.
  EOT
  type        = string
  default     = "build/argus-lambda.zip"
}

variable "s3_trigger_prefix" {
  description = <<-EOT
    Optional key prefix the triage Lambda restricts itself to inside the ingress
    bucket. The Terraform AWS provider cannot express a prefix filter on an
    `aws_s3_bucket_notification`, so this is enforced by the handler, not by S3 —
    and the notification is attached to the *ingress* bucket only, while all
    results are written to the separate evidence bucket. That separation is what
    makes an unfiltered s3:ObjectCreated:* rule safe: the Lambda can never
    re-trigger itself.
  EOT
  type        = string
  default     = ""
}

variable "cw_namespace" {
  description = "CloudWatch namespace for Argus-published triage metrics (PutMetricData)"
  type        = string
  default     = "Argus"
}

variable "enable_efs" {
  description = <<-EOT
    Mount an EFS access point at /data so ARGUS_OUT_DIR is durable across task
    replacements. The Fargate service must run in at least two private subnets
    with NAT (or an interface endpoint for EFS) for this to work. Set to false to
    run with the ephemeral container filesystem instead, in which case the
    decision log only reaches S3 via ARGUS_AWS_SINK.
  EOT
  type        = bool
  default     = true
}

variable "efs_subnet_ids" {
  description = "Subnets that create the EFS mount targets (defaults to subnet_ids)"
  type        = list(string)
  default     = []
}

variable "efs_security_group_ids" {
  description = "Security groups allowed to mount EFS (defaults to the service security group)"
  type        = list(string)
  default     = []
}
