"""Fail-closed orchestration for ECR cleanup and Pod image protection."""
from collections.abc import Callable, Iterable
from typing import Any

from ecr_cleanup.ecr import delete_candidates, discover_candidates, iter_eligible_repositories
from ecr_cleanup.errors import ConfigurationError
from ecr_cleanup.kubernetes import KubernetesInventoryCollector
from ecr_cleanup.models import DeletionReport, ImageCandidate, KubernetesTarget, ProtectedImage

Logger = Callable[[str], None]


class CleanupService:
    """Coordinates ECR retention cleanup with Kubernetes active-image safety.

    Args:
        ecr_client: Boto3-compatible ECR client for one ECR region.
        inventory_collector: Kubernetes active-image collector.
        logger: Output callback, injectable for tests.
    """

    def __init__(
        self,
        ecr_client: Any,
        inventory_collector: KubernetesInventoryCollector,
        logger: Logger = print,
    ) -> None:
        self._ecr_client = ecr_client
        self._inventory_collector = inventory_collector
        self._logger = logger

    def run(
        self,
        *,
        repository_name_contains: str,
        images_to_keep: int,
        ignore_tags_regex: str,
        dry_run: bool,
        protect_active_pod_images: bool,
        targets: Iterable[KubernetesTarget],
    ) -> tuple[DeletionReport, ...]:
        """Discover, protect, and optionally delete stale ECR image digests.

        Args:
            repository_name_contains: Required eligible repository substring.
            images_to_keep: Master branch retention count.
            ignore_tags_regex: Regex for tags protected from retention selection.
            dry_run: Whether to report candidates without ECR deletion.
            protect_active_pod_images: Enables Kubernetes protection.
            targets: Kubernetes clusters that must all inventory successfully.

        Returns:
            Per-repository cleanup reports.

        Raises:
            ConfigurationError: If a non-dry-run does not enable protection.
            ProtectionInventoryError: Propagated if an inventory cannot complete.
            DeletionError: Propagated if ECR rejects a deletion.
        """
        target_values = tuple(targets)
        _validate_safety(dry_run, protect_active_pod_images, target_values)
        protected = self._collect_protected(protect_active_pod_images, target_values)
        reports = []

        for repository in iter_eligible_repositories(
            self._ecr_client, repository_name_contains
        ):
            candidates = discover_candidates(
                self._ecr_client, repository, images_to_keep, ignore_tags_regex
            )
            deletable = _exclude_protected(candidates, protected)
            protected_count = len(candidates) - len(deletable)
            self._log_selection(repository.uri, candidates, protected, deletable, dry_run)

            if not dry_run and deletable:
                refreshed = self._inventory_collector.collect(target_values)
                deletable = _exclude_protected(candidates, refreshed)
                protected_count = len(candidates) - len(deletable)
                deleted = delete_candidates(self._ecr_client, deletable)
            else:
                deleted = 0

            reports.append(
                DeletionReport(
                    repository=repository,
                    candidates=len(candidates),
                    protected=protected_count,
                    deletable=len(deletable),
                    deleted=deleted,
                )
            )
        return tuple(reports)

    def _collect_protected(
        self,
        enabled: bool,
        targets: tuple[KubernetesTarget, ...],
    ) -> tuple[ProtectedImage, ...]:
        """Collect active images only when the caller enabled protection."""
        if not enabled:
            self._logger("WARNING: active Pod image protection is disabled")
            return ()
        protected = self._inventory_collector.collect(targets)
        self._logger("Protected active Kubernetes image digests: {}".format(len(protected)))
        return protected

    def _log_selection(
        self,
        repository_uri: str,
        candidates: tuple[ImageCandidate, ...],
        protected: tuple[ProtectedImage, ...],
        deletable: tuple[ImageCandidate, ...],
        dry_run: bool,
    ) -> None:
        """Log candidate decisions without logging bearer tokens."""
        self._logger(
            "Repository {}: candidates={}, protected={}, deletable={}, dry_run={}".format(
                repository_uri,
                len(candidates),
                len(candidates) - len(deletable),
                len(deletable),
                dry_run,
            )
        )
        candidate_keys = {_candidate_key(candidate) for candidate in candidates}
        for image in protected:
            if (image.repository_uri, image.digest) in candidate_keys:
                self._logger(
                    "Protected {}@{} used by {}/{}/{}".format(
                        image.repository_uri,
                        image.digest,
                        image.cluster_name,
                        image.namespace,
                        image.pod_name,
                    )
                )


def _validate_safety(
    dry_run: bool,
    protect_active_pod_images: bool,
    targets: tuple[KubernetesTarget, ...],
) -> None:
    """Require active Pod protection before any destructive invocation."""
    if dry_run:
        return
    if not protect_active_pod_images:
        raise ConfigurationError(
            "Non-dry-run cleanup requires --protect-active-pod-images"
        )
    if not targets:
        raise ConfigurationError(
            "Non-dry-run cleanup requires --k8s-targets-file"
        )


def _exclude_protected(
    candidates: tuple[ImageCandidate, ...],
    protected: tuple[ProtectedImage, ...],
) -> tuple[ImageCandidate, ...]:
    """Remove candidates whose repository URI and digest are actively used."""
    protected_keys = {(image.repository_uri, image.digest) for image in protected}
    return tuple(
        candidate for candidate in candidates if _candidate_key(candidate) not in protected_keys
    )


def _candidate_key(candidate: ImageCandidate) -> tuple[str, str]:
    """Return the repository-qualified identity for an ECR candidate."""
    return candidate.repository.uri, candidate.digest
