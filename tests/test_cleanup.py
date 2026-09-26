"""Tests for fail-closed ECR cleanup orchestration."""
from datetime import datetime, timedelta, timezone

import pytest

from ecr_cleanup.cleanup import CleanupService
from ecr_cleanup.errors import ConfigurationError, ProtectionInventoryError
from ecr_cleanup.models import KubernetesTarget, ProtectedImage
from tests.conftest import DIGEST_A, DIGEST_B, FakeEcrClient, REPOSITORY, image, repository


class FakeInventory:
    """Injectable active-image inventory with fixed responses or errors."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def collect(self, targets):
        self.calls.append(tuple(targets))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def _client_with_old_master():
    """Create one eligible repository with a stale master digest."""
    now = datetime(2025, 1, 2, tzinfo=timezone.utc)
    return FakeEcrClient(
        [{"repositories": [repository(), repository("ignored")]}],
        [{"imageDetails": [
            image(DIGEST_A, ["master-new"], now),
            image(DIGEST_B, ["master-old"], now - timedelta(days=1)),
        ]}],
    )


def _protected(digest=DIGEST_B):
    """Create a protected image in the repository under test."""
    return ProtectedImage(REPOSITORY.uri, digest, "prod", "default", "orders")


def test_dry_run_excludes_protected_digest_without_delete():
    """Dry run reports active Pod protection and cannot call ECR deletion."""
    client = _client_with_old_master()
    inventory = FakeInventory([(_protected(),)])
    logs = []
    service = CleanupService(client, inventory, logs.append)

    reports = service.run(
        repository_name_contains="-service",
        images_to_keep=1,
        ignore_tags_regex="^$",
        dry_run=True,
        protect_active_pod_images=True,
        targets=(KubernetesTarget("prod", "r", "TOKEN"),),
    )

    assert reports[0].protected == 1
    assert reports[0].deletable == 0
    assert reports[0].deleted == 0
    assert client.delete_calls == []
    assert len(inventory.calls) == 1
    assert any("Protected" in log for log in logs)
    assert not any("Would delete" in log for log in logs)


def test_real_delete_rechecks_and_deletes_only_unprotected_digest():
    """A destructive run performs an initial inventory and a final recheck."""
    client = _client_with_old_master()
    inventory = FakeInventory([(), ()])
    service = CleanupService(client, inventory, lambda _: None)

    reports = service.run(
        repository_name_contains="-service",
        images_to_keep=1,
        ignore_tags_regex="^$",
        dry_run=False,
        protect_active_pod_images=True,
        targets=(KubernetesTarget("prod", "r", "TOKEN"),),
    )

    assert reports[0].deleted == 1
    assert len(inventory.calls) == 2
    assert client.delete_calls[0]["imageIds"] == [{"imageDigest": DIGEST_B}]


def test_final_recheck_can_protect_previously_deletable_digest():
    """Fresh Pod inventory wins over an earlier empty inventory."""
    client = _client_with_old_master()
    inventory = FakeInventory([(), (_protected(),)])
    service = CleanupService(client, inventory, lambda _: None)

    reports = service.run(
        repository_name_contains="-service",
        images_to_keep=1,
        ignore_tags_regex="^$",
        dry_run=False,
        protect_active_pod_images=True,
        targets=(KubernetesTarget("prod", "r", "TOKEN"),),
    )

    assert reports[0].protected == 1
    assert reports[0].deleted == 0
    assert client.delete_calls == []


@pytest.mark.parametrize(
    ("enabled", "targets", "message"),
    [
        (False, (), "requires --protect"),
        (True, (), "requires --k8s"),
    ],
)
def test_real_delete_requires_active_pod_protection(enabled, targets, message):
    """Non-dry-run operation cannot bypass target-backed protection."""
    service = CleanupService(_client_with_old_master(), FakeInventory([()]), lambda _: None)
    with pytest.raises(ConfigurationError, match=message):
        service.run(
            repository_name_contains="-service",
            images_to_keep=1,
            ignore_tags_regex="^$",
            dry_run=False,
            protect_active_pod_images=enabled,
            targets=targets,
        )


def test_inventory_failure_prevents_any_ecr_delete_call():
    """Protection API failures abort before a destructive ECR request."""
    client = _client_with_old_master()
    service = CleanupService(
        client,
        FakeInventory([ProtectionInventoryError("cluster unavailable")]),
        lambda _: None,
    )
    with pytest.raises(ProtectionInventoryError):
        service.run(
            repository_name_contains="-service",
            images_to_keep=1,
            ignore_tags_regex="^$",
            dry_run=False,
            protect_active_pod_images=True,
            targets=(KubernetesTarget("prod", "r", "TOKEN"),),
        )
    assert client.delete_calls == []


def test_dry_run_can_expose_legacy_selection_with_explicit_warning():
    """Unprotected dry runs remain possible but never delete images."""
    client = _client_with_old_master()
    logs = []
    service = CleanupService(client, FakeInventory([]), logs.append)

    reports = service.run(
        repository_name_contains="-service",
        images_to_keep=1,
        ignore_tags_regex="^$",
        dry_run=True,
        protect_active_pod_images=False,
        targets=(),
    )

    assert reports[0].deletable == 1
    assert client.delete_calls == []
    assert "disabled" in logs[0]
    assert any("Would delete {}@{}".format(REPOSITORY.uri, DIGEST_B) == log for log in logs)
