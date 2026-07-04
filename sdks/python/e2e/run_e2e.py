"""Python SDK end-to-end proof driver (W4-04).

Spawns crasher.py, which inits the real SDK and raises an uncaught exception,
then asserts through the Crashlens issues API that the crash arrived and was
grouped into an issue with platform ``python``. Uses only the standard library
so it needs nothing beyond the installed SDK (``pip install -e sdks/python``).

Environment (set by the CI e2e job):
  CRASHLENS_DSN        - DSN the crasher inits with.
  CRASHLENS_API_BASE   - e.g. http://localhost/api
  CRASHLENS_TOKEN      - a session token for the account that owns the project.
  CRASHLENS_ORG_ID     - the owning org id.
  CRASHLENS_PROJECT_ID - the project the DSN key belongs to.

Exit code 0 on success, 1 on any failure or timeout.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from crasher import MARKER

EXPECTED_PLATFORM = "python"
POLL_TIMEOUT_S = 60


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - trusted local URL
        return json.loads(resp.read().decode("utf-8"))


def _find_issue(api_base: str, org: str, project: str, token: str) -> dict | None:
    """Return the issue whose title carries the crasher marker, or None yet."""
    listing = _get_json(
        f"{api_base}/orgs/{org}/projects/{project}/issues", token
    )
    for item in listing.get("issues", []):
        if MARKER in item.get("title", ""):
            return item
    return None


def main() -> int:
    dsn = os.environ["CRASHLENS_DSN"]
    api_base = os.environ["CRASHLENS_API_BASE"].rstrip("/")
    token = os.environ["CRASHLENS_TOKEN"]
    org = os.environ["CRASHLENS_ORG_ID"]
    project = os.environ["CRASHLENS_PROJECT_ID"]

    here = os.path.dirname(os.path.abspath(__file__))
    crasher = os.path.join(here, "crasher.py")

    # Run the crasher. A non-zero exit is EXPECTED (uncaught exception); we only
    # need it to have flushed the event before exiting.
    print("running the crasher subprocess (an uncaught exception is expected)...")
    proc = subprocess.run(  # noqa: S603 - fixed argv, trusted script
        [sys.executable, crasher],
        env={**os.environ, "CRASHLENS_DSN": dsn},
        capture_output=True,
        text=True,
        timeout=30,
    )
    print(f"crasher exited with code {proc.returncode}")

    deadline = time.time() + POLL_TIMEOUT_S
    issue = None
    while time.time() < deadline:
        try:
            issue = _find_issue(api_base, org, project, token)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"  poll error (retrying): {exc}")
        if issue is not None:
            break
        time.sleep(1)

    if issue is None:
        print("FAIL: no issue carrying the crasher marker appeared within the timeout")
        return 1

    detail = _get_json(
        f"{api_base}/orgs/{org}/projects/{project}/issues/{issue['id']}", token
    )
    platform = (detail.get("latest_event") or {}).get("payload", {}).get("platform")
    if platform != EXPECTED_PLATFORM:
        print(f"FAIL: issue platform was {platform!r}, expected {EXPECTED_PLATFORM!r}")
        return 1

    print(
        f"PASS: uncaught {issue['title']!r} arrived and grouped with "
        f"platform={platform!r} (issue {issue['id']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
