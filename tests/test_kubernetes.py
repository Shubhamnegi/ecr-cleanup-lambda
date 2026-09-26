"""Tests for fail-closed Kubernetes active-image inventory."""
import base64
from unittest.mock import Mock, patch

import pytest

from ecr_cleanup.errors import ProtectionInventoryError
from ecr_cleanup.kubernetes import (
    KubernetesInventoryCollector,
    _create_ca_file,
    _default_core_api,
    _default_eks_client,
    _normalize_ecr_reference,
    _strip_tag_or_digest,
)
from ecr_cleanup.models import KubernetesTarget
from tests.conftest import DIGEST_A, DIGEST_B, pod

REPO = "111111111111.dkr.ecr.us-east-1.amazonaws.com/orders-service"


class FakeCoreApi:
    """Kubernetes Core API fake with page recording."""

    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []
        self.closed = False

    def list_pod_for_all_namespaces(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.pages)

    def close(self):
        self.closed = True


def _cluster_response():
    """Return minimal EKS describe-cluster response."""
    return {
        "cluster": {
            "endpoint": "https://cluster.example",
            "certificateAuthority": {"data": base64.b64encode(b"test-ca").decode()},
        }
    }


def test_collector_protects_normal_init_and_ephemeral_active_images():
    """All non-terminal container status groups contribute protected digests."""
    active = pod(
        "active",
        containers=[{"name": "app", "image": REPO + ":v1"}],
        statuses=[{"name": "app", "image_id": "containerd://" + REPO + "@" + DIGEST_A}],
        init=[{"name": "init", "image": REPO + ":init"}],
        init_statuses=[{"name": "init", "image_id": "docker-pullable://" + REPO + "@" + DIGEST_B}],
        ephemeral=[{"name": "debug", "image": REPO + ":debug"}],
        ephemeral_statuses=[{"name": "debug", "image_id": REPO + "@sha256:" + "c" * 64}],
    )
    terminal = pod(
        "finished",
        phase="Succeeded",
        containers=[{"name": "app", "image": REPO + ":old"}],
        statuses=[{"name": "app", "image_id": REPO + "@sha256:" + "d" * 64}],
    )
    core = FakeCoreApi([{"items": [active, terminal], "metadata": {"continue": ""}}])
    eks = Mock()
    eks.describe_cluster.return_value = _cluster_response()
    target = KubernetesTarget("prod", "us-east-1", "TOKEN")
    collector = KubernetesInventoryCollector(lambda _: eks, lambda *_: core, {"TOKEN": "secret"})

    protected = collector.collect((target,))

    assert {item.digest for item in protected} == {DIGEST_A, DIGEST_B, "sha256:" + "c" * 64}
    assert all(item.repository_uri == REPO for item in protected)
    assert core.closed


def test_collector_paginates_and_keeps_pending_and_unknown_pods():
    """Every non-terminal phase is retained across Kubernetes continuation pages."""
    pending = pod(
        "pending",
        phase="Pending",
        containers=[{"name": "app", "image": REPO + ":v1"}],
        statuses=[{"name": "app", "image_id": REPO + "@" + DIGEST_A}],
    )
    unknown = pod(
        "unknown",
        phase="Unknown",
        containers=[{"name": "app", "image": REPO + ":v2"}],
        statuses=[{"name": "app", "image_id": REPO + "@" + DIGEST_B}],
    )
    core = FakeCoreApi([
        {"items": [pending], "metadata": {"continue": "next"}},
        {"items": [unknown], "metadata": {"continue": ""}},
    ])
    eks = Mock()
    eks.describe_cluster.return_value = _cluster_response()
    collector = KubernetesInventoryCollector(lambda _: eks, lambda *_: core, {"TOKEN": "secret"})

    found = collector.collect((KubernetesTarget("prod", "r", "TOKEN"),))

    assert {image.digest for image in found} == {DIGEST_A, DIGEST_B}
    assert [call["_continue"] for call in core.calls] == [None, "next"]


def test_collector_supports_kubernetes_model_continue_metadata():
    """Kubernetes client's model serialization uses _continue, not continue."""
    core = FakeCoreApi([
        {"items": [], "metadata": {"_continue": "next"}},
        {"items": [], "metadata": {"_continue": ""}},
    ])
    eks = Mock()
    eks.describe_cluster.return_value = _cluster_response()
    collector = KubernetesInventoryCollector(lambda _: eks, lambda *_: core, {"TOKEN": "secret"})

    assert collector.collect((KubernetesTarget("prod", "r", "TOKEN"),)) == ()
    assert [call["_continue"] for call in core.calls] == [None, "next"]


@pytest.mark.parametrize("image_id", ["containerd://not-a-digest", ""])
def test_collector_fails_closed_for_unresolved_active_ecr_images(image_id):
    """A non-terminal ECR Pod without a stable digest blocks deletion."""
    target = KubernetesTarget("prod", "r", "TOKEN")
    core = FakeCoreApi([{"items": [pod(
        "unsafe",
        containers=[{"name": "app", "image": REPO + ":v1"}],
        statuses=[{"name": "app", "image_id": image_id}],
    )], "metadata": {}}])
    eks = Mock()
    eks.describe_cluster.return_value = _cluster_response()
    collector = KubernetesInventoryCollector(lambda _: eks, lambda *_: core, {"TOKEN": "secret"})

    with pytest.raises(ProtectionInventoryError):
        collector.collect((target,))


def test_collector_fails_closed_when_active_ecr_container_has_no_status():
    """An ECR image awaiting resolution is not treated as safe to delete."""
    target = KubernetesTarget("prod", "r", "TOKEN")
    core = FakeCoreApi([{"items": [pod(
        "starting",
        phase="Pending",
        containers=[{"name": "app", "image": REPO + ":v1"}],
    )], "metadata": {}}])
    eks = Mock()
    eks.describe_cluster.return_value = _cluster_response()
    collector = KubernetesInventoryCollector(lambda _: eks, lambda *_: core, {"TOKEN": "secret"})

    with pytest.raises(ProtectionInventoryError, match="unresolved"):
        collector.collect((target,))


def test_collector_ignores_non_ecr_images_and_fails_for_missing_token():
    """Only ECR references are protected while credential errors fail closed."""
    core = FakeCoreApi([{"items": [pod(
        "public",
        containers=[{"name": "app", "image": "nginx:1.27"}],
        statuses=[{"name": "app", "image_id": "docker.io/nginx@" + DIGEST_A}],
    )], "metadata": {}}])
    eks = Mock()
    eks.describe_cluster.return_value = _cluster_response()
    collector = KubernetesInventoryCollector(lambda _: eks, lambda *_: core, {"TOKEN": "secret"})
    assert collector.collect((KubernetesTarget("prod", "r", "TOKEN"),)) == ()

    missing = KubernetesInventoryCollector(lambda _: eks, lambda *_: core, {})
    with pytest.raises(ProtectionInventoryError, match="Missing"):
        missing.collect((KubernetesTarget("prod", "r", "TOKEN"),))


def test_reference_helpers_normalize_tag_and_digest_forms():
    """Repository matching retains the registry path and uses immutable digest."""
    assert _strip_tag_or_digest(REPO + ":v1") == REPO
    assert _strip_tag_or_digest(REPO + "@" + DIGEST_A) == REPO
    assert _normalize_ecr_reference(REPO + ":v1", "docker-pullable://" + REPO + "@" + DIGEST_A) == (REPO, DIGEST_A)
    with pytest.raises(ProtectionInventoryError, match="immutable"):
        _normalize_ecr_reference(REPO + ":v1", "containerd://sha256:short")
    with pytest.raises(ProtectionInventoryError, match="does not match"):
        _normalize_ecr_reference(
            REPO + ":v1",
            "containerd://111111111111.dkr.ecr.us-east-1.amazonaws.com/other-service@" + DIGEST_A,
        )


def test_ca_file_rejects_invalid_data_and_cleans_up_valid_file():
    """CA data is decoded to a temporary file and malformed data is rejected."""
    with pytest.raises(ProtectionInventoryError):
        _create_ca_file("not base64!")

    path = _create_ca_file(base64.b64encode(b"certificate").decode())
    with open(path, "rb") as certificate:
        assert certificate.read() == b"certificate"
    import os
    os.unlink(path)


def test_default_eks_client_uses_profile_and_default_session():
    """Target profiles select an isolated boto3 session."""
    with patch("ecr_cleanup.kubernetes.boto3.Session") as session:
        _default_eks_client(KubernetesTarget("prod", "r", "TOKEN", "example-profile"))
        session.assert_called_with(profile_name="example-profile")
        session.return_value.client.assert_called_with("eks", region_name="r")

    with patch("ecr_cleanup.kubernetes.boto3.Session") as session:
        _default_eks_client(KubernetesTarget("prod", "r", "TOKEN"))
        session.assert_called_with()


def test_default_core_api_enforces_tls_and_removes_ca_file():
    """The production client has verification enabled and owns temporary CA data."""
    with patch("kubernetes.client.ApiClient") as api_client, patch("kubernetes.client.CoreV1Api") as core_api:
        adapter = _default_core_api(
            "https://cluster.example",
            base64.b64encode(b"certificate").decode(),
            "token",
        )
        configuration = api_client.call_args.kwargs["configuration"]
        assert configuration.host == "https://cluster.example"
        assert configuration.verify_ssl is True
        assert configuration.api_key["authorization"] == "Bearer token"
        path = configuration.ssl_ca_cert
        adapter.close()
        import os
        assert not os.path.exists(path)
        core_api.assert_called_once()
