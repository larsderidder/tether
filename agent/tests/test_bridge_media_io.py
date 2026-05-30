"""Tests for inbound non-image bridge media storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from tether.bridges.media_io import (
    append_media_file_references,
    download_with_media_policy,
    store_bridge_media_file,
    supported_media_type,
    validate_media_download_url,
)


def test_validate_media_download_url_allows_platform_hosts() -> None:
    """Bridge downloads are limited to known platform media hosts."""

    validate_media_download_url("discord", "https://cdn.discordapp.com/attachments/a/b")
    validate_media_download_url("telegram", "https://api.telegram.org/file/bot/x")

    with pytest.raises(ValueError, match="host"):
        validate_media_download_url("discord", "https://example.com/file.png")


@pytest.mark.anyio
async def test_download_with_media_policy_applies_total_timeout() -> None:
    """Slow bridge media downloads are cancelled centrally."""

    async def slow_download() -> bytes:
        import asyncio

        await asyncio.sleep(0.05)
        return b"late"

    with pytest.raises(TimeoutError):
        await download_with_media_policy(
            slow_download,
            platform="discord",
            total_timeout_s=0.001,
        )


@pytest.mark.anyio
async def test_download_with_media_policy_returns_bytes() -> None:
    """Successful downloads are normalized to bytes."""

    async def download() -> bytearray:
        return bytearray(b"ok")

    assert await download_with_media_policy(download, platform="discord") == b"ok"


def test_supported_media_type_accepts_documents_audio_and_video() -> None:
    """Supported media excludes images but includes common file inputs."""

    assert supported_media_type("application/pdf") is True
    assert supported_media_type("audio/mpeg") is True
    assert supported_media_type("video/mp4") is True
    assert supported_media_type("image/png") is False
    assert supported_media_type("application/x-msdownload") is False


def test_store_bridge_media_file_sanitizes_name_and_writes_under_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored media paths are rooted in Tether's data dir with safe names."""

    monkeypatch.setattr("tether.bridges.media_io.settings.data_dir", lambda: str(tmp_path))

    media = store_bridge_media_file(
        session_id="sess/../bad",
        data=b"hello",
        filename="../report.pdf",
        mime_type="application/pdf",
    )

    path = Path(media.path)
    assert path.is_file()
    assert path.read_bytes() == b"hello"
    assert tmp_path in path.parents
    assert media.filename == "report.pdf"
    assert ".." not in path.name


def test_append_media_file_references_adds_local_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents receive a plain-text list of stored media files."""

    monkeypatch.setattr("tether.bridges.media_io.settings.data_dir", lambda: str(tmp_path))
    media = store_bridge_media_file(
        session_id="sess1",
        data=b"hello",
        filename="notes.txt",
        mime_type="text/plain",
    )

    text = append_media_file_references("summarize", [media])

    assert text.startswith("summarize")
    assert "Attached files saved locally:" in text
    assert "notes.txt" in text
    assert media.path in text
