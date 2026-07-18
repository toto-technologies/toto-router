"""Cache implementations for the Phase-1 decision pipeline.

ExactCache: in-memory (+ optional SQLite) exact-match response cache keyed on a
normalized request hash. Tenants are namespaced so they never share hits.
"""

from .exact import ExactCache

__all__ = ["ExactCache"]
