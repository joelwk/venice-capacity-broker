"""
Lightweight package init to avoid eager imports and circular dependencies.

PathQuoteEngine and related types are loaded lazily via __getattr__.
"""

__all__ = ["PathQuoteEngine", "QuoteMode", "QuoteRequest", "QuoteResult"]


def __getattr__(name):
    if name in __all__:
        from . import orchestrator as _orc

        return getattr(_orc, name)
    raise AttributeError(name)
