#!/usr/bin/env python3
"""Smoke test for the Claude Local runner (Agent SDK with OAuth).

Usage:
    # Dry run (no API calls)
    python scripts/smoke_test_claude_local.py

    # With real API call (requires Claude CLI OAuth setup)
    python scripts/smoke_test_claude_local.py --live
"""

import argparse
import asyncio
import os
import sys
import tempfile

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TETHER_AGENT_DEV_MODE", "1")
os.environ.setdefault("TETHER_AGENT_ADAPTER", "claude_local")


class MockEvents:
    """Collects runner events for verification."""

    def __init__(self):
        self.outputs = []
        self.errors = []
        self.metadata = []
        self.heartbeats = []
        self.exit_code = None
        self.awaiting_input = False

    async def on_output(self, session_id, stream, text, kind=None, is_final=None):
        self.outputs.append({"stream": stream, "text": text, "kind": kind})
        print(f"  [output:{kind}] {text[:100]}..." if len(text) > 100 else f"  [output:{kind}] {text}")

    async def on_error(self, session_id, code, message):
        self.errors.append({"code": code, "message": message})
        print(f"  [error] {code}: {message}")

    async def on_metadata(self, session_id, key, value, raw):
        self.metadata.append({"key": key, "value": value})
        print(f"  [metadata] {key}: {raw}")

    async def on_heartbeat(self, session_id, elapsed_s, done):
        self.heartbeats.append({"elapsed": elapsed_s, "done": done})
        if done:
            print(f"  [heartbeat] done after {elapsed_s:.1f}s")

    async def on_exit(self, session_id, exit_code):
        self.exit_code = exit_code
        print(f"  [exit] code={exit_code}")

    async def on_awaiting_input(self, session_id):
        self.awaiting_input = True
        print("  [awaiting_input]")

    def get_all_text(self):
        """Get all output text concatenated."""
        return "".join(o["text"] for o in self.outputs if o.get("text"))

    def reset_for_turn(self):
        """Reset state for next turn while keeping SDK session."""
        self.outputs = []
        self.errors = []
        self.awaiting_input = False
        self.exit_code = None


def test_sdk_available():
    """Test that claude_agent_sdk is installed."""
    print("\n1. Testing SDK availability...")
    try:
        import claude_agent_sdk
        print(f"   OK: claude_agent_sdk is installed")
        return True
    except ImportError as e:
        print(f"   SKIP: claude_agent_sdk not installed ({e})")
        return False


def test_instantiation():
    """Test that ClaudeLocalRunner can be instantiated."""
    print("\n2. Testing instantiation...")
    try:
        from tether.runner.claude_local import ClaudeLocalRunner
        events = MockEvents()
        runner = ClaudeLocalRunner(events)
        print(f"   Runner type: {runner.runner_type}")
        print("   OK: Runner instantiated successfully")
        return runner, events
    except Exception as e:
        print(f"   FAIL: {e}")
        return None, None


def test_oauth_credentials():
    """Check if OAuth credentials exist."""
    print("\n3. Checking OAuth credentials...")
    creds_path = os.path.expanduser("~/.claude/.credentials.json")
    if os.path.exists(creds_path):
        print(f"   OK: Credentials file exists at {creds_path}")
        return True
    else:
        print(f"   SKIP: No credentials at {creds_path}")
        print("   TIP: Run 'claude' CLI and authenticate first")
        return False


async def test_live_call(runner, events, prompt):
    """Make a real API call via Agent SDK."""
    print(f"\n4. Testing live API call with prompt: '{prompt}'")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["TETHER_AGENT_DATA_DIR"] = tmpdir

        from tether import store as store_module
        import importlib
        importlib.reload(store_module)
        from tether.store import store

        session = store.create_session("smoke_test", "main")
        session.state = store_module.SessionState.RUNNING
        session.directory = tmpdir
        store.update_session(session)
        store.set_workdir(session.id, tmpdir, managed=False)

        print(f"   Session: {session.id}")
        print("   Calling Agent SDK...")

        try:
            await runner.start(session.id, prompt, approval_choice=0)

            timeout = 60
            elapsed = 0
            while elapsed < timeout:
                await asyncio.sleep(0.5)
                elapsed += 0.5
                if events.exit_code is not None or events.awaiting_input:
                    break

            if events.errors:
                print(f"   FAIL: Got errors: {events.errors}")
                return False

            if not events.outputs:
                print("   FAIL: No outputs received")
                return False

            print(f"   OK: Received {len(events.outputs)} output chunks")
            await runner.stop(session.id)
            return True

        except Exception as e:
            print(f"   FAIL: {e}")
            import traceback
            traceback.print_exc()
            return False


def main():
    parser = argparse.ArgumentParser(description="Smoke test for Claude Local runner")
    parser.add_argument("--live", action="store_true", help="Make real API calls")
    parser.add_argument("--prompt", default="Say 'Hello from Claude Local!' and nothing else.",
                        help="Prompt to send (only with --live)")
    args = parser.parse_args()

    print("=" * 60)
    print("Claude Local Runner Smoke Test")
    print("=" * 60)

    # Test 1: SDK available
    sdk_ok = test_sdk_available()
    if not sdk_ok:
        print("\n" + "=" * 60)
        print("Smoke test skipped (SDK not installed)")
        print("=" * 60)
        sys.exit(0)

    # Test 2: Instantiation
    runner, events = test_instantiation()
    if not runner:
        sys.exit(1)

    # Test 3: OAuth credentials
    has_creds = test_oauth_credentials()

    # Test 4: Live call (optional)
    if args.live:
        if not has_creds:
            print("\n   ERROR: --live requires OAuth credentials")
            sys.exit(1)
        success = asyncio.run(test_live_call(runner, events, args.prompt))
        if not success:
            sys.exit(1)
    else:
        print("\n4. Skipping live API call (use --live to enable)")

    print("\n" + "=" * 60)
    print("Smoke test passed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
