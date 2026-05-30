"""Tests for bridge image pass-through helpers."""

import base64

import pytest

from tether.bridges.image_io import (
    MAX_IMAGE_BYTES,
    detect_image_mime_type,
    images_from_payload,
    make_bridge_image,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + (b"\x00" * 16)


def test_detect_image_mime_type_sniffs_supported_images() -> None:
    """Supported images are identified from bytes, not filename hints."""

    assert detect_image_mime_type(PNG_BYTES) == "image/png"
    assert detect_image_mime_type(JPEG_BYTES) == "image/jpeg"
    assert detect_image_mime_type(b"not an image") is None


def test_make_bridge_image_prefers_sniffed_mime_type() -> None:
    """Bridge MIME metadata is advisory because chat providers can get it wrong."""

    image = make_bridge_image(PNG_BYTES, declared_mime_type="image/jpeg")

    assert image.mime_type == "image/png"


def test_make_bridge_image_encodes_api_payload() -> None:
    """Validated images are converted to the API payload shape."""

    image = make_bridge_image(PNG_BYTES, filename="photo")

    assert image.as_api_payload() == {
        "type": "image",
        "data": base64.b64encode(PNG_BYTES).decode("ascii"),
        "mimeType": "image/png",
        "filename": "photo.png",
    }


def test_make_bridge_image_rejects_oversized_images() -> None:
    """Images above the bridge limit are rejected before base64 expansion."""

    with pytest.raises(ValueError, match="larger"):
        make_bridge_image(PNG_BYTES + (b"x" * MAX_IMAGE_BYTES))


def test_images_from_payload_filters_invalid_items() -> None:
    """API payloads are normalized before being passed to runner RPC."""

    encoded = base64.b64encode(PNG_BYTES).decode("ascii")

    assert images_from_payload(
        [
            {"type": "image", "data": encoded, "mimeType": "image/png"},
            {"type": "image", "data": encoded, "mimeType": "image/jpeg"},
            {"type": "image", "data": "not-base64", "mimeType": "image/png"},
        ]
    ) == [{"type": "image", "data": encoded, "mimeType": "image/png"}]
