"""Authenticated, content-free Docker health probe for the bridge."""

from __future__ import annotations

import http.client
import json
import os


def main() -> int:
    """Return success only when App Server is running with ChatGPT auth."""
    token = os.environ.get("HA_CODEX_BRIDGE_TOKEN", "")
    port_text = os.environ.get("HA_CODEX_BRIDGE_PORT", "8787")
    if not token:
        return 1
    try:
        port = int(port_text)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request(
            "GET",
            "/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        response = connection.getresponse()
        body = response.read(64 * 1024)
        connection.close()
        if response.status != 200:
            return 1
        payload = json.loads(body)
        app_server = payload.get("app_server", {})
        if not isinstance(app_server, dict):
            return 1
        return int(
            payload.get("status") != "ok"
            or app_server.get("running") is not True
            or app_server.get("auth_mode") != "chatgpt"
        )
    except OSError:
        return 1
    except ValueError:
        return 1
    except http.client.HTTPException:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
