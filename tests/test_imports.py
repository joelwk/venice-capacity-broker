import importlib


def test_imports():
    modules = [
        "libs.telemetry.logger",
        "libs.venice_sdk.client",
        "services.wallet.provider",
        "services.staking.client",
        "services.venice_keys.manager",
        "libs.dex.providers",
        "agents.quorum.core",
        "graph.workflows.revenue_streams",
    ]
    for m in modules:
        importlib.import_module(m)
