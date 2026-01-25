#!/usr/bin/env python3
"""Smoke test for permission bypass in Claude Local runner.

Tests that approval_choice=2 (bypassPermissions) actually works by asking
Claude to write a file, which normally requires user approval.

Requires: Claude CLI OAuth setup (~/.claude/.credentials.json)
"""

import asyncio
import importlib
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TETHER_AGENT_DEV_MODE", "1")
os.environ.setdefault("TETHER_AGENT_ADAPTER", "claude_local")

try:
    import claude_agent_sdk  # noqa: F401
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

# Note: We import store_module here but delay runner import until after
# we reload the store in run_test() to ensure they share the same instance
if SDK_AVAILABLE:
    from tether import store as store_module


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

    async def on_header(self, session_id, title, model, provider):
        print(f"[HEADER] {title} | {model} | {provider}")

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

    # Set data dir BEFORE reloading store
    os.environ["TETHER_AGENT_DATA_DIR"] = tmpdir
    importlib.reload(store_module)

    # Import runner AFTER reloading store so they share the same store instance
    from tether.runner import claude_local as runner_module
    importlib.reload(runner_module)
    runner = runner_module.ClaudeLocalRunner(events)

    session = store_module.store.create_session("test", "main")
    session.state = store_module.SessionState.RUNNING
    session.directory = tmpdir
    store_module.store.update_session(session)
    store_module.store.set_workdir(session.id, tmpdir, managed=False)

    # Generate unique content
    secret = f"PERMISSION_TEST_{random.randint(10000, 99999)}"
    output_file = os.path.join(tmpdir, "created_by_claude.txt")

    # Test: Ask Claude to WRITE a file (requires permission normally)
    print(f"=== Permission Bypass Test ===")
    print(f"Working directory: {tmpdir}")
    print(f"Asking Claude to write '{secret}' to created_by_claude.txt")
    print(f"approval_choice=2 (bypassPermissions)")
    print()

    await runner.start(
        session.id,
        f"Create a file called 'created_by_claude.txt' containing exactly this text: {secret}\n"
        f"Do not include any other text. Just write the file.",
        approval_choice=2  # bypassPermissions
    )
    await wait_for_turn(events, timeout=90)
    print("\n")

    if events.errors:
        print(f"FAIL: Errors occurred: {events.errors}")
        await runner.stop(session.id)
        return False

    # Check if file was created
    if not os.path.exists(output_file):
        print(f"FAIL: File was NOT created at {output_file}")
        print("This suggests permission bypass is NOT working - Claude may have asked for approval")
        print("\nFull output:")
        print(events.get_text())
        await runner.stop(session.id)
        return False

    # Check file contents
    with open(output_file, "r") as f:
        content = f.read().strip()

    if secret in content:
        print(f"PASS: File created successfully with correct content")
        print(f"Content: {content}")
    else:
        print(f"FAIL: File exists but content is wrong")
        print(f"Expected: {secret}")
        print(f"Got: {content}")
        await runner.stop(session.id)
        return False

    await runner.stop(session.id)
    return True


def main():
    print("=" * 60)
    print("Permission Bypass Smoke Test (Claude Local / Agent SDK)")
    print("=" * 60)
    print()

    if not SDK_AVAILABLE:
        print("ERROR: claude_agent_sdk not installed")
        sys.exit(1)

    creds_path = os.path.expanduser("~/.claude/.credentials.json")
    if not os.path.exists(creds_path):
        print(f"ERROR: No OAuth credentials at {creds_path}")
        print("TIP: Run 'claude' CLI and authenticate first")
        sys.exit(1)

    print(f"OAuth credentials: {creds_path}")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            success = asyncio.run(run_test(tmpdir))
            if success:
                print("\n" + "=" * 60)
                print("SUCCESS: Permission bypass is working correctly")
                print("=" * 60)
            else:
                print("\n" + "=" * 60)
                print("FAILURE: Permission bypass is NOT working")
                print("=" * 60)
            sys.exit(0 if success else 1)
        except Exception as e:
            print(f"\nFAIL: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)


if __name__ == "__main__":
    main()
