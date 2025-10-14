import os

os.environ.setdefault('BROKER_REQUIRE_ADMIN_TOKEN', 'false')
os.environ.setdefault('BROKER_ADMIN_TOKEN', 'test-admin')
os.environ.setdefault('VENICE_PARENT_KEY', 'parent-test')
