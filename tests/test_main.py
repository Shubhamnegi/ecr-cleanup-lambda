"""Tests for the command-line entry point and legacy handler."""
from unittest.mock import Mock, patch

import main
from ecr_cleanup.errors import CleanupError
from ecr_cleanup.models import DeletionReport
from tests.conftest import REPOSITORY


def test_parser_defaults_to_service_filter_and_safe_dry_run(monkeypatch):
    """The CLI is safe by default and filters only service repositories."""
    monkeypatch.delenv("REPOSITORY_NAME_CONTAINS", raising=False)
    parser = main.build_parser()
    parsed = parser.parse_args([])

    assert parsed.repository_name_contains == "-service"
    assert main._is_dry_run(parsed.dryrun)
    assert main._is_dry_run("TRUE")
    assert not main._is_dry_run("false")


def test_run_wires_explicit_region_and_protection(monkeypatch):
    """CLI passes target-backed protection options into cleanup service."""
    targets = ()
    report = DeletionReport(REPOSITORY, candidates=3, protected=2, deletable=1, deleted=0)
    service = Mock()
    service.run.return_value = (report,)
    monkeypatch.setattr(main, "load_kubernetes_targets", lambda _: targets)
    monkeypatch.setattr(main, "KubernetesInventoryCollector", lambda: "inventory")
    monkeypatch.setattr(main.boto3, "client", lambda service_name, region_name=None: "ecr-client")
    monkeypatch.setattr(main, "CleanupService", lambda ecr, inventory: service)

    result = main.run(["-region", "us-east-1", "-dryrun", "true"])

    assert result == 0
    assert service.run.call_args.kwargs["repository_name_contains"] == "-service"
    assert service.run.call_args.kwargs["dry_run"] is True


def test_run_returns_safe_error_for_invalid_protection_configuration(monkeypatch):
    """A protection switch without targets cannot reach AWS clients."""
    monkeypatch.setattr(main, "load_kubernetes_targets", lambda _: ())
    with patch("main.boto3.client") as client:
        result = main.run(["--protect-active-pod-images"])
    assert result == 2
    client.assert_not_called()

    with patch("main.boto3.client") as client:
        result = main.run(["-dryrun", "false", "-region", "r"])
    assert result == 2
    client.assert_not_called()


def test_run_handles_domain_and_value_errors(monkeypatch):
    """Expected failures become a non-zero Jenkins-friendly exit code."""
    monkeypatch.setattr(main, "load_kubernetes_targets", lambda _: ())
    monkeypatch.setattr(main, "KubernetesInventoryCollector", lambda: "inventory")
    monkeypatch.setattr(main.boto3, "client", lambda *_, **__: "ecr")
    broken_service = Mock()
    broken_service.run.side_effect = CleanupError("blocked")
    monkeypatch.setattr(main, "CleanupService", lambda *_: broken_service)
    assert main.run(["-region", "r"]) == 2

    with patch("main.build_parser") as parser:
        parser.return_value.parse_args.side_effect = ValueError("bad value")
        assert main.run([]) == 2


def test_regions_to_scan_and_handler(monkeypatch):
    """Legacy all-region mode and Lambda handler retain the CLI behavior."""
    ec2 = Mock()
    ec2.describe_regions.return_value = {"Regions": [{"RegionName": "a"}, {"RegionName": "b"}]}
    monkeypatch.setattr(main.boto3, "client", lambda name: ec2)
    assert main._regions_to_scan("None") == ("a", "b")
    assert main._regions_to_scan("us-east-1") == ("us-east-1",)

    with patch("main.run", return_value=0) as run:
        assert main.handler({}, object()) == 0
    run.assert_called_once_with(())


def test_environment_bool_and_summary_output(monkeypatch, capsys):
    """Environment flags and reported aggregate counts remain deterministic."""
    monkeypatch.setenv("FEATURE", "true")
    assert main._environment_bool("FEATURE")
    monkeypatch.setenv("FEATURE", "yes")
    assert not main._environment_bool("FEATURE")

    main._print_summary("r", (DeletionReport(REPOSITORY, 2, 1, 1, 1),))
    assert "repositories=1" in capsys.readouterr().out
