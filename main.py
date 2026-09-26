"""Command-line ECR cleanup with Kubernetes active-image protection."""
import argparse
import os
from collections.abc import Sequence
from typing import Any, Optional

import boto3

from ecr_cleanup.cleanup import CleanupService
from ecr_cleanup.config import load_kubernetes_targets
from ecr_cleanup.errors import CleanupError, ConfigurationError
from ecr_cleanup.kubernetes import KubernetesInventoryCollector


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser for the Jenkins cleanup job.

    Returns:
        Parser containing legacy retention options and Kubernetes protection
        options.
    """
    parser = argparse.ArgumentParser(description="Safely deletes stale ECR images")
    parser.add_argument(
        "-dryrun",
        default=os.environ.get("DRYRUN", "true"),
        help="Only the literal value false permits deletion; defaults to true.",
    )
    parser.add_argument(
        "-imagestokeep",
        type=int,
        default=int(os.environ.get("IMAGES_TO_KEEP", "100")),
        help="Number of newest matching master images to retain.",
    )
    parser.add_argument(
        "-region",
        default=os.environ.get("REGION", "None"),
        help="ECR region; None scans all enabled account regions.",
    )
    parser.add_argument(
        "-ignoretagsregex",
        default=os.environ.get("IGNORE_TAGS_REGEX", "^$"),
        help="Regex of tag names excluded from retention selection.",
    )
    parser.add_argument(
        "--repository-name-contains",
        default=os.environ.get("REPOSITORY_NAME_CONTAINS", "-service"),
        help="Only process repository names containing this value (default: -service).",
    )
    parser.add_argument(
        "--k8s-targets-file",
        default=os.environ.get("K8S_TARGETS_FILE"),
        help="Path to non-secret Kubernetes target JSON.",
    )
    parser.add_argument(
        "--protect-active-pod-images",
        action="store_true",
        default=_environment_bool("PROTECT_ACTIVE_POD_IMAGES"),
        help="Protect ECR digests used by active Kubernetes Pods.",
    )
    return parser


def run(arguments: Optional[Sequence[str]] = None) -> int:
    """Run cleanup from command-line arguments.

    Args:
        arguments: Optional explicit arguments, excluding the executable name.

    Returns:
        Process-compatible exit code.
    """
    try:
        parsed = build_parser().parse_args(arguments)
        dry_run = _is_dry_run(parsed.dryrun)
        targets = load_kubernetes_targets(parsed.k8s_targets_file)
        if parsed.protect_active_pod_images and not targets:
            raise ConfigurationError(
                "--protect-active-pod-images requires --k8s-targets-file"
            )
        if not dry_run and not parsed.protect_active_pod_images:
            raise ConfigurationError(
                "Non-dry-run cleanup requires --protect-active-pod-images"
            )
        if not dry_run and not targets:
            raise ConfigurationError(
                "Non-dry-run cleanup requires --k8s-targets-file"
            )
        inventory = KubernetesInventoryCollector()
        for region in _regions_to_scan(parsed.region):
            ecr_client = boto3.client("ecr", region_name=region)
            service = CleanupService(ecr_client, inventory)
            reports = service.run(
                repository_name_contains=parsed.repository_name_contains,
                images_to_keep=parsed.imagestokeep,
                ignore_tags_regex=parsed.ignoretagsregex,
                dry_run=dry_run,
                protect_active_pod_images=parsed.protect_active_pod_images,
                targets=targets,
            )
            _print_summary(region, reports)
    except CleanupError as error:
        print("Cleanup aborted safely: {}".format(error))
        return 2
    except ValueError as error:
        print("Invalid cleanup configuration: {}".format(error))
        return 2
    return 0


def handler(event: Any, context: Any) -> int:
    """Provide the legacy Lambda handler entry point.

    The Jenkins CLI is the supported operational entry point. Lambda invocation
    uses the same environment-backed defaults and remains dry-run by default.

    Args:
        event: Unused Lambda event payload.
        context: Unused Lambda execution context.

    Returns:
        Process-compatible cleanup exit code.
    """
    del event, context
    return run(())


def _regions_to_scan(configured_region: str) -> tuple[str, ...]:
    """Return the configured region or all enabled regions for legacy mode."""
    if configured_region != "None":
        return (configured_region,)
    ec2_client = boto3.client("ec2")
    return tuple(region["RegionName"] for region in ec2_client.describe_regions()["Regions"])


def _is_dry_run(value: str) -> bool:
    """Preserve legacy behavior: only the literal false enables deletion."""
    return value.lower() != "false"


def _environment_bool(name: str) -> bool:
    """Read a conventional true environment value without accepting ambiguity."""
    return os.environ.get(name, "false").lower() == "true"


def _print_summary(region: str, reports: Sequence[Any]) -> None:
    """Print a concise region-level cleanup summary."""
    print(
        "Region {} complete: repositories={}, candidates={}, protected={}, deleted={}".format(
            region,
            len(reports),
            sum(report.candidates for report in reports),
            sum(report.protected for report in reports),
            sum(report.deleted for report in reports),
        )
    )


if __name__ == "__main__":
    raise SystemExit(run())
