"""Tests for final-output cleanup and STOP footer rendering."""

from tether.models import Session, SessionState
from tether.output_postprocess import (
    compose_final_output,
    extract_publish_attachments,
    format_duration,
)


def _session_for_directory(path: str) -> Session:
    return Session(
        id="sess_test",
        repo_id="repo",
        repo_display="repo",
        repo_ref_type="path",
        repo_ref_value=path,
        state=SessionState.RUNNING,
        name="Test",
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
        ended_at=None,
        last_activity_at="2026-01-01T00:00:00Z",
        exit_code=None,
        summary=None,
        runner_header=None,
        directory=path,
        directory_has_git=False,
        workdir_managed=False,
    )


class TestExtractPublishAttachments:
    def test_extracts_and_strips_attachment_directives(self, tmp_path) -> None:
        report = tmp_path / "report.md"
        report.write_text("# hello\n", encoding="utf-8")
        session = _session_for_directory(str(tmp_path))

        processed = extract_publish_attachments(
            session,
            "Summary\nPUBLISH AS ATTACHMENT: report.md",
        )

        assert processed.text == "Summary"
        assert len(processed.attachments) == 1
        assert processed.attachments[0].filename == "report.md"
        assert processed.warnings == ()

    def test_rejects_attachment_outside_session_directory(self, tmp_path) -> None:
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        session = _session_for_directory(str(tmp_path))

        processed = extract_publish_attachments(
            session,
            f"PUBLISH AS ATTACHEMENT: {outside}",
        )

        assert processed.attachments == ()
        assert processed.warnings
        assert "escapes the session directory" in processed.warnings[0]


class TestComposeFinalOutput:
    def test_footer_is_appended_once(self) -> None:
        visible = compose_final_output(
            "Result\nSTOP 🛑✅ 1s",
            status="success",
            duration_ms=65_000,
        )
        assert visible == "Result\nSTOP 🛑✅ 1m 05s"

    def test_warnings_stay_above_stop_footer(self) -> None:
        visible = compose_final_output(
            "Result",
            status="error",
            duration_ms=5_000,
            warnings=["report.md: file not found."],
        )
        assert visible.endswith("STOP 🛑❌ 5s")
        assert "Attachment warning: report.md: file not found." in visible

    def test_duration_formatting(self) -> None:
        assert format_duration(12_000) == "12s"
        assert format_duration(65_000) == "1m 05s"
        assert format_duration(3_723_000) == "1h 02m 03s"
