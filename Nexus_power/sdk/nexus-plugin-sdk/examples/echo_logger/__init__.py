"""Echo Logger — reference plugin built with nexus-plugin-sdk.

A minimal but production-shaped plugin that demonstrates the four
mechanisms a plugin author cares about:

  * ``@on_event``  — subscribe to a platform event
  * ``@action``    — implement an outbound action
  * ``@scheduled`` — run on a cron schedule
  * Test harness   — drive the plugin without a real event bus

The plugin appends a JSON line per inbound echo to a configurable log
file, and exposes a ``log_echo`` action that does the same on demand.
The ``periodic_flush`` schedule no-ops in this reference but shows
the decorator shape.
"""

from __future__ import annotations

from .plugin import EchoLoggerPlugin

__all__ = ["EchoLoggerPlugin"]
