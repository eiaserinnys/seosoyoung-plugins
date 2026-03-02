# seosoyoung-plugins

Plugin implementations for [seosoyoung](https://github.com/eiaserinnys/seosoyoung) slackbot.

## Overview

This repository contains concrete plugin implementations that depend on seosoyoung's `plugin_sdk`. By separating plugins from the main application:

- The main slackbot doesn't need to know plugin implementation details
- Plugins can be developed and deployed independently
- Dependencies are cleanly separated

## Installation

```bash
# For development
pip install -e ".[dev]"

# For production (installed as a dependency)
pip install git+https://github.com/eiaserinnys/seosoyoung-plugins.git
```

## Development

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=seosoyoung_plugins --cov-report=html
```

## Plugin Structure

Plugins must:
1. Subclass `Plugin` from `seosoyoung.plugin_sdk`
2. Define a `meta` attribute with `PluginMeta`
3. Implement `on_load()` and `on_unload()` methods
4. Optionally implement `register_hooks()` to participate in hook chains

Example:

```python
from seosoyoung.plugin_sdk import Plugin, PluginMeta, HookContext, HookResult

class MyPlugin(Plugin):
    meta = PluginMeta(
        name="my-plugin",
        version="1.0.0",
        description="A sample plugin"
    )

    async def on_load(self, config: dict) -> None:
        # Initialize plugin resources
        pass

    async def on_unload(self) -> None:
        # Cleanup plugin resources
        pass

    def register_hooks(self) -> dict:
        return {
            "message_received": self.handle_message,
        }

    async def handle_message(self, ctx: HookContext) -> tuple[HookResult, any]:
        # Handle the hook
        return HookResult.CONTINUE, None
```

## License

MIT
