"""Monitoring configuration helpers shared by the bot-side service."""

from secrets import compare_digest

from ..core.config_manager import Config


def is_enabled():
    return bool(getattr(Config, "MONITOR_API_ENABLED", False))


def public_token():
    return str(getattr(Config, "MONITOR_API_TOKEN", "") or "").strip()


def internal_token():
    return str(
        getattr(Config, "MONITOR_INTERNAL_SECRET", "") or public_token()
    ).strip()


def token_matches(provided, expected):
    if not provided or not expected:
        return False
    return compare_digest(str(provided), str(expected))


def settings():
    interval = max(1, int(getattr(Config, "STATUS_UPDATE_INTERVAL", 15) or 15))
    configured_max_age = max(0, int(getattr(Config, "MONITOR_SNAPSHOT_MAX_AGE", 30) or 0))
    return {
        "enabled": is_enabled(),
        "public_token": public_token(),
        "internal_token": internal_token(),
        "internal_host": str(
            getattr(Config, "MONITOR_INTERNAL_HOST", "127.0.0.1") or "127.0.0.1"
        ),
        "internal_port": int(getattr(Config, "MONITOR_INTERNAL_PORT", 8765) or 8765),
        # Follow WZML's own status refresh setting; no second independent
        # polling interval can drift out of sync when the setting changes.
        "snapshot_interval": interval,
        # Never declare a normal WZML-aligned snapshot stale before two
        # configured status cycles have elapsed.
        "snapshot_max_age": max(interval * 2, configured_max_age),
        "history_enabled": bool(
            getattr(Config, "MONITOR_HISTORY_ENABLED", True)
        ),
        "history_retention_days": max(
            1, int(getattr(Config, "MONITOR_HISTORY_RETENTION_DAYS", 30) or 30)
        ),
    }
