"""Domain exceptions for the ECR cleanup application."""


class CleanupError(Exception):
    """Base exception for an expected cleanup failure."""


class ConfigurationError(CleanupError):
    """Raised when command-line or target configuration is invalid."""


class ProtectionInventoryError(CleanupError):
    """Raised when active Kubernetes image protection cannot be established."""


class DeletionError(CleanupError):
    """Raised when ECR rejects one or more requested image deletions."""
