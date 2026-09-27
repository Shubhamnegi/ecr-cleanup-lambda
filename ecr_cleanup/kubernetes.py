"""Kubernetes active-image discovery through EKS and ServiceAccount tokens."""
import base64
import binascii
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Optional

import boto3

from ecr_cleanup.config import get_target_token
from ecr_cleanup.errors import ProtectionInventoryError
from ecr_cleanup.models import KubernetesTarget, ProtectedImage

_DIGEST_PATTERN = re.compile(r"(?:@|^)(sha256:[0-9a-fA-F]{64})$")
_TERMINAL_PHASES = frozenset(("Succeeded", "Failed"))
_STATUS_GROUPS = (
    ("containers", "container_statuses"),
    ("init_containers", "init_container_statuses"),
    ("ephemeral_containers", "ephemeral_container_statuses"),
)

EksClientFactory = Callable[[KubernetesTarget], Any]
CoreApiFactory = Callable[[str, str, str], Any]
ImageReferenceResolver = Callable[[str, str], Optional[str]]


class KubernetesInventoryCollector:
    """Collect normalized active ECR image digests from Kubernetes targets.

    The collector is intentionally fail-closed. Any configuration, API, TLS, or
    parsing problem is converted to ``ProtectionInventoryError`` so callers can
    prevent ECR deletion.

    Args:
        eks_client_factory: Creates an EKS client for a target.
        core_api_factory: Creates a Kubernetes CoreV1 API client.
        environment: Source of target token environment variables.
    """

    def __init__(
        self,
        eks_client_factory: Optional[EksClientFactory] = None,
        core_api_factory: Optional[CoreApiFactory] = None,
        environment: Mapping[str, str] = os.environ,
    ) -> None:
        self._eks_client_factory = eks_client_factory or _default_eks_client
        self._core_api_factory = core_api_factory or _default_core_api
        self._environment = environment

    def collect(
        self,
        targets: Iterable[KubernetesTarget],
        image_reference_resolver: Optional[ImageReferenceResolver] = None,
    ) -> tuple[ProtectedImage, ...]:
        """Collect active ECR images from every configured target.

        Args:
            targets: Kubernetes targets to inventory.
            image_reference_resolver: Resolves a tagged ECR spec image when a
                container has not yet reported an immutable runtime image ID.

        Returns:
            Unique protected images with their Pod origin.

        Raises:
            ProtectionInventoryError: If any target cannot be safely inventoried.
        """
        protected = set()
        for target in targets:
            protected.update(self._collect_target(target, image_reference_resolver))
        return tuple(
            sorted(
                protected,
                key=lambda image: (
                    image.repository_uri,
                    image.digest,
                    image.cluster_name,
                    image.namespace,
                    image.pod_name,
                ),
            )
        )

    def _collect_target(
        self,
        target: KubernetesTarget,
        image_reference_resolver: Optional[ImageReferenceResolver],
    ) -> tuple[ProtectedImage, ...]:
        """Collect protected images for one target."""
        try:
            token = get_target_token(target, self._environment)
            cluster = self._eks_client_factory(target).describe_cluster(name=target.cluster_name)["cluster"]
            endpoint = cluster["endpoint"]
            ca_data = cluster["certificateAuthority"]["data"]
            core_api = self._core_api_factory(endpoint, ca_data, token)
            try:
                return tuple(
                    image
                    for pod in _iter_active_pods(core_api)
                    for image in _protected_images_from_pod(
                        pod, target.cluster_name, image_reference_resolver
                    )
                )
            finally:
                close = getattr(core_api, "close", None)
                if callable(close):
                    close()
        except ProtectionInventoryError:
            raise
        except Exception as error:
            raise ProtectionInventoryError(
                "Cannot inventory active images in {} ({}): {}".format(
                    target.cluster_name, target.region, error
                )
            )


def _default_eks_client(target: KubernetesTarget) -> Any:
    """Build an EKS client using the target's optional boto3 profile."""
    session = boto3.Session(profile_name=target.aws_profile) if target.aws_profile else boto3.Session()
    return session.client("eks", region_name=target.region)


def _default_core_api(endpoint: str, ca_data: str, token: str) -> Any:
    """Build a TLS-verifying Kubernetes CoreV1 client from EKS metadata."""
    try:
        from kubernetes import client
    except ImportError as error:
        raise ProtectionInventoryError(
            "The 'kubernetes' package is required for active-image protection"
        ) from error

    ca_path = _create_ca_file(ca_data)
    try:
        configuration = client.Configuration()
        configuration.host = endpoint
        configuration.ssl_ca_cert = ca_path
        configuration.verify_ssl = True
        configuration.api_key["authorization"] = "Bearer {}".format(token)
        api_client = client.ApiClient(configuration=configuration)
        return _CoreApiAdapter(client.CoreV1Api(api_client), api_client, ca_path)
    except Exception:
        os.unlink(ca_path)
        raise


class _CoreApiAdapter:
    """Keep a Kubernetes API client and decoded CA file alive for pagination."""

    __slots__ = ("_api", "_api_client", "_ca_path")

    def __init__(self, api: Any, api_client: Any, ca_path: str) -> None:
        self._api = api
        self._api_client = api_client
        self._ca_path = ca_path

    def list_pod_for_all_namespaces(self, **kwargs: Any) -> Any:
        """Delegate the Kubernetes Pod-list call."""
        return self._api.list_pod_for_all_namespaces(**kwargs)

    def close(self) -> None:
        """Close the API client and remove the decoded CA certificate."""
        try:
            self._api_client.close()
        finally:
            try:
                os.unlink(self._ca_path)
            except FileNotFoundError:
                pass


def _create_ca_file(encoded_ca: str) -> str:
    """Decode EKS CA data to a restrictive temporary file.

    Args:
        encoded_ca: Base64-encoded PEM certificate returned by EKS.

    Returns:
        Temporary PEM path.

    Raises:
        ProtectionInventoryError: If CA data is not valid base64.
    """
    try:
        certificate = base64.b64decode(encoded_ca, validate=True)
    except (TypeError, ValueError, binascii.Error) as error:
        raise ProtectionInventoryError("EKS returned invalid certificate authority data") from error

    file_descriptor, path = tempfile.mkstemp(prefix="ecr-cleanup-ca-", suffix=".crt")
    with os.fdopen(file_descriptor, "wb") as certificate_file:
        certificate_file.write(certificate)
    return path


def _iter_active_pods(core_api: Any) -> Iterator[dict[str, Any]]:
    """Yield all non-terminal Pods from paginated Kubernetes API responses."""
    continue_token: Optional[str] = None
    while True:
        response = core_api.list_pod_for_all_namespaces(limit=500, _continue=continue_token)
        response_dict = _as_dict(response)
        for pod in response_dict.get("items", []):
            phase = _nested(pod, "status", "phase")
            if phase not in _TERMINAL_PHASES:
                yield pod
        continue_token = (
            _nested(response_dict, "metadata", "continue")
            or _nested(response_dict, "metadata", "_continue")
        )
        if not continue_token:
            return


def _protected_images_from_pod(
    pod: dict[str, Any],
    cluster_name: str,
    image_reference_resolver: Optional[ImageReferenceResolver] = None,
) -> tuple[ProtectedImage, ...]:
    """Extract all active ECR images from one Pod.

    Runtime image IDs are authoritative. Pods that have not yet populated one
    are mapped through their ECR tag so Pending and Unknown Pods remain in the
    active-image protection set.
    """
    namespace = _nested(pod, "metadata", "namespace") or "default"
    pod_name = _nested(pod, "metadata", "name") or "<unknown>"
    images = []
    spec = pod.get("spec", {})
    status = pod.get("status", {})

    for spec_key, status_key in _STATUS_GROUPS:
        spec_by_name = {
            container.get("name"): container.get("image", "")
            for container in spec.get(spec_key, []) or []
            if container.get("name")
        }
        status_by_name = {
            container_status.get("name"): container_status
            for container_status in status.get(status_key, []) or []
            if container_status.get("name")
        }
        for name, spec_image in spec_by_name.items():
            if _is_private_ecr_image(spec_image) and name not in status_by_name:
                resolved = _resolve_spec_image(spec_image, image_reference_resolver)
                if resolved is not None:
                    repository_uri, digest = resolved
                    images.append(
                        ProtectedImage(repository_uri, digest, cluster_name, namespace, pod_name)
                    )
        for container_status in status.get(status_key, []) or []:
            name = container_status.get("name")
            spec_image = spec_by_name.get(name, "")
            if not _is_private_ecr_image(spec_image):
                continue
            image_id = container_status.get("image_id") or container_status.get("imageID")
            if not image_id:
                resolved = _resolve_spec_image(spec_image, image_reference_resolver)
                if resolved is None:
                    continue
                repository_uri, digest = resolved
            else:
                repository_uri, digest = _normalize_ecr_reference(spec_image, image_id)
            images.append(
                ProtectedImage(
                    repository_uri=repository_uri,
                    digest=digest,
                    cluster_name=cluster_name,
                    namespace=namespace,
                    pod_name=pod_name,
                )
            )
    return tuple(images)


def _resolve_spec_image(
    spec_image: str,
    image_reference_resolver: Optional[ImageReferenceResolver],
) -> Optional[tuple[str, str]]:
    """Resolve a tagged ECR spec image when Kubernetes has no runtime digest."""
    repository_uri = _strip_tag_or_digest(spec_image)
    digest_match = _DIGEST_PATTERN.search(spec_image)
    if digest_match:
        return repository_uri, digest_match.group(1).lower()
    if image_reference_resolver is None:
        raise ProtectionInventoryError(
            "Active ECR image '{}' needs an ECR tag resolver".format(spec_image)
        )
    digest = image_reference_resolver(repository_uri, _image_tag(spec_image))
    if digest is None:
        return None
    if not _DIGEST_PATTERN.fullmatch(digest):
        raise ProtectionInventoryError(
            "ECR resolver returned an invalid digest for active image '{}'".format(spec_image)
        )
    return repository_uri, digest.lower()


def _normalize_ecr_reference(spec_image: str, image_id: str) -> tuple[str, str]:
    """Return repository URI and resolved digest for a Pod container image."""
    repository_uri = _strip_tag_or_digest(spec_image)
    normalized_image_id = image_id.split("://", 1)[-1]
    digest_match = _DIGEST_PATTERN.search(normalized_image_id)
    if not digest_match:
        raise ProtectionInventoryError(
            "Cannot extract immutable digest from active ECR image ID '{}'".format(image_id)
        )
    image_id_repository = normalized_image_id.split("@", 1)[0]
    if _is_private_ecr_image(image_id_repository) and image_id_repository != repository_uri:
        raise ProtectionInventoryError(
            "Active ECR image ID repository does not match Pod image '{}'".format(spec_image)
        )
    return repository_uri, digest_match.group(1).lower()


def _strip_tag_or_digest(image: str) -> str:
    """Remove a tag or digest while preserving registry host and repository path."""
    without_digest = image.split("@", 1)[0]
    slash_index = without_digest.rfind("/")
    colon_index = without_digest.rfind(":")
    if colon_index > slash_index:
        return without_digest[:colon_index]
    return without_digest


def _image_tag(image: str) -> str:
    """Return a tagged image's requested tag, defaulting to Docker's latest."""
    without_digest = image.split("@", 1)[0]
    slash_index = without_digest.rfind("/")
    colon_index = without_digest.rfind(":")
    if colon_index > slash_index:
        return without_digest[colon_index + 1:]
    return "latest"


def _is_private_ecr_image(image: str) -> bool:
    """Return whether a Pod spec image uses an Amazon ECR private registry."""
    return ".dkr.ecr." in image and ".amazonaws.com/" in image


def _as_dict(value: Any) -> dict[str, Any]:
    """Convert Kubernetes model responses to dictionaries for deterministic parsing."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        if isinstance(converted, dict):
            return converted
    raise ProtectionInventoryError("Kubernetes API returned an unsupported response")


def _nested(value: Mapping[str, Any], *keys: str) -> Any:
    """Read a nested mapping key without treating absent keys as failures."""
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current
