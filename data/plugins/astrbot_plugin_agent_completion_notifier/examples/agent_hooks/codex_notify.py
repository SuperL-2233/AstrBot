#!/usr/bin/env python3
"""Adapt Codex's external notifier payload to the AstrBot webhook protocol."""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        return 0
    event = json.loads(sys.argv[1])
    if event.get("type") != "agent-turn-complete":
        return 0

    url = os.environ.get("ASTRBOT_AGENT_WEBHOOK_URL", "").strip()
    token = os.environ.get("ASTRBOT_AGENT_WEBHOOK_TOKEN", "").strip()
    if not url or not token:
        print(
            "Codex QQ notifier: webhook URL or token is not configured", file=sys.stderr
        )
        return 0

    session_id = str(event.get("thread-id") or event.get("session_id") or "unknown")
    turn_id = str(event.get("turn-id") or event.get("turn_id") or "unknown")
    cwd = str(event.get("cwd") or "")
    payload = {
        "source": "codex",
        "event": "agent-turn-complete",
        "event_id": f"codex:{session_id}:{turn_id}",
        "session_id": session_id,
        "project": Path(cwd).name if cwd else "",
        "host": os.environ.get(
            "ASTRBOT_AGENT_NOTIFY_NAME", f"{socket.gethostname()}-Codex"
        ),
        "message": str(event.get("last-assistant-message") or ""),
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Codex QQ notifier failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
