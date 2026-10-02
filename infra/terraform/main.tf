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
}

# --- Evidence + clip storage -------------------------------------------------
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

resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    id     = "expire-evidence"
    status = "Enabled"
    filter {}
    expiration {
      days = var.evidence_retention_days
    }
  }
}

# --- Container registry ------------------------------------------------------
resource "aws_ecr_repository" "argus" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = local.tags
}

# --- Observability -----------------------------------------------------------
resource "aws_cloudwatch_log_group" "argus" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
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

resource "aws_iam_role_policy" "task" {
  name = "${local.name}-task"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.evidence.arn, "${aws_s3_bucket.evidence.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.argus.arn}:*"
      },
    ]
  })
}

resource "aws_security_group" "service" {
  name        = "${local.name}-svc"
  description = "Argus API service"
  vpc_id      = var.vpc_id
  ingress {
    from_port   = var.container_port
    to_port     = var.container_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
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
      { name = "ARGUS_REASONER", value = var.reasoner },
    ]
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
