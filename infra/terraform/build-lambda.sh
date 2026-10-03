#!/usr/bin/env bash
# Build the Argus triage Lambda deployment package.
#
# Produces <repo>/infra/terraform/build/argus-lambda.zip containing the `argus`
# package (so the handler resolves as argus.lambda_handler:handler). This zip is
# what Terraform uploads (aws_s3_object.lambda_package -> aws_lambda_function),
# so it MUST be built before `terraform plan` in infra/terraform.
#
# Dependencies are not vendored into the zip: the Lambda-provided
# boto3/botocore cover the AWS calls, and opencv-contrib-python / numpy must be
# supplied by the runtime or a layer pinned to the same versions as
# requirements.txt (opencv-contrib-python==5.0.0.93, numpy==2.5.1). Keep the
# runtime and requirements.txt in sync when bumping either.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="${repo_root}/infra/terraform/build"
out="${build_dir}/argus-lambda.zip"

mkdir -p "${build_dir}"
rm -f "${out}"

if [[ ! -f "${repo_root}/src/argus/lambda_handler.py" ]]; then
  echo "error: src/argus/lambda_handler.py not found; nothing to package" >&2
  exit 1
fi

# -r recurse, -x exclude caches and tests, -X strip extended attributes.
(cd "${repo_root}/src" && zip -qr9X "${out}" argus \
  -x '*/__pycache__/*' '*.py[co]' '*/.pytest_cache/*' '*.mp4')

echo "wrote ${out} ($(du -h "${out}" | cut -f1))"
unzip -l "${out}" | head -n 20
