"""Tests for shared outbound bridge attachment policy."""

from __future__ import annotations

from tether.bridges.attachments import attachments_from_metadata, resolve_outbound_attachment
from tether.output_postprocess import PublishedAttachment

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16)


def _attachment(path: str, filename: str) -> PublishedAttachment:
    return PublishedAttachment(
        path=path,
        filename=filename,
        title=None,
        size_bytes=None,
    )


def test_resolve_outbound_attachment_sends_valid_png_as_image(tmp_path) -> None:
    """Image sends require both image extension and matching bytes."""

    path = tmp_path / "chart.png"
    path.write_bytes(PNG_BYTES)

    attachment = resolve_outbound_attachment(_attachment(str(path), "chart.png"))

    assert attachment is not None
    assert attachment.send_as_image is True
    assert attachment.media_type == "image/png"


def test_resolve_outbound_attachment_treats_spoofed_png_as_document(tmp_path) -> None:
    """Spoofed image extensions are sent as documents instead of photos."""

    path = tmp_path / "not-image.png"
    path.write_text("plain text")

    attachment = resolve_outbound_attachment(_attachment(str(path), "not-image.png"))

    assert attachment is not None
    assert attachment.send_as_image is False


def test_attachments_from_metadata_applies_count_and_size_caps(tmp_path) -> None:
    """Shared policy drops oversized attachments and stops at max_count."""

    small = tmp_path / "small.txt"
    small.write_text("ok")
    big = tmp_path / "big.txt"
    big.write_text("too big")
    another = tmp_path / "another.txt"
    another.write_text("ok")

    metadata = {
        "attachments": [
            {"path": str(small), "filename": "small.txt"},
            {"path": str(big), "filename": "big.txt"},
            {"path": str(another), "filename": "another.txt"},
        ]
    }

    attachments = attachments_from_metadata(metadata, max_count=1, max_bytes=3)

    assert [attachment.filename for attachment in attachments] == ["small.txt"]


def test_resolve_outbound_attachment_skips_missing_files(tmp_path) -> None:
    """Missing files are ignored before bridge upload attempts."""

    missing = tmp_path / "missing.txt"

    assert resolve_outbound_attachment(_attachment(str(missing), "missing.txt")) is None
