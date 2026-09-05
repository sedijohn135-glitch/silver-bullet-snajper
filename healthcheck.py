"""Container health probe.

Used by the Dockerfile's HEALTHCHECK. Exits 0 when healthy, 1 when not.

When ``PORT`` is unset the bot is running as a worker with no HTTP surface, so
there is nothing to probe and the check passes trivially - a worker must not be
restarted just because it is not a web service.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    port = os.environ.get("PORT")
    if not port:
        return 0
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return 0 if response.status == 200 else 1
    except urllib.error.HTTPError as exc:
        # The bot answers 503 while the MCP session is down: unhealthy, not broken.
        print(f"health: HTTP {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - any failure to answer is unhealthy
        print(f"health: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
