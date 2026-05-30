"""Tests for bridge debug attachment bundle generation."""

from pathlib import Path

import pytest

from tether.bridges.debug_attachments import build_error_debug_bundle
from tether.store import SessionStore


class TestBuildErrorDebugBundle:
    """Test diagnostic attachment generation for bridge error delivery."""

    @pytest.mark.anyio
    async def test_bundle_includes_summary_events_output_and_backtraces(
        self,
        fresh_store: SessionStore,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        session = fresh_store.create_session("repo_test", "main")
        session.directory = "/tmp/repo_test"
        fresh_store.update_session(session)

        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": 'RuntimeError: boom\nTraceback (most recent call last):\n  File "runner.py", line 1\n',
                    "final": True,
                },
            },
        )

        log_path = tmp_path / "tether.log"
        log_path.write_text(
            "INFO starting\n"
            "Traceback (most recent call last):\n"
            '  File "/tmp/tether.py", line 5, in <module>\n'
            "RuntimeError: exploded\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("TETHER_AGENT_LOG_FILE", str(log_path))

        bundle = build_error_debug_bundle(
            session.id,
            metadata={"message": "Process crashed"},
        )

        names = {attachment.filename for attachment in bundle.attachments}
        assert "error-summary.txt" in names
        assert "session-events.jsonl" in names
        assert "recent-output.txt" in names
        assert "backtraces.txt" in names
        assert "tether-log-tail.txt" in names
        assert "Process crashed" in bundle.message

        attachments = {
            attachment.filename: attachment.content for attachment in bundle.attachments
        }
        assert session.id in attachments["error-summary.txt"]
        assert "RuntimeError: boom" in attachments["session-events.jsonl"]
        assert "runtimeerror: exploded" in attachments["tether-log-tail.txt"].lower()
        assert "Traceback (most recent call last)" in attachments["backtraces.txt"]
