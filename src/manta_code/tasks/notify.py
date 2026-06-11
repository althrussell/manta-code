"""Best-effort desktop notification when a background task finishes (ADR 0012).

Agents that report back feel like staff; agents you must poll feel like cron.
The runner fires one notification at each task's terminal state — macOS via
``osascript``, Linux via ``notify-send`` — and silently does nothing when
neither exists or ``MANTA_NOTIFY=0``. Never raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _enabled() -> bool:
    return os.environ.get("MANTA_NOTIFY", "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def notify(title: str, message: str) -> bool:
    """Send a desktop notification; returns whether one was attempted."""
    if not _enabled():
        return False
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = (
                f'display notification "{_escape(message)}" '
                f'with title "{_escape(title)}"'
            )
            subprocess.run(  # noqa: S603 - fixed binary, escaped args
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                timeout=10,
            )
            return True
        if shutil.which("notify-send"):
            subprocess.run(  # noqa: S603
                ["notify-send", title, message],
                check=False,
                capture_output=True,
                timeout=10,
            )
            return True
    except Exception:  # noqa: BLE001 - notifications are best-effort
        pass
    return False


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify_task_finished(task_id: str, agent: str, state: str) -> bool:
    """Notification for a task reaching a terminal state."""
    icon = {"done": "✅", "failed": "❌", "cancelled": "🛑"}.get(state, "ℹ️")
    return notify(
        f"Manta task {state}",
        f"{icon} @{agent} task {task_id} — collect with: manta task output {task_id}",
    )
