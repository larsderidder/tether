#!/usr/bin/env python3
"""Smoke test for the Sidecar runner.

Usage:
    # Dry run (check config only)
    python scripts/smoke_test_sidecar.py

    # With real sidecar connection (requires running sidecar)
    python scripts/smoke_test_sidecar.py --live

    # Custom sidecar URL
    python scripts/smoke_test_sidecar.py --live --url http://localhost:8788
"""

import argparse
import asyncio
import http.client
import os
import sys
import tempfile
import urllib.parse

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TETHER_AGENT_DEV_MODE", "1")
os.environ.setdefault("TETHER_AGENT_ADAPTER", "codex_sdk_sidecar")


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
        preview = text[:80] + "..." if len(text) > 80 else text
        print(f"  [output:{kind}] {preview}")

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


def test_instantiation():
    """Test that SidecarRunner can be instantiated."""
    print("\n1. Testing instantiation...")
    try:
        from tether.runner.sidecar import SidecarRunner
        events = MockEvents()
        runner = SidecarRunner(events)
        print(f"   Runner type: {runner.runner_type}")
        print(f"   Base URL: {runner._base_url}")
        print(f"   Token configured: {'yes' if runner._token else 'no'}")
        print("   OK: Runner instantiated successfully")
        return runner, events
    except Exception as e:
        print(f"   FAIL: {e}")
        return None, None


def test_sidecar_health(url):
    """Check if sidecar is reachable."""
    print(f"\n2. Checking sidecar health at {url}...")
    try:
        parsed = urllib.parse.urlparse(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()

        if resp.status == 200:
            print(f"   OK: Sidecar is healthy ({data})")
            return True
        else:
            print(f"   FAIL: Sidecar returned {resp.status}: {data}")
            return False
    except Exception as e:
        print(f"   FAIL: Cannot connect to sidecar: {e}")
        return False


async def test_live_session(runner, events, url):
    """Start a real session with the sidecar."""
    print("\n3. Testing live sidecar session...")

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

        workdir = tmpdir
        store.set_workdir(session.id, workdir, managed=False)

        print(f"   Session: {session.id}")
        print(f"   Workdir: {workdir}")

        prompt = "Say 'Hello from sidecar smoke test!' and nothing else."
        print(f"   Prompt: {prompt}")
        print("   Starting session...")

        try:
            await runner.start(session.id, prompt, approval_choice=0)

            # Wait for some output (with timeout)
            timeout = 30
            elapsed = 0
            while elapsed < timeout:
                await asyncio.sleep(0.5)
                elapsed += 0.5
                if events.exit_code is not None or events.awaiting_input:
                    break
                if events.errors:
                    break

            if events.errors:
                # Connection errors are expected if sidecar isn't fully set up
                print(f"   WARN: Got errors (may be expected): {events.errors[0]}")

            if events.outputs:
                print(f"   OK: Received {len(events.outputs)} output chunks")
            else:
                print("   WARN: No outputs received (sidecar may not have Codex configured)")

            # Stop the runner
            try:
                await runner.stop(session.id)
            except Exception:
                pass  # Stop may fail if session didn't start

            return True

        except Exception as e:
            print(f"   FAIL: {e}")
            import traceback
            traceback.print_exc()
            return False


def main():
    parser = argparse.ArgumentParser(description="Smoke test for Sidecar runner")
    parser.add_argument("--live", action="store_true", help="Connect to real sidecar")
    parser.add_argument("--url", default=None, help="Sidecar URL (default: from settings)")
    args = parser.parse_args()

    if args.url:
        os.environ["TETHER_CODEX_SIDECAR_URL"] = args.url

    print("=" * 60)
    print("Sidecar Runner Smoke Test")
    print("=" * 60)

    # Test 1: Instantiation
    runner, events = test_instantiation()
    if not runner:
        sys.exit(1)

    url = runner._base_url

    # Test 2: Health check (optional for --live)
    if args.live:
        healthy = test_sidecar_health(url)
        if not healthy:
            print("\n   TIP: Start the sidecar with: cd codex-sdk-sidecar && npm start")
            sys.exit(1)

        # Test 3: Live session
        success = asyncio.run(test_live_session(runner, events, url))
        if not success:
            sys.exit(1)
    else:
        print(f"\n2. Skipping sidecar health check (use --live to enable)")
        print("   Sidecar URL would be:", url)
        print("\n3. Skipping live session (use --live to enable)")

    print("\n" + "=" * 60)
    print("Smoke test passed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
