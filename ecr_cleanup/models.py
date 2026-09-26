"""Immutable value objects used by cleanup orchestration."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class KubernetesTarget:
    """A Kubernetes cluster from which active image references are read.

    Args:
        cluster_name: EKS cluster name.
        region: AWS region containing the EKS cluster.
        token_env: Environment variable containing the ServiceAccount token.
        aws_profile: Optional boto3 profile name for this target.
    """

    cluster_name: str
    region: str
    token_env: str
    aws_profile: Optional[str] = None


@dataclass(frozen=True)
class Repository:
    """The ECR repository metadata needed for discovery and deletion."""

    registry_id: str
    name: str
    uri: str


@dataclass(frozen=True)
class ImageCandidate:
    """An ECR image digest eligible for retention-policy evaluation."""

    repository: Repository
    digest: str


@dataclass(frozen=True)
class ProtectedImage:
    """An ECR digest referenced by an active Pod.

    Args:
        repository_uri: Normalized ECR repository URI without tag or digest.
        digest: Immutable image digest.
        cluster_name: Cluster that reported the image.
        namespace: Pod namespace.
        pod_name: Pod name.
    """

    repository_uri: str
    digest: str
    cluster_name: str
    namespace: str
    pod_name: str


@dataclass(frozen=True)
class DeletionReport:
    """Summary of one repository cleanup calculation."""

    repository: Repository
    candidates: int
    protected: int
    deletable: int
    deleted: int


@dataclass(frozen=True)
class TaggedImage:
    """ECR image detail values used by the existing branch-retention policy."""

    digest: str
    tags: tuple[str, ...]
    pushed_at: datetime
