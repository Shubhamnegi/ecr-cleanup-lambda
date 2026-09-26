# Automated Image Cleanup for Amazon ECR
This repository contains a Python script for cleaning up stale images in
[Amazon ECR](https://aws.amazon.com/ecr). It can be run from the command line
or a Jenkins job, with a dry-run mode, regional selection, and configurable
retention count. A legacy SAM/Lambda template also remains in the repository.

> **Safety note:** non-dry-run cleanup requires Kubernetes active-image
> protection and a target configuration file. The Jenkins/Kubernetes active-
> image protection design, cluster setup, multi-cluster configuration, and
> test/coverage gate are documented in
> [Kubernetes active-image protection guide](KUBERNETES_ACTIVE_IMAGE_PROTECTION.md).
> The deployable RBAC manifest is
> [`kubernetes/ecr-cleanup-reader.yaml`](kubernetes/ecr-cleanup-reader.yaml),
> and [`k8s-targets.example.json`](k8s-targets.example.json) is a token-free
> target configuration example. The manifest uses the existing `default`
> namespace. An optional, long-lived token Secret template is available at
> [`kubernetes/ecr-cleanup-reader-token.example.yaml`](kubernetes/ecr-cleanup-reader-token.example.yaml);
> use it only with a credential rotation process.

## Authenticate with AWS
[Configuring the AWS Command Line Interface.](http://docs.aws.amazon.com/cli/latest/userguide/cli-chap-getting-started.html)

## Jenkins/Python setup

Use a Python 3 virtual environment and install the runtime and test
dependencies:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest
```

The legacy SAM/Lambda template is retained for reference; the supported
operational workflow is the Jenkins command-line job.

## Running cleanup

Start with a dry run. Only repositories containing `-service` are eligible:

```bash
./venv/bin/python main.py -dryrun true -imagestokeep 5 -region YOUR_ECR_REGION
```

For real deletion, bind a masked ServiceAccount token in Jenkins, configure
every relevant Kubernetes cluster in a target JSON file, and enable active
image protection:

```bash
./venv/bin/python main.py \
  -dryrun false \
  -imagestokeep 5 \
  -region YOUR_ECR_REGION \
  --k8s-targets-file k8s-targets.json \
  --protect-active-pod-images
```

The command exits without deletion if Kubernetes inventory, token validation,
or TLS verification fails.
