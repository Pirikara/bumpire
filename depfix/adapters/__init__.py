"""Package-manager adapters."""

from depfix.adapters.base import DependencyAdapter
from depfix.adapters.factory import create_adapter

__all__ = ["DependencyAdapter", "create_adapter"]
