# AWS deployment (Terraform)

Provisions the AWS side of Argus:

- **S3 (ingress)** — private, encrypted bucket whose object-created events trigger the
  triage Lambda. Nothing is ever written back into it.
- **S3 (evidence)** — decision records (`decisions/`), evidence bundles (`results/`),
  archived eval artefacts (`evidence/`) and the Lambda deployment package (`lambda/`).
  It carries **no** notification, which is what makes the Lambda trigger loop-free.
- **Amazon Bedrock** — model access for the `bedrock` reasoner backend
  (`bedrock:InvokeModel` / `InvokeModelWithResponseStream`, used by the Converse
  tool-use loop)
- **Lambda** — `src/argus/lambda_handler.py`, triggered by `s3:ObjectCreated:*` on the
  ingress bucket, writing its decision into the evidence bucket
- **ECR** — container registry with **immutable** tags and scan-on-push
- **ECS Fargate** — the FastAPI service (`/healthz`, `/api/triage`, …) with an
  **EFS** mount at `/data` for `ARGUS_OUT_DIR`
- **CloudWatch** — ECS and Lambda log groups, Container Insights, and the
  `Argus` metric namespace the AWS sink publishes into

## 0. Build the Lambda package (required before `terraform plan`)

`aws_s3_object.lambda_package` reads a zip built from `src/`, so the file must
exist before you plan. From the **repository root**:

```bash
make lambda-package        # -> infra/terraform/build/argus-lambda.zip
```

That target runs `infra/terraform/build-lambda.sh`, which zips the `argus` package
(handler `argus.lambda_handler:handler`) and excludes `__pycache__`. The zip is
tracked by Terraform via `source_code_hash`, so re-running the target after a code
change is enough to roll the handler forward.

## 1. Apply

```bash
cd infra/terraform
terraform init
terraform fmt
terraform plan \
  -var "vpc_id=vpc-xxxxxxxx" \
  -var 'subnet_ids=["subnet-aaaa","subnet-bbbb"]'
terraform apply \
  -var "vpc_id=vpc-xxxxxxxx" \
  -var 'subnet_ids=["subnet-aaaa","subnet-bbbb"]'
```

## 2. Build and push the container image

From the **repository root** (the `Dockerfile` path is relative to it):

```bash
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin "$(cd infra/terraform && terraform output -raw ecr_repository_url)"
docker build -t argus .                                   # from the repo root
docker tag argus:latest "$(cd infra/terraform && terraform output -raw ecr_repository_url):0.1.0"
docker push  "$(cd infra/terraform && terraform output -raw ecr_repository_url):0.1.0"
```

ECR tags are **immutable**, so a tag can be pushed once and never repointed. Bump
`image_tag` (`-var image_tag=0.1.1`) for the next release; the lifecycle rule keeps
the newest ten images and expires the rest.

## 3. Use the deployed service

```bash
cd infra/terraform
BUCKET=$(terraform output -raw ingress_bucket)

# Event-driven path: drop a clip in the ingress bucket and let the Lambda triage it
aws s3 cp clip.mp4 "s3://${BUCKET}/clip.mp4"

# The decision lands in the evidence bucket, not the ingress bucket
aws s3 cp "s3://$(terraform output -raw evidence_bucket)/decisions/decisions.jsonl" -

# Watch it work
aws logs tail "$(terraform output -raw lambda_log_group)" --follow
aws cloudwatch get-metric-statistics \
  --namespace-name "$(terraform output -raw cloudwatch_namespace)" \
  --metric-name decisions --start-time "$(date -u -d '10 min ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" --period 300
```

## Variables worth setting

| Variable | Default | Why you would change it |
| --- | --- | --- |
| `allowed_cidr` | `10.0.0.0/8` | The only CIDR allowed to reach the API port. Set it to your operator/VPN egress range or to the security-group CIDR of a fronting ALB. `0.0.0.0/0` is deliberately *not* the default. |
| `reasoner` | `heuristic` | `bedrock` enables the Bedrock tool-use loop (grants the model permissions; needs an inference profile or on-demand access in `us-east-1`). |
| `bedrock_model_id` | `anthropic.claude-3-5-sonnet-20240620-v1:0` | Model used by the `bedrock` reasoner. |
| `bedrock_inference_profiles` | `["*"]` | Narrow this to the exact inference-profile ARNs you intend to allow. |
| `enable_efs` | `true` | Durable `ARGUS_OUT_DIR`. Requires the task subnets to have NAT (or an EFS interface endpoint). Set `false` to use the ephemeral container filesystem; the S3 sink is then the durable copy. |
| `efs_subnet_ids` | `subnet_ids` | One mount target is created per subnet, so these must be in **distinct availability zones**. |
| `s3_trigger_prefix` | `""` (all objects) | Optional key prefix the *handler* honours inside the ingress bucket. The Terraform AWS provider cannot express a prefix filter on a bucket notification, so this is not enforced by S3. |
| `cw_namespace` | `Argus` | CloudWatch namespace for the metrics the AWS sink publishes. |

## Notes

- The service is **not** public: ingress is limited to `allowed_cidr`. Put an ALB or
  API Gateway in front for TLS and auth, then set `allowed_cidr` to that ALB's
  security group range.
- No secrets live in Terraform. Inject `ARGUS_LLM_*` or Bedrock credentials via SSM
  Parameter Store / Secrets Manager in the ECS task definition; the default
  `heuristic` reasoner needs no secrets at all, and `boto3` picks up the task role's
  credentials automatically.
- `var/` in the repo is gitignored, so the decision log the judges read is the copy
  in `docs/evidence/decisions.jsonl`; the deployed evidence bucket holds the live
  equivalent under `decisions/`.
