"""Directory-plugin entry point; no side effects at import."""
from .adapter import register

__all__ = ["register"]
