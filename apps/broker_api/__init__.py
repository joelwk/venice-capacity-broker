"""Compatibility package for tests expecting `apps.broker_api`.

This package dynamically loads the implementation from `apps/broker-api` (hyphen),
so imports like `from apps.broker_api.app import app` work seamlessly.
"""

