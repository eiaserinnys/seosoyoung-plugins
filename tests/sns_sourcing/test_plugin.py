import pytest

from seosoyoung.plugin_sdk import HookContext, HookResult
from seosoyoung_plugins.sns_sourcing.plugin import SnsSourcingPlugin


BASE_CONFIG = {
    "enabled": True,
    "node_guard": "eias-linegames-wsl",
    "scan_channels": ["C1"],
    "output_channel": "COUT",
    "workspace_domain": "thelinegames.slack.com",
    "state_path": "/tmp/sns_sourcing_test",
    "session": {
        "folder_id": "f8d5d190-001b-489d-82ab-e26d09773322",
        "agent_id": "seosoyoung-opus",
    },
}


def test_plugin_meta_version_is_r5():
    assert SnsSourcingPlugin.meta.version == "1.1.0"


@pytest.mark.asyncio
async def test_node_guard_uses_preferred_node_fallback(monkeypatch):
    monkeypatch.delenv("SOULSTREAM_NODE_ID", raising=False)
    monkeypatch.setenv("SOULSTREAM_PREFERRED_NODE", "eias-linegames-wsl")
    plugin = SnsSourcingPlugin()

    await plugin.on_load(BASE_CONFIG)

    assert plugin._active is True


@pytest.mark.asyncio
async def test_node_guard_fail_closed_when_env_missing(monkeypatch):
    monkeypatch.delenv("SOULSTREAM_NODE_ID", raising=False)
    monkeypatch.delenv("SOULSTREAM_PREFERRED_NODE", raising=False)
    plugin = SnsSourcingPlugin()

    await plugin.on_load(BASE_CONFIG)
    result, value = await plugin.register_hooks()["on_startup"](
        HookContext("on_startup", args={})
    )

    assert plugin._active is False
    assert result == HookResult.CONTINUE
    assert value == {"sns_sourcing_active": False}


@pytest.mark.asyncio
async def test_node_guard_fail_closed_on_mismatch(monkeypatch):
    monkeypatch.setenv("SOULSTREAM_NODE_ID", "other-node")
    monkeypatch.setenv("SOULSTREAM_PREFERRED_NODE", "eias-linegames-wsl")
    plugin = SnsSourcingPlugin()

    await plugin.on_load(BASE_CONFIG)

    assert plugin._active is False
