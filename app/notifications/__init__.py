"""Notification senders. Currently Slack only; designed to be extended."""

from app.notifications.slack import SlackNotifier, get_slack_notifier

__all__ = ["SlackNotifier", "get_slack_notifier"]
