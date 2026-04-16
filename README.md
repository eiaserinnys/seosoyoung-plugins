# SeoSoyoung Plugins

Plugin implementations for the [seosoyoung](https://github.com/eiaserinnys/seosoyoung) Slack bot.

This package contains the concrete plugins that extend the bot's capabilities. By separating plugins from the core bot, each plugin can be developed, tested, and deployed independently without modifying the main application.

## Available Plugins

### Memory

Observes Slack conversations and builds a memory of people, topics, and interactions. Uses this memory to inject relevant context into Claude Code sessions, making the bot's responses more contextual and personalized.

- Watches messages across configured channels
- Extracts observations about users (interests, expertise, preferences)
- Promotes recurring observations into long-term memory
- Injects relevant memories as context when responding to mentions

### Channel Observer

Monitors Slack channels and can autonomously intervene in conversations when relevant topics arise. Uses an LLM pipeline to decide when and how to participate.

- Collects messages from configured channels on a schedule
- Runs an analysis pipeline to evaluate whether intervention is appropriate
- Posts messages autonomously when the pipeline decides to intervene
- Configurable cooldown and channel-specific settings

### Trello

Integrates with Trello boards for task management. Watches for card movements and can automatically trigger Claude Code sessions to execute tasks defined on Trello cards.

- Polls Trello boards for card changes
- Detects cards moved to action lists (e.g., "To Go")
- Builds context-rich prompts from card content, checklists, and comments
- Triggers Claude Code sessions via Soulstream to execute the task

### Translate

Automatically translates messages in designated Slack channels. Detects the source language and translates to the target language, preserving Slack formatting and mentions.

- Language detection using morphological analysis (kiwipiepy for Korean)
- Supports custom glossaries for domain-specific terminology
- Preserves Slack-specific syntax (mentions, emoji, links)
- Configurable per-channel with source/target language pairs

## Requirements

- Python 3.11+
- The [seosoyoung](https://github.com/eiaserinnys/seosoyoung) main repository (plugins depend on `seosoyoung.plugin_sdk`)

## Installation

```bash
# As a dependency (production)
pip install git+https://github.com/eiaserinnys/seosoyoung-plugins.git
```

> **Note**: Plugins depend on `seosoyoung.plugin_sdk` from the [main repository](https://github.com/eiaserinnys/seosoyoung). The main repo must be installed in the same environment for imports to work.

## Development

For local development, clone both repositories as siblings:

```bash
git clone https://github.com/eiaserinnys/seosoyoung.git
git clone https://github.com/eiaserinnys/seosoyoung-plugins.git

# Directory layout:
# parent/
# ├── seosoyoung/          ← main bot (provides plugin_sdk)
# └── seosoyoung-plugins/  ← this repo
```

```bash
cd seosoyoung-plugins

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
# (pytest automatically adds ../seosoyoung/src to the Python path)
pytest

# Run tests with coverage
pytest --cov=seosoyoung_plugins --cov-report=html
```

## Writing a Plugin

Plugins implement the `Plugin` base class from `seosoyoung.plugin_sdk`.

```python
from seosoyoung.plugin_sdk import Plugin, PluginMeta, HookContext, HookResult
from seosoyoung.plugin_sdk import slack, soulstream

class MyPlugin(Plugin):
    meta = PluginMeta(
        name="my-plugin",
        version="1.0.0",
        description="A custom plugin"
    )

    async def on_load(self, config: dict) -> None:
        """Called when the plugin is loaded. Initialize resources here."""
        self.api_key = config.get("api_key")

    async def on_unload(self) -> None:
        """Called when the plugin is unloaded. Clean up resources here."""
        pass

    def register_hooks(self) -> dict:
        """Register handlers for lifecycle hooks."""
        return {
            "message_received": self.on_message,
            "before_session": self.inject_context,
        }

    async def on_message(self, ctx: HookContext) -> tuple[HookResult, any]:
        """Handle an incoming message."""
        # Process the message, optionally short-circuit with HookResult.STOP
        return HookResult.CONTINUE, None

    async def inject_context(self, ctx: HookContext) -> tuple[HookResult, any]:
        """Inject context before a Claude Code session starts."""
        ctx.data["extra_prompt"] = "Remember: the user prefers concise answers."
        return HookResult.CONTINUE, None
```

### Plugin Lifecycle

1. **Loading**: `on_load(config)` is called with the plugin's YAML configuration.
2. **Hook Registration**: `register_hooks()` returns a dict mapping hook names to handler methods.
3. **Runtime**: Hooks are called in priority order as events flow through the system.
4. **Unloading**: `on_unload()` is called for cleanup when the bot shuts down.

### Available Hooks

| Hook | Trigger | Typical Use |
|------|---------|-------------|
| `message_received` | Any message in a subscribed channel | Observation, translation, filtering |
| `before_session` | Before a Claude Code session is created | Context injection, prompt modification |
| `after_session` | After a Claude Code session completes | Post-processing, logging |
| `on_startup` | Bot initialization complete | Start background tasks (watchers, schedulers) |
| `on_shutdown` | Bot is shutting down | Stop background tasks, flush state |

### SDK Backends

Plugins access system capabilities through the SDK's backend interfaces:

- **`slack`** — `send_message()`, `add_reaction()`, `get_user_info()`, `get_thread_messages()`
- **`soulstream`** — `run()` (create Claude Code session), `compact()` (compress context)
- **`mention`** — `is_handled()`, `mark()`, `unmark()` (track mention handling state)

## Project Structure

```
src/seosoyoung_plugins/
├── memory/                # Memory plugin (~12 modules)
│   ├── plugin.py          # Plugin entry point
│   ├── observer.py        # Message observation
│   ├── observation_pipeline.py  # Multi-stage observation pipeline
│   ├── store.py           # Memory storage (observations, long-term)
│   ├── reflector.py       # Observation → long-term memory promotion
│   ├── promoter.py        # Memory promotion logic
│   ├── context_builder.py # Build context from memories for sessions
│   ├── intervention.py    # Memory-aware session intervention
│   └── ...                # token_counter, prompts, migration, etc.
├── channel_observer/      # Channel observer plugin (~10 modules)
│   ├── plugin.py          # Plugin entry point
│   ├── observer.py        # Channel monitoring logic
│   ├── collector.py       # Message collection
│   ├── pipeline.py        # LLM analysis pipeline
│   ├── scheduler.py       # Periodic observation scheduling
│   ├── store.py           # Observation state storage
│   ├── intervention.py    # Autonomous message posting
│   └── ...                # pipeline_lock, prompts
├── trello/                # Trello plugin
│   ├── plugin.py          # Plugin entry point
│   ├── watcher.py         # Board polling and change detection
│   ├── client.py          # Trello API client
│   ├── list_runner.py     # Card-to-session execution
│   ├── prompt_builder.py  # Build prompts from card content
│   └── formatting.py      # Output formatting
├── translate/             # Translate plugin
│   ├── plugin.py          # Plugin entry point
│   ├── detector.py        # Language detection
│   ├── translator.py      # Translation via LLM
│   ├── glossary.py        # Custom terminology glossary
│   └── slack_escape.py    # Slack formatting preservation
├── soulstream_client.py   # Shared Soulstream LLM proxy client
└── utils/                 # Shared utilities
    ├── token_counter.py   # Token counting (tiktoken)
    ├── message_formatter.py
    ├── prompt_loader.py
    └── async_runner.py
```

## Configuration

Each plugin has its own YAML configuration file in the main bot's `config/` directory:

```
config/
├── plugins.yaml          # Plugin registry (which plugins to load)
├── memory.yaml           # Memory plugin settings
├── channel_observer.yaml # Channel observer settings
├── trello.yaml           # Trello API credentials and board config
└── translate.yaml        # Translation settings and glossary paths
```

These files are gitignored. Create them manually following the format expected by each plugin's `on_load(config)` method.

## License

[MIT](LICENSE)
