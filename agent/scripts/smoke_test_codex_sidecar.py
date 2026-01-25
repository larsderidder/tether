#!/usr/bin/env python3
"""Smoke test for the Codex SDK Sidecar runner.

Tests:
1. Working directory - create a file, ask model to read it
2. Multi-turn memory - ask to remember a number, then recall it

Requires: Running sidecar at TETHER_CODEX_SIDECAR_URL (default: http://localhost:8788)
"""

import asyncio
import http.client
import importlib
import os
import random
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TETHER_AGENT_DEV_MODE", "1")
os.environ.setdefault("TETHER_AGENT_ADAPTER", "codex_sdk_sidecar")

from tether import store as store_module
from tether.settings import settings
# Note: Runner import delayed until after store reload in run_test()


class Events:
    """Collects runner events."""

    def __init__(self):
        self.outputs = []
        self.errors = []
        self.awaiting_input = False
        self.exited = False

    async def on_output(self, session_id, stream, text, kind=None, is_final=None):
        self.outputs.append(text)
        print(text, end="", flush=True)

    async def on_error(self, session_id, code, message):
        self.errors.append(f"{code}: {message}")
        print(f"\n[ERROR] {code}: {message}")

    async def on_metadata(self, session_id, key, value, raw):
        pass

    async def on_heartbeat(self, session_id, elapsed_s, done):
        pass

    async def on_exit(self, session_id, exit_code):
        self.exited = True

    async def on_awaiting_input(self, session_id):
        self.awaiting_input = True

    async def on_header(self, session_id, title, model, provider, sandbox=None, approval=None):
        print(f"[HEADER] {title} | {model} | {provider} | sandbox={sandbox} | approval={approval}")

    def get_text(self):
        return "".join(self.outputs)

    def reset(self):
        self.outputs = []
        self.errors = []
        self.awaiting_input = False
        self.exited = False


async def wait_for_turn(events, timeout=120):
    """Wait for the current turn to complete."""
    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(0.5)
        elapsed += 0.5
        if events.awaiting_input or events.exited or events.errors:
            return
    raise TimeoutError("Turn timed out")


def check_sidecar_health(url):
    """Check if sidecar is reachable."""
    try:
        parsed = urllib.parse.urlparse(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        if resp.status == 200:
            return True
        print(f"ERROR: Sidecar returned {resp.status}: {data}")
        return False
    except Exception as e:
        print(f"ERROR: Cannot connect to sidecar: {e}")
        return False


async def run_test(tmpdir):
    events = Events()
    secret = random.randint(100, 999)

    # Set data dir BEFORE reloading store
    os.environ["TETHER_AGENT_DATA_DIR"] = tmpdir
    importlib.reload(store_module)

    # Import runner AFTER reloading store so they share the same store instance
    from tether.runner import sidecar as runner_module
    importlib.reload(runner_module)
    runner = runner_module.SidecarRunner(events)

    session = store_module.store.create_session("test", "main")
    session.state = store_module.SessionState.RUNNING
    store_module.store.update_session(session)
    store_module.store.set_workdir(session.id, tmpdir, managed=False)

    # Create test file for working directory verification
    marker = f"SMOKE_TEST_{random.randint(10000, 99999)}"
    test_file = os.path.join(tmpdir, "test_marker.txt")
    with open(test_file, "w") as f:
        f.write(marker)

    # Turn 1: Verify working directory
    print(f"=== Turn 1: Read test_marker.txt (contains {marker}) ===")
    await runner.start(
        session.id,
        "Read the file test_marker.txt and tell me what's inside. Reply with just the content.",
        approval_choice=2
    )
    await wait_for_turn(events)
    print("\n")

    if events.errors:
        return False

    response = events.get_text()
    if marker not in response:
        print(f"FAIL: Working directory test - expected {marker} in response")
        await runner.stop(session.id)
        return False
    print(f"PASS: Working directory correct (found {marker})")

    # Turn 2: Remember number
    events.reset()
    print(f"\n=== Turn 2: Remember {secret} ===")
    await runner.send_input(
        session.id,
        f"Remember this number: {secret}. Reply only with 'OK'.",
    )
    await wait_for_turn(events)
    print("\n")

    if events.errors:
        return False

    # Turn 3: Recall number
    events.reset()
    print("=== Turn 3: What was the number? ===")
    await runner.send_input(session.id, "What number did I ask you to remember? Reply with just the number.")
    await wait_for_turn(events)
    print("\n")

    if events.errors:
        return False

    response = events.get_text()
    if str(secret) in response:
        print(f"PASS: Codex remembered {secret}")
        await runner.stop(session.id)
        return True
    else:
        print(f"FAIL: Expected {secret} in response")
        await runner.stop(session.id)
        return False


def main():
    print("=" * 50)
    print("Codex SDK Sidecar Runner Smoke Test")
    print("=" * 50)
    print()

    sidecar_url = settings.codex_sidecar_url()
    print(f"Sidecar URL: {sidecar_url}")

    if not check_sidecar_health(sidecar_url):
        print("\nTIP: Start the sidecar with: cd codex-sdk-sidecar && npm start")
        sys.exit(1)

    print("Sidecar: OK")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            success = asyncio.run(run_test(tmpdir))
            sys.exit(0 if success else 1)
        except Exception as e:
            print(f"\nFAIL: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)


if __name__ == "__main__":
    main()
