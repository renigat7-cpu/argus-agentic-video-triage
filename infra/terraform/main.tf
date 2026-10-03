terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name = var.project_name
  tags = {
    Project   = var.project_name
    Component = "argus-video-triage"
    ManagedBy = "terraform"
  }

  # Key prefixes inside the S3 bucket. The ECS task writes decisions + results
  # (AWS sink); the Lambda is triggered only by `s3_trigger_prefix` (ingress).
  decisions_prefix = "decisions/"
  results_prefix   = "results/"
  evidence_prefix  = "evidence/"

  # EFS access point identity — matches uid 10001 (useradd argus) in Dockerfile.
  argus_uid = 10001
  argus_gid = 10001

  # Fall back to the service subnets when no dedicated EFS subnets are given.
  efs_subnet_ids = length(var.efs_subnet_ids) > 0 ? var.efs_subnet_ids : var.subnet_ids

  container_volumes = var.enable_efs ? [
    {
      name = "data"
      efsVolumeConfiguration = {
        fileSystemId      = aws_efs_file_system.argus.id
        transitEncryption = "ENABLED"
        rootDirectory = {
          path          = "/"
          accessPointId = aws_efs_access_point.data.id
        }
      }
    }
  ] : []

  container_mount_points = var.enable_efs ? [
    {
      sourceVolume    = "data"
      destinationPath = "/data"
      readOnly        = false
    }
  ] : []
}

# --- Storage -----------------------------------------------------------------
# Two buckets, and the split is what makes the Lambda trigger safe.
#
# ingress/ (separate bucket, notification here, nothing is ever written back):
#   clips dropped by a camera/uploader -> one ObjectCreated event each
#
# evidence/ (no notification at all):
#   decisions/  — decision records written by the ECS task and the Lambda
#   results/    — evidence bundles (frames, tool traces)
#   evidence/   — archived eval/live-demo evidence
#   lambda/     — the Lambda deployment package (never expires)
#
# Because the Lambda writes only into the evidence bucket, an unfiltered
# `s3:ObjectCreated:*` rule on the ingress bucket cannot re-trigger itself.
resource "aws_s3_bucket" "ingress" {
  bucket_prefix = "${local.name}-ingress-"
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "ingress" {
  bucket                  = aws_s3_bucket.ingress.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ingress" {
  bucket = aws_s3_bucket.ingress.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "ingress" {
  bucket = aws_s3_bucket.ingress.id
  rule {
    id     = "expire-ingress"
    status = "Enabled"
    filter {}
    expiration {
      days = var.evidence_retention_days
    }
  }
}

resource "aws_s3_bucket" "evidence" {
  bucket_prefix = "${local.name}-evidence-"
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket                  = aws_s3_bucket.evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "expire-decisions"
    status = "Enabled"
    filter {
      prefix = local.decisions_prefix
    }
    expiration {
      days = var.evidence_retention_days
    }
  }

  rule {
    id     = "expire-results"
    status = "Enabled"
    filter {
      prefix = local.results_prefix
    }
    expiration {
      days = var.evidence_retention_days
    }
  }

  rule {
    id     = "expire-evidence"
    status = "Enabled"
    filter {
      prefix = local.evidence_prefix
    }
    expiration {
      days = var.evidence_retention_days
    }
  }

  # The deployment package is state for `aws_s3_object.lambda_package`, so it is
  # deliberately excluded from every expiration rule above.
  rule {
    id     = "abort-stale-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# --- Container registry ------------------------------------------------------
# IMMUTABLE: a published tag is a permanent, addressable statement about exactly
# which bytes shipped. Combined with the digest-pinned Dockerfile base, an image
# tag cannot be silently repointed at different content.
resource "aws_ecr_repository" "argus" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = local.tags
}

# Mutability is one-way, so prune old tags instead of letting them accumulate.
resource "aws_ecr_lifecycle_policy" "argus" {
  repository = aws_ecr_repository.argus.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the newest 10 immutable images"
      selection = {
        tagStatus   = "tagged"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# --- Observability -----------------------------------------------------------
resource "aws_cloudwatch_log_group" "argus" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}-triage"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

# --- IAM roles ---------------------------------------------------------------
resource "aws_iam_role" "execution" {
  name = "${local.name}-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_efs" {
  name = "${local.name}-execution-efs"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "elasticfilesystem:ClientMount",
        "elasticfilesystem:ClientWrite",
        "elasticfilesystem:DescribeMountTargets",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_role" "task" {
  name = "${local.name}-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

# What the API container is actually allowed to do, and nothing more:
#   * read/write only the key prefixes Argus uses in its own bucket
#   * invoke the configured Bedrock models (Converse tool-use loop)
#   * publish its own CloudWatch metrics
resource "aws_iam_role_policy" "task" {
  name = "${local.name}-task"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListOwnBuckets"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = [aws_s3_bucket.evidence.arn, aws_s3_bucket.ingress.arn]
      },
      {
        Sid    = "ReadWriteDecisionPrefixes"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = [
          "${aws_s3_bucket.evidence.arn}/${local.decisions_prefix}*",
          "${aws_s3_bucket.evidence.arn}/${local.results_prefix}*",
          "${aws_s3_bucket.evidence.arn}/${local.evidence_prefix}*",
          "${aws_s3_bucket.ingress.arn}/*",
        ]
      },
      {
        Sid    = "InvokeBedrockConverse"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = var.bedrock_inference_profiles
      },
      {
        Sid      = "PublishMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
      },
      {
        Sid      = "WriteServiceLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.argus.arn}:*"
      },
    ]
  })
}

# --- Networking --------------------------------------------------------------
# Not world-reachable: ingress is limited to var.allowed_cidr (default the
# private 10.0.0.0/8 range). Widen it to the operator/ALB CIDR via -var.
resource "aws_security_group" "service" {
  name        = "${local.name}-svc"
  description = "Argus API service"
  vpc_id      = var.vpc_id
  ingress {
    from_port   = var.container_port
    to_port     = var.container_port
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

# --- Durable storage for ARGUS_OUT_DIR --------------------------------------
# ARGUS_OUT_DIR=/data is where decisions.jsonl and evidence bundles land. Without
# a volume that data dies with the container, so it is mounted on EFS. Set
# -var enable_efs=false to fall back to the ephemeral container filesystem (the
# S3 sink remains the durable copy in that case).
resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "EFS access from the Argus task only"
  vpc_id      = var.vpc_id
  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.service.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

resource "aws_efs_file_system" "argus" {
  creation_token = "${local.name}-data"
  encrypted      = true
  tags           = local.tags
}

# One mount target per availability zone — hence one per subnet below.
resource "aws_efs_mount_target" "argus" {
  count           = var.enable_efs ? length(local.efs_subnet_ids) : 0
  file_system_id  = aws_efs_file_system.argus.id
  subnet_id       = local.efs_subnet_ids[count.index]
  security_groups = length(var.efs_security_group_ids) > 0 ? var.efs_security_group_ids : [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.argus.id
  posix_user {
    uid = local.argus_uid
    gid = local.argus_gid
  }
  root_directory {
    path = "/data"
    creation_info {
      owner_uid   = local.argus_uid
      owner_gid   = local.argus_gid
      permissions = "0755"
    }
  }
  tags = local.tags
}

# --- ECS Fargate API ---------------------------------------------------------
resource "aws_ecs_cluster" "argus" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = local.tags
}

resource "aws_ecs_task_definition" "argus" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "argus"
    image     = "${aws_ecr_repository.argus.repository_url}:${var.image_tag}"
    essential = true
    portMappings = [{
      containerPort = var.container_port
      protocol      = "tcp"
    }]
    environment = [
      { name = "ARGUS_OUT_DIR", value = "/data" },
      # The root filesystem is read-only, so uploads must land on the EFS volume
      # too; otherwise the service cannot even stage an upload.
      { name = "ARGUS_UPLOAD_DIR", value = "/data/uploads" },
      { name = "ARGUS_REASONER", value = var.reasoner },
      { name = "ARGUS_AWS_SINK", value = "s3" },
      { name = "ARGUS_AWS_REGION", value = var.aws_region },
      { name = "ARGUS_S3_BUCKET", value = aws_s3_bucket.evidence.id },
      { name = "ARGUS_S3_PREFIX", value = local.decisions_prefix },
      { name = "ARGUS_CW_NAMESPACE", value = var.cw_namespace },
      { name = "ARGUS_BEDROCK_MODEL_ID", value = var.bedrock_model_id },
    ]
    volumes                = local.container_volumes
    mountPoints            = local.container_mount_points
    readonlyRootFilesystem = true
    user                   = tostring(local.argus_uid)
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.argus.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "argus"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:${var.container_port}/healthz')\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 15
    }
  }])
  tags = local.tags
}

resource "aws_ecs_service" "argus" {
  name            = local.name
  cluster         = aws_ecs_cluster.argus.id
  task_definition = aws_ecs_task_definition.argus.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true
  }
  tags = local.tags
}

# --- Event-driven triage (Lambda) -------------------------------------------
# A clip landing in the ingress bucket is triaged without an API call.
# BUILD STEP: the zip below is built from src/argus/lambda_handler.py and must
# exist before `terraform plan` — see infra/terraform/README.md and
# `make lambda-package` from the repo root.
resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.name}-lambda"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadTriggeredClip"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.ingress.arn}/*",
          "${aws_s3_bucket.evidence.arn}/${local.decisions_prefix}*",
        ]
      },
      {
        Sid      = "ListBuckets"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = [aws_s3_bucket.ingress.arn, aws_s3_bucket.evidence.arn]
      },
      {
        Sid    = "WriteDecisions"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.evidence.arn}/${local.decisions_prefix}*",
          "${aws_s3_bucket.evidence.arn}/${local.results_prefix}*",
        ]
      },
      {
        Sid    = "InvokeBedrockConverse"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = var.bedrock_inference_profiles
      },
      {
        Sid      = "PublishMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
      },
      {
        Sid      = "WriteLambdaLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
    ]
  })
}

resource "aws_s3_object" "lambda_package" {
  bucket       = aws_s3_bucket.evidence.id
  key          = "lambda/${local.name}.zip"
  source       = var.lambda_package_path
  etag         = filemd5(var.lambda_package_path)
  source_hash  = filebase64sha256(var.lambda_package_path)
  content_type = "application/zip"
  tags         = local.tags
}

resource "aws_lambda_function" "triage" {
  function_name = "${local.name}-triage"
  description   = "Argus agentic triage, triggered by S3 object-created events"
  role          = aws_iam_role.lambda.arn
  handler       = var.lambda_handler_module
  runtime       = var.lambda_runtime
  timeout       = var.lambda_timeout_s
  memory_size   = var.lambda_memory_mb

  filename         = var.lambda_package_path
  source_code_hash = filebase64sha256(var.lambda_package_path)
  s3_bucket        = aws_s3_bucket.evidence.id
  s3_key           = aws_s3_object.lambda_package.key

  environment {
    variables = {
      ARGUS_OUT_DIR              = "/tmp/argus"
      ARGUS_REASONER             = var.reasoner
      ARGUS_AWS_SINK             = "s3"
      ARGUS_AWS_REGION           = var.aws_region
      ARGUS_S3_BUCKET            = aws_s3_bucket.evidence.id
      ARGUS_S3_PREFIX            = local.decisions_prefix
      ARGUS_CW_NAMESPACE         = var.cw_namespace
      ARGUS_BEDROCK_MODEL_ID     = var.bedrock_model_id
      BEDROCK_INFERENCE_PROFILES = jsonencode(var.bedrock_inference_profiles)
    }
  }

  tags = local.tags
}

resource "aws_lambda_permission" "s3_invoke" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.triage.function_name
  principal     = "s3.amazonaws.com"
  # Scoped to the one ingress bucket, so no other bucket can invoke the handler.
  source_arn = aws_s3_bucket.ingress.arn
}

# Attached to the *ingress* bucket only. The Lambda writes its results into the
# evidence bucket, which has no notification, so an unfiltered
# s3:ObjectCreated:* rule here cannot re-trigger the handler.
resource "aws_s3_bucket_notification" "triage" {
  bucket = aws_s3_bucket.ingress.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.triage.arn
    events              = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_lambda_permission.s3_invoke]
}
