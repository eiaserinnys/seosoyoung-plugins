"""Basic tests to verify package structure."""


def test_package_import():
    """Test that the package can be imported."""
    import seosoyoung_plugins

    assert seosoyoung_plugins.__version__ == "0.1.0"


def test_sdk_import():
    """Test that plugin_sdk can be imported from seosoyoung."""
    from seosoyoung.plugin_sdk import (
        HookContext,
        HookPriority,
        HookResult,
        Plugin,
        PluginMeta,
    )

    # Verify exports are available
    assert Plugin is not None
    assert PluginMeta is not None
    assert HookContext is not None
    assert HookResult is not None
    assert HookPriority is not None
