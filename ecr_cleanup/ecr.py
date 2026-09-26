"""ECR discovery, retention selection, and deletion adapters."""
import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

from ecr_cleanup.errors import DeletionError, ProtectionInventoryError
from ecr_cleanup.models import ImageCandidate, Repository, TaggedImage

BRANCHES = ("master", "develop")
MAX_DELETE_BATCH_SIZE = 100


class CandidateImageResolver:
    """Resolve Pod image tags only for repositories with deletion candidates.

    Args:
        ecr_client: Boto3-compatible ECR client for the cleanup region.
        candidates: Digests selected by the retention policy.
    """

    def __init__(self, ecr_client: Any, candidates: Iterable[ImageCandidate]) -> None:
        self._ecr_client = ecr_client
        self._repositories = {
            candidate.repository.uri: candidate.repository for candidate in candidates
        }
        self._resolved_tags: dict[tuple[str, str], str | None] = {}

    def resolve(self, repository_uri: str, image_tag: str) -> str | None:
        """Resolve a tag to its ECR digest when that repository has candidates.

        A missing tag is not an ECR deletion risk and returns ``None``. Other
        ECR failures prevent cleanup because active-image protection could not
        be completed safely.
        """
        repository = self._repositories.get(repository_uri)
        if repository is None:
            return None
        lookup_key = repository_uri, image_tag
        if lookup_key in self._resolved_tags:
            return self._resolved_tags[lookup_key]
        try:
            response = self._ecr_client.batch_get_image(
                registryId=repository.registry_id,
                repositoryName=repository.name,
                imageIds=[{"imageTag": image_tag}],
            )
        except Exception as error:
            raise ProtectionInventoryError(
                "Cannot resolve active ECR image {}:{}: {}".format(
                    repository_uri, image_tag, error
                )
            ) from error

        failures = response.get("failures", [])
        if failures:
            if all(failure.get("failureCode") == "ImageNotFound" for failure in failures):
                self._resolved_tags[lookup_key] = None
                return None
            raise ProtectionInventoryError(
                "Cannot resolve active ECR image {}:{}: {}".format(
                    repository_uri, image_tag, failures
                )
            )
        images = response.get("images", [])
        if len(images) != 1:
            raise ProtectionInventoryError(
                "ECR returned no unambiguous digest for active image {}:{}".format(
                    repository_uri, image_tag
                )
            )
        digest = images[0].get("imageId", {}).get("imageDigest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ProtectionInventoryError(
                "ECR returned an invalid digest for active image {}:{}".format(
                    repository_uri, image_tag
                )
            )
        resolved_digest = digest.lower()
        self._resolved_tags[lookup_key] = resolved_digest
        return resolved_digest


def iter_eligible_repositories(ecr_client: Any, name_contains: str) -> Iterator[Repository]:
    """Yield ECR repositories whose name contains the configured substring.

    Args:
        ecr_client: Boto3-compatible ECR client.
        name_contains: Required repository-name substring.

    Yields:
        Eligible repositories in ECR paginator order.
    """
    if "-service" not in name_contains:
        raise ValueError(
            "repository-name-contains must include the required '-service' substring"
        )

    paginator = ecr_client.get_paginator("describe_repositories")
    for page in paginator.paginate():
        for raw_repository in page.get("repositories", []):
            name = raw_repository["repositoryName"]
            if name_contains not in name:
                continue
            yield Repository(
                registry_id=raw_repository["registryId"],
                name=name,
                uri=raw_repository["repositoryUri"],
            )


def discover_candidates(
    ecr_client: Any,
    repository: Repository,
    images_to_keep: int,
    ignore_tags_regex: str,
) -> tuple[ImageCandidate, ...]:
    """Calculate deletion candidates while preserving branch retention behavior.

    Untagged images are candidates. Tagged images are considered separately for
    ``master`` and ``develop``: master retains ``images_to_keep`` images and
    develop retains one. No deletion occurs in this function.

    Args:
        ecr_client: Boto3-compatible ECR client.
        repository: Repository to inspect.
        images_to_keep: Number of newest master images to retain.
        ignore_tags_regex: Tags matching this regex are not candidates.

    Returns:
        Unique digest candidates for the repository.

    Raises:
        ValueError: If the retention count or regex is invalid.
    """
    if images_to_keep < 0:
        raise ValueError("images_to_keep must be non-negative")

    ignore_tags = re.compile(ignore_tags_regex)
    image_details = tuple(_iter_image_details(ecr_client, repository))
    candidates = {
        image["imageDigest"]
        for image in image_details
        if not image.get("imageTags")
    }
    tagged_images = tuple(_to_tagged_image(image) for image in image_details if image.get("imageTags"))

    for branch in BRANCHES:
        matching = sorted(
            (image for image in tagged_images if _matches_branch(image.tags, branch)),
            key=lambda image: image.pushed_at,
            reverse=True,
        )
        keep_count = 1 if branch == "develop" else images_to_keep
        for image in matching[keep_count:]:
            if any(_is_deletable_tag(tag, ignore_tags) for tag in image.tags):
                candidates.add(image.digest)

    return tuple(
        ImageCandidate(repository=repository, digest=digest) for digest in sorted(candidates)
    )


def delete_candidates(ecr_client: Any, candidates: Iterable[ImageCandidate]) -> int:
    """Delete candidate digests in ECR's maximum supported batch size.

    Args:
        ecr_client: Boto3-compatible ECR client.
        candidates: Candidates for one ECR repository.

    Returns:
        Count of ECR digests submitted for deletion.

    Raises:
        DeletionError: If candidates span repositories or ECR reports failures.
    """
    selected = tuple(candidates)
    if not selected:
        return 0

    repository = selected[0].repository
    if any(candidate.repository != repository for candidate in selected):
        raise DeletionError("A deletion batch must contain one repository only")

    deleted = 0
    for batch in _chunks(selected, MAX_DELETE_BATCH_SIZE):
        response = ecr_client.batch_delete_image(
            registryId=repository.registry_id,
            repositoryName=repository.name,
            imageIds=[{"imageDigest": candidate.digest} for candidate in batch],
        )
        failures = response.get("failures", [])
        if failures:
            raise DeletionError("ECR rejected image deletion: {}".format(failures))
        deleted += len(batch)
    return deleted


def _iter_image_details(ecr_client: Any, repository: Repository) -> Iterator[dict[str, Any]]:
    """Yield paginated image detail dictionaries for one repository."""
    paginator = ecr_client.get_paginator("describe_images")
    for page in paginator.paginate(
        registryId=repository.registry_id,
        repositoryName=repository.name,
    ):
        yield from page.get("imageDetails", [])


def _to_tagged_image(image: dict[str, Any]) -> TaggedImage:
    """Convert an ECR response record to a typed tagged-image value."""
    pushed_at = image.get("imagePushedAt")
    if not isinstance(pushed_at, datetime):
        raise ValueError("ECR image {} is missing imagePushedAt".format(image["imageDigest"]))
    return TaggedImage(
        digest=image["imageDigest"],
        tags=tuple(image["imageTags"]),
        pushed_at=pushed_at,
    )


def _matches_branch(tags: tuple[str, ...], branch: str) -> bool:
    """Retain the original substring matching semantics for branch tags."""
    return re.search(branch, str(list(tags))) is not None


def _is_deletable_tag(tag: str, ignore_tags: re.Pattern[str]) -> bool:
    """Return whether a tag permits its digest to become a candidate."""
    return "latest" not in tag and ignore_tags.search(tag) is None


def _chunks(values: tuple[ImageCandidate, ...], size: int) -> Iterator[tuple[ImageCandidate, ...]]:
    """Yield fixed-size tuples from a candidate sequence."""
    for index in range(0, len(values), size):
        yield values[index:index + size]
