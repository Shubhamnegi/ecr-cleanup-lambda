"""Tests for non-secret Kubernetes target configuration."""
import json

import pytest

from ecr_cleanup.config import get_target_token, load_kubernetes_targets
from ecr_cleanup.errors import ConfigurationError
from ecr_cleanup.models import KubernetesTarget


def test_load_targets_returns_valid_multiple_targets(tmp_path):
    """Valid target JSON creates immutable target values."""
    path = tmp_path / "targets.json"
    path.write_text(json.dumps([
        {"cluster_name": "prod", "region": "us-east-1", "token_env": "PROD_TOKEN"},
        {"cluster_name": "qa", "region": "us-west-2", "token_env": "QA_TOKEN", "aws_profile": "example-profile"},
    ]))

    targets = load_kubernetes_targets(str(path))

    assert targets == (
        KubernetesTarget("prod", "us-east-1", "PROD_TOKEN"),
        KubernetesTarget("qa", "us-west-2", "QA_TOKEN", "example-profile"),
    )


@pytest.mark.parametrize("contents", ["{}", "[]", "not-json"])
def test_load_targets_rejects_invalid_top_level(tmp_path, contents):
    """A target file must be a non-empty JSON list."""
    path = tmp_path / "targets.json"
    path.write_text(contents)

    with pytest.raises(ConfigurationError):
        load_kubernetes_targets(str(path))


def test_load_targets_rejects_missing_and_duplicate_fields(tmp_path):
    """Required field and duplicate cluster validation is fail-closed."""
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps([{"cluster_name": "prod"}]))
    with pytest.raises(ConfigurationError, match="required"):
        load_kubernetes_targets(str(missing))

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps([
        {"cluster_name": "prod", "region": "us-east-1", "token_env": "A"},
        {"cluster_name": "prod", "region": "us-east-1", "token_env": "B"},
    ]))
    with pytest.raises(ConfigurationError, match="Duplicate"):
        load_kubernetes_targets(str(duplicate))


def test_load_targets_handles_absent_file_and_invalid_profile(tmp_path):
    """Optional target config can be absent but profiles must be valid strings."""
    assert load_kubernetes_targets(None) == ()
    with pytest.raises(ConfigurationError):
        load_kubernetes_targets(str(tmp_path / "none.json"))

    path = tmp_path / "profile.json"
    path.write_text(json.dumps([
        {"cluster_name": "prod", "region": "r", "token_env": "TOKEN", "aws_profile": 3}
    ]))
    with pytest.raises(ConfigurationError, match="aws_profile"):
        load_kubernetes_targets(str(path))


def test_get_target_token_never_accepts_empty_value():
    """Token lookup returns only non-empty masked credential values."""
    target = KubernetesTarget("prod", "region", "TOKEN")
    assert get_target_token(target, {"TOKEN": "secret"}) == "secret"
    with pytest.raises(ConfigurationError, match="TOKEN"):
        get_target_token(target, {})
