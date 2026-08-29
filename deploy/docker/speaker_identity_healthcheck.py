#!/usr/bin/env python3
"""Authenticated loopback health probe for the optional identity worker."""

from __future__ import annotations

import json
import os
import urllib.request


def main() -> int:
    """Return success only when the authenticated worker reports healthy."""
    token = os.environ.get("HA_CODEX_SPEAKER_IDENTITY_TOKEN", "")
    port = int(os.environ.get("HA_CODEX_SPEAKER_IDENTITY_PORT", "8790"))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            value = json.load(response)
    except OSError:
        return 1
    except ValueError:
        return 1
    return 0 if value.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
