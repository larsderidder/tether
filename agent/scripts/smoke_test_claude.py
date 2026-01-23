#!/usr/bin/env python3
"""Smoke test for the Claude runner.

Usage:
    # Dry run (no API calls)
    python scripts/smoke_test_claude.py

    # With real API call (requires ANTHROPIC_API_KEY)
    python scripts/smoke_test_claude.py --live

    # Custom prompt
    python scripts/smoke_test_claude.py --live --prompt "What is 2+2?"

    # Multi-turn conversation test
    python scripts/smoke_test_claude.py --live --multi-turn
"""

import argparse
import asyncio
import os
import sys
import tempfile

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TETHER_AGENT_DEV_MODE", "1")
os.environ.setdefault("TETHER_AGENT_ADAPTER", "claude")


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
        """Reset state for next conversation turn."""
        self.outputs = []
        self.errors = []
        self.metadata = []
        self.awaiting_input = False
        self.exit_code = None


def test_instantiation():
    """Test that ClaudeRunner can be instantiated."""
    print("\n1. Testing instantiation...")
    try:
        from tether.runner.claude import ClaudeRunner
        events = MockEvents()
        runner = ClaudeRunner(events)
        print(f"   Runner type: {runner.runner_type}")
        print(f"   Model: {runner._model}")
        print(f"   Max tokens: {runner._max_tokens}")
        print("   OK: Runner instantiated successfully")
        return runner, events
    except Exception as e:
        print(f"   FAIL: {e}")
        return None, None


def test_api_key():
    """Check if API key is configured."""
    print("\n2. Checking API key...")
    from tether.settings import settings
    key = settings.anthropic_api_key()
    if key:
        print(f"   OK: API key configured ({key[:10]}...)")
        return True
    else:
        print("   SKIP: No ANTHROPIC_API_KEY set")
        return False


async def test_live_call(runner, events, prompt):
    """Make a real API call to Claude."""
    print(f"\n3. Testing live API call with prompt: '{prompt}'")

    # Set up temp data dir for store
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["TETHER_AGENT_DATA_DIR"] = tmpdir

        # Re-import store to pick up new data dir
        from tether import store as store_module
        import importlib
        importlib.reload(store_module)
        from tether.store import store

        # Create a test session
        session = store.create_session("smoke_test", "main")
        session.state = store_module.SessionState.RUNNING
        store.update_session(session)
        store.set_workdir(session.id, tmpdir, managed=False)

        print(f"   Session: {session.id}")
        print("   Calling Claude API...")

        try:
            await runner.start(session.id, prompt, approval_choice=0)

            # Wait for completion (with timeout)
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
            print(f"   OK: Token usage recorded: {len(events.metadata)} metadata events")

            # Stop the runner
            await runner.stop(session.id)
            return True

        except Exception as e:
            print(f"   FAIL: {e}")
            import traceback
            traceback.print_exc()
            return False


async def test_multi_turn(runner, events):
    """Test multi-turn conversation memory."""
    import random
    secret_number = random.randint(100, 999)

    print(f"\n4. Testing multi-turn conversation (secret number: {secret_number})")

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["TETHER_AGENT_DATA_DIR"] = tmpdir

        from tether import store as store_module
        import importlib
        importlib.reload(store_module)
        from tether.store import store

        session = store.create_session("multi_turn_test", "main")
        session.state = store_module.SessionState.RUNNING
        store.update_session(session)
        store.set_workdir(session.id, tmpdir, managed=False)

        print(f"   Session: {session.id}")

        # Turn 1: Ask Claude to remember a number
        turn1_prompt = f"Remember this number: {secret_number}. Just reply with 'OK, I'll remember {secret_number}' and nothing else."
        print(f"\n   Turn 1: '{turn1_prompt}'")

        try:
            await runner.start(session.id, turn1_prompt, approval_choice=0)

            timeout = 60
            elapsed = 0
            while elapsed < timeout:
                await asyncio.sleep(0.5)
                elapsed += 0.5
                if events.awaiting_input:
                    break

            if events.errors:
                print(f"   FAIL: Turn 1 got errors: {events.errors}")
                return False

            turn1_response = events.get_all_text()
            print(f"   Response: {turn1_response[:200]}...")
            print(f"   OK: Turn 1 complete")

            # Reset for turn 2
            events.reset_for_turn()

            # Turn 2: Ask what the number was
            turn2_prompt = "What number did I ask you to remember? Reply with just the number."
            print(f"\n   Turn 2: '{turn2_prompt}'")

            await runner.send_input(session.id, turn2_prompt)

            elapsed = 0
            while elapsed < timeout:
                await asyncio.sleep(0.5)
                elapsed += 0.5
                if events.awaiting_input:
                    break

            if events.errors:
                print(f"   FAIL: Turn 2 got errors: {events.errors}")
                return False

            turn2_response = events.get_all_text()
            print(f"   Response: {turn2_response[:200]}")

            # Check if the number is in the response
            if str(secret_number) in turn2_response:
                print(f"   OK: Claude remembered the number {secret_number}!")
                await runner.stop(session.id)
                return True
            else:
                print(f"   FAIL: Response doesn't contain {secret_number}")
                await runner.stop(session.id)
                return False

        except Exception as e:
            print(f"   FAIL: {e}")
            import traceback
            traceback.print_exc()
            return False


def main():
    parser = argparse.ArgumentParser(description="Smoke test for Claude runner")
    parser.add_argument("--live", action="store_true", help="Make real API calls")
    parser.add_argument("--multi-turn", action="store_true", help="Test multi-turn conversation")
    parser.add_argument("--prompt", default="Say 'Hello from smoke test!' and nothing else.",
                        help="Prompt to send (only with --live)")
    args = parser.parse_args()

    print("=" * 60)
    print("Claude Runner Smoke Test")
    print("=" * 60)

    # Test 1: Instantiation
    runner, events = test_instantiation()
    if not runner:
        sys.exit(1)

    # Test 2: API key check
    has_key = test_api_key()

    # Test 3: Live call (optional)
    if args.live or args.multi_turn:
        if not has_key:
            print("\n   ERROR: --live/--multi-turn requires ANTHROPIC_API_KEY")
            sys.exit(1)

        if args.multi_turn:
            # Run multi-turn test instead of single prompt
            success = asyncio.run(test_multi_turn(runner, events))
        else:
            success = asyncio.run(test_live_call(runner, events, args.prompt))

        if not success:
            sys.exit(1)
    else:
        print("\n3. Skipping live API call (use --live to enable)")
        print("\n4. Skipping multi-turn test (use --multi-turn to enable)")

    print("\n" + "=" * 60)
    print("Smoke test passed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
