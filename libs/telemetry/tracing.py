from __future__ import annotations

from typing import Any, Dict


def annotate_span(attrs: Dict[str, Any], name: str = "attrs") -> None:
    """Attach attributes to the current trace span (LangSmith child span).

    Best-effort: if LangSmith is enabled and available, creates a short-lived
    child span with provided metadata so attributes are visible in traces.
    No-op on failure.
    """
    try:
        from langsmith import trace  # type: ignore

        # Ensure values are JSON-serializable
        safe_attrs: Dict[str, Any] = {}
        for k, v in attrs.items():
            try:
                # Basic safety conversion
                if isinstance(v, (str, int, float, bool)) or v is None:
                    safe_attrs[k] = v
                else:
                    safe_attrs[k] = str(v)
            except Exception:
                safe_attrs[k] = str(v)
        # Create a child span containing the attributes
        with trace(name=name, metadata=safe_attrs):
            pass
    except Exception:
        # Swallow any tracing errors silently
        return

