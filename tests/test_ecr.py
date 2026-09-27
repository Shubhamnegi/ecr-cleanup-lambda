"""Tests for ECR repository filtering and retention/deletion behavior."""
from datetime import datetime, timedelta, timezone

import pytest

from ecr_cleanup.ecr import CandidateImageResolver, delete_candidates, discover_candidates, iter_eligible_repositories
from ecr_cleanup.errors import DeletionError, ProtectionInventoryError
from ecr_cleanup.models import ImageCandidate
from tests.conftest import DIGEST_A, DIGEST_B, FakeEcrClient, REPOSITORY, image, repository


def test_iter_eligible_repositories_filters_every_paginator_page():
    """Only repository names containing the safety substring are yielded."""
    client = FakeEcrClient(
        [{"repositories": [repository("orders-service"), repository("web")]}, {"repositories": [repository("billing-service")]}],
        [],
    )

    found = tuple(iter_eligible_repositories(client, "-service"))

    assert [item.name for item in found] == ["orders-service", "billing-service"]


def test_iter_eligible_repositories_never_allows_bypassing_service_filter():
    """The required -service scope cannot be widened through a CLI option."""
    client = FakeEcrClient([], [])
    with pytest.raises(ValueError, match="required '-service'"):
        tuple(iter_eligible_repositories(client, ""))


def test_discover_candidates_keeps_master_count_and_one_develop_image():
    """Existing branch-retention policy is calculated before deletion."""
    now = datetime(2025, 1, 5, tzinfo=timezone.utc)
    client = FakeEcrClient([], [{"imageDetails": [
        image(DIGEST_A, ["master-new"], now),
        image(DIGEST_B, ["master-old"], now - timedelta(days=1)),
        image("sha256:" + "c" * 64, ["develop-new"], now),
        image("sha256:" + "d" * 64, ["develop-old"], now - timedelta(days=1)),
        image("sha256:" + "e" * 64),
    ]}])

    candidates = discover_candidates(client, REPOSITORY, 1, "^$")

    assert {candidate.digest for candidate in candidates} == {
        DIGEST_B,
        "sha256:" + "d" * 64,
        "sha256:" + "e" * 64,
    }
    assert len(client.image_paginator.calls) == 1


def test_discover_candidates_honors_latest_and_ignore_regex():
    """Protected tags do not make their older digest deletable."""
    now = datetime(2025, 1, 2, tzinfo=timezone.utc)
    client = FakeEcrClient([], [{"imageDetails": [
        image(DIGEST_A, ["master-new"], now),
        image(DIGEST_B, ["master-latest", "release-master"], now - timedelta(days=1)),
    ]}])

    assert discover_candidates(client, REPOSITORY, 1, "release") == ()


def test_discover_candidates_rejects_invalid_retention_and_image_metadata():
    """Invalid inputs cannot silently select a surprising image set."""
    client = FakeEcrClient([], [{"imageDetails": []}])
    with pytest.raises(ValueError, match="non-negative"):
        discover_candidates(client, REPOSITORY, -1, "^$")

    invalid = FakeEcrClient([], [{"imageDetails": [{"imageDigest": DIGEST_A, "imageTags": ["master"]}]}])
    with pytest.raises(ValueError, match="imagePushedAt"):
        discover_candidates(invalid, REPOSITORY, 1, "^$")


def test_delete_candidates_batches_and_rejects_cross_repository():
    """ECR delete requests stay within 100 IDs and one repository."""
    client = FakeEcrClient([], [])
    candidates = tuple(
        ImageCandidate(REPOSITORY, "sha256:{:064x}".format(index)) for index in range(101)
    )

    assert delete_candidates(client, candidates) == 101
    assert [len(call["imageIds"]) for call in client.delete_calls] == [100, 1]
    assert delete_candidates(client, ()) == 0

    other = ImageCandidate(
        type(REPOSITORY)(REPOSITORY.registry_id, "other-service", REPOSITORY.uri + "-other"),
        DIGEST_A,
    )
    with pytest.raises(DeletionError, match="one repository"):
        delete_candidates(client, (ImageCandidate(REPOSITORY, DIGEST_A), other))


def test_delete_candidates_surfaces_ecr_failures():
    """Batch failures produce a non-success cleanup result."""
    client = FakeEcrClient([], [], failures=[{"failureCode": "ImageNotFound"}])
    with pytest.raises(DeletionError, match="rejected"):
        delete_candidates(client, (ImageCandidate(REPOSITORY, DIGEST_A),))


def test_delete_candidates_skips_manifest_list_children_and_continues():
    """Manifest-list children are logged skips and do not stop later batches."""
    candidates = tuple(
        ImageCandidate(REPOSITORY, "sha256:{:064x}".format(index))
        for index in range(101)
    )
    skipped_digest = candidates[0].digest
    client = FakeEcrClient(
        [],
        [],
        delete_failures_by_call=[
            [{
                "imageId": {"imageDigest": skipped_digest},
                "failureCode": "ImageReferencedByManifestList",
            }],
            [],
        ],
    )
    logs = []

    deleted = delete_candidates(client, candidates, logs.append)

    assert deleted == 100
    assert [len(call["imageIds"]) for call in client.delete_calls] == [100, 1]
    assert logs == [
        "Skipped {}@{}: image is referenced by an ECR manifest list".format(
            REPOSITORY.uri, skipped_digest
        )
    ]


def test_candidate_image_resolver_maps_only_candidate_repositories():
    """Pending Pod tags resolve only when their repository can be deleted."""
    client = FakeEcrClient(
        [], [], image_lookup={"images": [{"imageId": {"imageDigest": DIGEST_A}}]}
    )
    resolver = CandidateImageResolver(client, (ImageCandidate(REPOSITORY, DIGEST_B),))

    assert resolver.resolve(REPOSITORY.uri, "master-old") == DIGEST_A
    assert client.get_image_calls == [{
        "registryId": REPOSITORY.registry_id,
        "repositoryName": REPOSITORY.name,
        "imageIds": [{"imageTag": "master-old"}],
    }]
    assert resolver.resolve(REPOSITORY.uri, "master-old") == DIGEST_A
    assert len(client.get_image_calls) == 1
    assert resolver.resolve(REPOSITORY.uri + "-other", "master-old") is None


def test_candidate_image_resolver_ignores_missing_tags_and_fails_for_ecr_errors():
    """A deleted Pod tag is harmless, but lookup ambiguity blocks cleanup."""
    missing = FakeEcrClient(
        [], [], image_lookup={"images": [], "failures": [{"failureCode": "ImageNotFound"}]}
    )
    resolver = CandidateImageResolver(missing, (ImageCandidate(REPOSITORY, DIGEST_B),))
    assert resolver.resolve(REPOSITORY.uri, "removed") is None

    broken = FakeEcrClient([], [], image_lookup={"images": []})
    resolver = CandidateImageResolver(broken, (ImageCandidate(REPOSITORY, DIGEST_B),))
    with pytest.raises(ProtectionInventoryError, match="unambiguous"):
        resolver.resolve(REPOSITORY.uri, "master-old")


@pytest.mark.parametrize(
    "response, message",
    [
        ({"images": [], "failures": [{"failureCode": "AccessDenied"}]}, "AccessDenied"),
        ({"images": [{"imageId": {"imageDigest": "not-a-digest"}}]}, "invalid digest"),
    ],
)
def test_candidate_image_resolver_fails_closed_for_invalid_ecr_responses(response, message):
    """Only an ECR digest or an explicit missing tag may continue cleanup."""
    client = FakeEcrClient([], [], image_lookup=response)
    resolver = CandidateImageResolver(client, (ImageCandidate(REPOSITORY, DIGEST_B),))

    with pytest.raises(ProtectionInventoryError, match=message):
        resolver.resolve(REPOSITORY.uri, "master-old")


def test_candidate_image_resolver_fails_closed_for_ecr_client_errors():
    """Transport errors do not make active-image protection optional."""
    client = FakeEcrClient([], [], image_lookup=RuntimeError("network unavailable"))
    resolver = CandidateImageResolver(client, (ImageCandidate(REPOSITORY, DIGEST_B),))

    with pytest.raises(ProtectionInventoryError, match="network unavailable"):
        resolver.resolve(REPOSITORY.uri, "master-old")
