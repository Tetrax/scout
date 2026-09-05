from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen


def main() -> int:
    expected_revision = os.environ.get("SCOUT_REVISION", "")
    request = Request(
        "http://127.0.0.1:8080/healthz",
        headers={"User-Agent": "Scout-Healthcheck/1.0"},
        method="GET",
    )
    # The request URL is a compile-time loopback constant, never user input.
    with urlopen(request, timeout=3) as response:  # nosec B310
        if response.status != 200:
            return 1
        body = response.read(4097)
    if len(body) > 4096:
        return 1
    payload = json.loads(body.decode("utf-8"))
    if payload.get("status") != "ok":
        return 1
    if expected_revision and payload.get("revision") != expected_revision:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
