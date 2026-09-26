"""Configuration parsing for the command-line cleanup job."""
import json
import os
from pathlib import Path
from typing import Iterable, Mapping, Optional

from ecr_cleanup.errors import ConfigurationError
from ecr_cleanup.models import KubernetesTarget


def load_kubernetes_targets(path: Optional[str]) -> tuple[KubernetesTarget, ...]:
    """Load and validate non-secret Kubernetes target configuration.

    Args:
        path: JSON file path, or ``None`` when Kubernetes protection is absent.

    Returns:
        Validated targets in declared order.

    Raises:
        ConfigurationError: If the file cannot be read or has an invalid schema.
    """
    if path is None:
        return ()

    try:
        decoded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError("Cannot load Kubernetes targets: {}".format(error))

    if not isinstance(decoded, list) or not decoded:
        raise ConfigurationError("Kubernetes targets must be a non-empty JSON list")

    targets = tuple(_parse_target(item, index) for index, item in enumerate(decoded))
    duplicate_keys = _duplicates(
        "{}:{}".format(target.cluster_name, target.region) for target in targets
    )
    if duplicate_keys:
        raise ConfigurationError(
            "Duplicate Kubernetes cluster target(s): {}".format(", ".join(duplicate_keys))
        )
    return targets


def get_target_token(target: KubernetesTarget, environment: Mapping[str, str] = os.environ) -> str:
    """Retrieve a target's bearer token without ever logging its value.

    Args:
        target: Target whose token variable will be read.
        environment: Environment mapping, injectable for tests.

    Returns:
        Non-empty ServiceAccount bearer token.

    Raises:
        ConfigurationError: If the configured variable is not available.
    """
    token = environment.get(target.token_env)
    if not token:
        raise ConfigurationError(
            "Missing ServiceAccount token environment variable '{}'".format(target.token_env)
        )
    return token


def _parse_target(value: object, index: int) -> KubernetesTarget:
    """Convert one decoded JSON object to a Kubernetes target."""
    if not isinstance(value, dict):
        raise ConfigurationError("Kubernetes target {} must be an object".format(index))

    required = ("cluster_name", "region", "token_env")
    missing = [key for key in required if not isinstance(value.get(key), str) or not value[key].strip()]
    if missing:
        raise ConfigurationError(
            "Kubernetes target {} has invalid required field(s): {}".format(
                index, ", ".join(missing)
            )
        )

    profile = value.get("aws_profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise ConfigurationError("Kubernetes target {} has invalid aws_profile".format(index))

    return KubernetesTarget(
        cluster_name=value["cluster_name"].strip(),
        region=value["region"].strip(),
        token_env=value["token_env"].strip(),
        aws_profile=profile.strip() if profile else None,
    )


def _duplicates(values: Iterable[str]) -> tuple[str, ...]:
    """Return duplicate values while preserving the first duplicate order."""
    seen = set()
    duplicates = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return tuple(duplicates)
