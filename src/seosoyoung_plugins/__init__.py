"""seosoyoung-plugins: Plugin implementations for seosoyoung slackbot.

This package contains concrete plugin implementations that depend on
seosoyoung's plugin_sdk. Plugins here are registered via plugins.yaml
in the main seosoyoung application.

Available plugins:
- trello: Trello watcher and card management
- translate: Channel message translation (Korean ↔ English)
- channel_observer: Channel observation and intervention
- memory: Memory observation and reflection
"""

__version__ = "0.1.0"

# Re-export plugins for convenient access
from seosoyoung_plugins.trello.plugin import TrelloPlugin
from seosoyoung_plugins.translate.plugin import TranslatePlugin
from seosoyoung_plugins.channel_observer.plugin import ChannelObserverPlugin

__all__ = [
    "TrelloPlugin",
    "TranslatePlugin",
    "ChannelObserverPlugin",
]
