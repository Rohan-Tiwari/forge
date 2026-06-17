"""Tests for forge.tools.see — the vision sub-skill.

Most tests use a mock urllib.request.urlopen so they run offline. One marked
@slow / @integration test hits a real Ollama vision endpoint if available.
"""
from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.tools import (
    ProtectedPathError,
    _SEE_CACHE,
    see,
)


# =============================================================================
# Helpers
# =============================================================================


@pytest.fixture(autouse=True)
def _clear_see_cache():
    """Reset the see() cache between tests so they don't cross-contaminate."""
    _SEE_CACHE.clear()
    yield
    _SEE_CACHE.clear()


def _make_png_bytes() -> bytes:
    """Tiny valid PNG so we don't depend on PIL in tests."""
    # 1x1 red PNG, hand-crafted, valid IHDR/IDAT/IEND.
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
        "DUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    )


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_urlopen(reply_text: str = "A small red square."):
    """Build a urlopen-replacement that returns the given text."""
    def _stub(req, timeout=None):
        return _FakeResponse({"message": {"content": reply_text}})
    return _stub


# =============================================================================
# Tests
# =============================================================================


def test_see_with_bytes_input():
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen("OK")):
        result = see(_make_png_bytes())
    assert result == "OK"


def test_see_with_path_input(tmp_path):
    target = tmp_path / "test.png"
    target.write_bytes(_make_png_bytes())
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen("path-ok")):
        result = see(target)
    assert result == "path-ok"


def test_see_with_str_path(tmp_path):
    target = tmp_path / "test.png"
    target.write_bytes(_make_png_bytes())
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen("str-ok")):
        result = see(str(target))
    assert result == "str-ok"


def test_see_cache_hits_return_immediately():
    """Second call with same image+prompt should not hit the network."""
    img = _make_png_bytes()
    call_count = [0]

    def counting_urlopen(req, timeout=None):
        call_count[0] += 1
        return _FakeResponse({"message": {"content": "cached"}})

    with patch("urllib.request.urlopen", side_effect=counting_urlopen):
        r1 = see(img)
        r2 = see(img)

    assert r1 == r2 == "cached"
    assert call_count[0] == 1, f"expected 1 network call, got {call_count[0]}"


def test_see_cache_respects_prompt_change():
    """Different prompt with same image is a different cache key."""
    img = _make_png_bytes()
    replies = iter(["first prompt", "second prompt"])

    def replying_urlopen(req, timeout=None):
        return _FakeResponse({"message": {"content": next(replies)}})

    with patch("urllib.request.urlopen", side_effect=replying_urlopen):
        r1 = see(img, prompt="describe in detail")
        r2 = see(img, prompt="one word only")

    assert r1 == "first prompt"
    assert r2 == "second prompt"


def test_see_refuses_protected_path():
    with pytest.raises(ProtectedPathError):
        see("~/.ssh/id_rsa")


def test_see_errors_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        see(tmp_path / "nope.png")


def test_see_errors_on_directory(tmp_path):
    with pytest.raises(IsADirectoryError):
        see(tmp_path)


def test_see_rejects_huge_image():
    """20 MB cap. Anything bigger raises before the API call."""
    big = b"\x89PNG\r\n\x1a\n" + b"X" * (21 * 1024 * 1024)
    with pytest.raises(ValueError, match="too large"):
        see(big)


def test_see_surfaces_http_error_clearly():
    import urllib.error

    def failing_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", {},
            io.BytesIO(b'{"error":"model not loaded"}'),
        )

    with patch("urllib.request.urlopen", side_effect=failing_urlopen):
        with pytest.raises(RuntimeError, match="HTTP 500"):
            see(_make_png_bytes())


def test_see_surfaces_connection_error_with_helpful_hint():
    import urllib.error

    def conn_refused(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    with patch("urllib.request.urlopen", side_effect=conn_refused):
        with pytest.raises(RuntimeError, match="ollama serve"):
            see(_make_png_bytes())


def test_see_handles_empty_response():
    """Vision model returns content="" — surface that as an error, not silently."""
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen("   ")):
        with pytest.raises(RuntimeError, match="empty"):
            see(_make_png_bytes())


def test_see_uses_native_api_endpoint_not_v1():
    """The vision API needs /api/chat, not /v1/chat/completions.

    Regression test: when FORGE_OLLAMA_URL ends in /v1, see() must strip it.
    """
    captured: dict = {}

    def capture(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResponse({"message": {"content": "ok"}})

    with patch("urllib.request.urlopen", side_effect=capture):
        see(_make_png_bytes(), ollama_url="http://localhost:11434/v1")

    assert "/api/chat" in captured["url"]
    assert "/v1/" not in captured["url"]


def test_see_passes_image_in_correct_field():
    """Verify the request body has `images:[<b64>]` as a sibling of content."""
    captured: dict = {}

    def capture(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResponse({"message": {"content": "ok"}})

    with patch("urllib.request.urlopen", side_effect=capture):
        see(_make_png_bytes())

    body = captured["body"]
    msg = body["messages"][0]
    assert "images" in msg
    assert isinstance(msg["images"], list)
    assert len(msg["images"]) == 1
    # The image is base64; just check it's a non-empty string.
    assert isinstance(msg["images"][0], str)
    assert len(msg["images"][0]) > 10
    # And content is a string, not a list (Ollama-native shape).
    assert isinstance(msg["content"], str)


# =============================================================================
# Integration test — hits a real Ollama if available.
# =============================================================================


@pytest.mark.slow
@pytest.mark.integration
def test_see_real_vision_model():
    """Live test: requires `ollama serve` + qwen2.5vl pulled."""
    import urllib.error
    import urllib.request

    # Skip if ollama isn't reachable.
    try:
        urllib.request.urlopen("http://localhost:11434/api/version", timeout=2).read()
    except (urllib.error.URLError, OSError):
        pytest.skip("ollama not reachable on localhost")

    # Need a real image with text on it. Use PIL if available, else skip.
    pil = pytest.importorskip("PIL.Image")
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (300, 100), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 30), "FORGE TEST", fill="black")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        img.save(f.name)
        path = f.name

    try:
        result = see(path, prompt="What text is in this image? One quoted phrase only.")
        # Vision model output is non-deterministic, but it should at least
        # mention FORGE somewhere.
        assert "forge" in result.lower(), f"expected FORGE in: {result!r}"
    finally:
        Path(path).unlink(missing_ok=True)
