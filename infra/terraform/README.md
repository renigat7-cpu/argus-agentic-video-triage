# AWS deployment (Terraform)

Provisions the AWS side of Argus:

- **S3** — clip ingress and decision/evidence bundles, private, auto-expiring
- **ECR** — container registry for the Argus image
- **ECS Fargate** — the FastAPI service (`/healthz`, `/api/triage`, …)
- **CloudWatch** — container logs and Container Insights metrics

## Apply

```bash
cd infra/terraform
terraform init
terraform apply \
  -var "vpc_id=vpc-xxxxxxxx" \
  -var 'subnet_ids=["subnet-aaaa","subnet-bbbb"]'
```

Build and push the image to the ECR repository printed by `terraform output`:

```bash
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin "$(terraform output -raw ecr_repository_url)"
docker build -t argus ../..   # from repo root: docker build -t argus .
docker tag argus:latest "$(terraform output -raw ecr_repository_url):0.1.0"
docker push "$(terraform output -raw ecr_repository_url):0.1.0"
```

Then point the service at the new tag with `-var image_tag=0.1.0`.

## Notes

- The service is public behind a security group; put an ALB or API Gateway in front
  for TLS and auth in production.
- Set `-var reasoner=llm` and inject `ARGUS_LLM_*` secrets via SSM/Secrets Manager
  to run the LLM reasoner; the default `heuristic` reasoner needs no secrets.
