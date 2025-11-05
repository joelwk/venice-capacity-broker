import os

# Disable console capture during tests to avoid conflicts with pytest's capture mechanism
os.environ.setdefault('LOG_CAPTURE_CONSOLE', '0')

os.environ.setdefault('BROKER_REQUIRE_ADMIN_TOKEN', 'false')
os.environ.setdefault('BROKER_ADMIN_TOKEN', 'test-admin')
os.environ.setdefault('VENICE_PARENT_KEY', 'parent-test')
