"""Proxy Auto Config (PAC) file evaluation library."""

from .core import (
    FindProxyForURLResult,
    PACEvaluator,
    Proxies,
    PacSyntaxError,
    PacRuntimeError,
    evaluate_pac,
)

__all__ = [
    "FindProxyForURLResult",
    "PACEvaluator",
    "Proxies",
    "PacSyntaxError",
    "PacRuntimeError",
    "evaluate_pac",
]
