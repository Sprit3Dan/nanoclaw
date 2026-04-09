"""Chat channels package.

This module intentionally avoids eager imports so lightweight test runs
don't require all runtime channel dependencies at import time.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["BaseChannel", "ChannelManager"]


def __getattr__(name: str) -> Any:
    if name == "BaseChannel":
        return import_module("nanobot.channels.base").BaseChannel
    if name == "ChannelManager":
        return import_module("nanobot.channels.manager").ChannelManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")