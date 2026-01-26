#!/usr/bin/env python3
"""Smoke test for the Claude runner (Anthropic API).

Tests:
1. Working directory - create a file, ask model to read it
2. Multi-turn memory - ask to remember a number, then recall it

Requires: ANTHROPIC_API_KEY environment variable
"""

import asyncio
import importlib
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TETHER_AGENT_DEV_MODE", "1")
os.environ.setdefault("TETHER_AGENT_ADAPTER", "claude_api")

from tether import store as store_module
from tether.runner.claude_api import ClaudeRunner
from tether.settings import settings
from tether.store import store


class Events:
    """Collects runner events."""

    def __init__(self):
        self.outputs = []
        self.errors = []
        self.awaiting_input = False

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
        pass

    async def on_awaiting_input(self, session_id):
        self.awaiting_input = True

    def get_text(self):
        return "".join(self.outputs)

    def reset(self):
        self.outputs = []
        self.errors = []
        self.awaiting_input = False


async def wait_for_turn(events, timeout=60):
    """Wait for the current turn to complete."""
    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(0.5)
        elapsed += 0.5
        if events.awaiting_input or events.errors:
            return
    raise TimeoutError("Turn timed out")


async def run_test(tmpdir):
    events = Events()
    runner = ClaudeRunner(events)
    secret = random.randint(100, 999)

    os.environ["TETHER_AGENT_DATA_DIR"] = tmpdir
    importlib.reload(store_module)

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
        print(f"PASS: Claude remembered {secret}")
        await runner.stop(session.id)
        return True
    else:
        print(f"FAIL: Expected {secret} in response")
        await runner.stop(session.id)
        return False


def main():
    print("=" * 50)
    print("Claude Runner Smoke Test (Anthropic API)")
    print("=" * 50)
    print()

    if not settings.anthropic_api_key():
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    print(f"Model: {settings.claude_model()}")
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
