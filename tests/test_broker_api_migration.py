"""
Test that the new unified broker_api module loads and provides expected endpoints.
"""
import pytest


def test_app_imports():
    """Test that the app can be imported successfully."""
    from apps.broker_api.app import app, create_app
    
    assert app is not None
    assert callable(create_app)
    assert app.title == "Venice Capacity Broker API"


def test_app_has_routes():
    """Test that the app has routes from wired routers."""
    from apps.broker_api.app import app
    
    routes = [route.path for route in app.routes]
    
    # Check for key endpoints
    assert "/" in routes or any(r.startswith("/") for r in routes)
    # API probe
    assert "/api" in routes
    # At least one tenant endpoint should exist
    assert any("/v1/tenants" in r for r in routes)


def test_openapi_schema():
    """Test that OpenAPI schema is generated correctly."""
    from apps.broker_api.app import app
    
    schema = app.openapi()
    assert schema is not None
    assert "openapi" in schema
    assert "info" in schema
    assert schema["info"]["title"] == "Venice Capacity Broker API"
    assert "paths" in schema


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

