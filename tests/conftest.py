"""Shared fixtures and fake AWS/Kubernetes clients for cleanup tests."""
from datetime import datetime, timezone

from ecr_cleanup.models import Repository

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REPOSITORY = Repository(
    registry_id="111111111111",
    name="orders-service",
    uri="111111111111.dkr.ecr.us-east-1.amazonaws.com/orders-service",
)


class FakePaginator:
    """A paginator that returns fixed pages and records parameters."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.pages)


class FakeEcrClient:
    """Small in-memory ECR client for discovery and deletion tests."""

    def __init__(self, repository_pages, image_pages, failures=None, image_lookup=None):
        self.repository_paginator = FakePaginator(repository_pages)
        self.image_paginator = FakePaginator(image_pages)
        self.delete_calls = []
        self.get_image_calls = []
        self.failures = failures or []
        self.image_lookup = image_lookup or {"images": []}

    def get_paginator(self, name):
        return {
            "describe_repositories": self.repository_paginator,
            "describe_images": self.image_paginator,
        }[name]

    def batch_delete_image(self, **kwargs):
        self.delete_calls.append(kwargs)
        return {"failures": self.failures}

    def batch_get_image(self, **kwargs):
        """Return the configured tag-resolution response."""
        self.get_image_calls.append(kwargs)
        if isinstance(self.image_lookup, Exception):
            raise self.image_lookup
        return self.image_lookup


def image(digest, tags=None, pushed_at=None):
    """Return an ECR image detail dictionary."""
    value = {
        "imageDigest": digest,
        "imagePushedAt": pushed_at or datetime(2025, 1, 1, tzinfo=timezone.utc),
    }
    if tags is not None:
        value["imageTags"] = tags
    return value


def repository(name="orders-service", uri=None):
    """Return an ECR repository result dictionary."""
    return {
        "registryId": "111111111111",
        "repositoryName": name,
        "repositoryUri": uri or "111111111111.dkr.ecr.us-east-1.amazonaws.com/{}".format(name),
    }


def pod(name, phase="Running", containers=None, statuses=None, init=None, init_statuses=None, ephemeral=None, ephemeral_statuses=None):
    """Return a Kubernetes Pod dictionary with configurable container groups."""
    return {
        "metadata": {"name": name, "namespace": "default"},
        "spec": {
            "containers": containers or [],
            "init_containers": init or [],
            "ephemeral_containers": ephemeral or [],
        },
        "status": {
            "phase": phase,
            "container_statuses": statuses or [],
            "init_container_statuses": init_statuses or [],
            "ephemeral_container_statuses": ephemeral_statuses or [],
        },
    }
