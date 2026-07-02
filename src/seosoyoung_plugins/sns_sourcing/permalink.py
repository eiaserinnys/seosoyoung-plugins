"""Slack permalink helpers for SNS sourcing."""

from __future__ import annotations


def build_slack_permalink(workspace_domain: str, channel_id: str, ts: str) -> str:
    """Build a deterministic Slack permalink from channel and timestamp."""
    domain = workspace_domain.strip().removeprefix("https://").rstrip("/")
    return f"https://{domain}/archives/{channel_id}/p{ts.replace('.', '')}"

