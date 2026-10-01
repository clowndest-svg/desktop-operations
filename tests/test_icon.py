"""Tests for the generated application icon (scripts/make_icon.py).

The icon is a build input: a malformed .ico does not fail the build, it just
produces an executable with no icon — or worse, one Windows refuses to load.
Nothing else in the suite would notice, so the format is checked here.
"""

from __future__ import annotations

import importlib.util
import itertools
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = REPO_ROOT / "packaging" / "app.ico"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _module() -> Any:
    """Load ``scripts/make_icon.py`` by path (``scripts`` is not a package).

    ``Any`` because the module is loaded dynamically: there is no importable
    type for it, and pretending otherwise would need a stub file that would
    itself drift from the script.
    """
    spec = importlib.util.spec_from_file_location(
        "make_icon", REPO_ROOT / "scripts" / "make_icon.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_icon"] = module
    spec.loader.exec_module(module)
    return module


ICON = _module()


def _entries(data: bytes) -> list[tuple[int, int, int, int, bytes]]:
    """Parse an ICO into ``(width, height, bits, offset, payload)`` tuples."""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0
    assert kind == 1, "type 1 is an icon (type 2 is a cursor)"
    entries = []
    for index in range(count):
        base = 6 + index * 16
        width, height, _colors, _pad, _planes, bits, size, offset = struct.unpack(
            "<BBBBHHII", data[base : base + 16]
        )
        entries.append((width or 256, height or 256, bits, offset, data[offset : offset + size]))
    return entries


class TestIcoFormat:
    def test_the_committed_icon_parses(self) -> None:
        assert ICON_PATH.is_file(), "打包前必须先跑 scripts/make_icon.py"
        assert len(_entries(ICON_PATH.read_bytes())) == len(ICON.ICO_SIZES)

    def test_every_entry_is_a_png(self) -> None:
        """PNG-in-ICO is what Windows Vista and later decode natively; a BMP
        entry would need a bottom-up DIB and a separate AND mask."""
        for _w, _h, _bits, _offset, payload in _entries(ICON_PATH.read_bytes()):
            assert payload.startswith(PNG_SIGNATURE)

    def test_sizes_match_the_declared_ladder(self) -> None:
        """16 px is the taskbar, 256 the explorer preview. Letting Windows
        resample one bitmap for the rest looks visibly soft."""
        sizes = [(w, h) for w, h, _b, _o, _p in _entries(ICON_PATH.read_bytes())]
        assert sizes == [(size, size) for size in ICON.ICO_SIZES]

    def test_entries_do_not_overlap_or_run_past_the_end(self) -> None:
        data = ICON_PATH.read_bytes()
        spans = sorted(
            (offset, offset + len(payload)) for _w, _h, _b, offset, payload in _entries(data)
        )
        for start, end in spans:
            assert 0 < start < end <= len(data)
        for (_a, end), (start, _b) in itertools.pairwise(spans):
            assert end <= start, "icon payloads must not overlap"

    def test_256_is_encoded_as_zero(self) -> None:
        """The width/height fields are one byte each, so 256 is spelled 0."""
        raw = ICON_PATH.read_bytes()
        count = struct.unpack("<HHH", raw[:6])[2]
        width, height = struct.unpack("<BB", raw[6 + (count - 1) * 16 : 8 + (count - 1) * 16])
        assert (width, height) == (0, 0)
        assert ICON.ICO_SIZES[-1] == 256

    def test_generation_is_deterministic(self) -> None:
        """The icon is drawn from constants; two builds must agree, or every
        build would produce a different executable hash."""
        assert ICON.build_ico((32,)) == ICON.build_ico((32,))


class TestRendering:
    def test_rgba_buffer_is_the_right_size(self) -> None:
        assert len(ICON.render_rgba(32)) == 32 * 32 * 4

    def test_the_centre_is_opaque(self) -> None:
        rgba = ICON.render_rgba(64)
        middle = (32 * 64 + 32) * 4
        assert rgba[middle + 3] == 255, "the core dot must be solid"

    def test_the_corner_is_transparent(self) -> None:
        """A rounded square, not a filled rectangle: the corners are what make
        it read as an app icon rather than a swatch."""
        rgba = ICON.render_rgba(64)
        assert rgba[3] == 0

    def test_the_edge_is_partially_transparent(self) -> None:
        """Antialiasing: a hard edge at this size looks jagged in the taskbar."""
        rgba = ICON.render_rgba(64)
        alphas = {rgba[index * 4 + 3] for index in range(64 * 64)}
        assert any(0 < alpha < 255 for alpha in alphas)

    def test_the_mark_is_cyan(self) -> None:
        rgba = ICON.render_rgba(64)
        middle = (32 * 64 + 32) * 4
        red, blue = rgba[middle], rgba[middle + 2]
        assert blue > red, "the core is a cool light, not a warm one"
        assert blue > 200 and red < 220

    def test_no_colour_bleeds_into_transparent_pixels(self) -> None:
        """Premultiplied averaging; without it the antialiased rim picks up the
        colour of fully transparent neighbours and the edge looks dirty."""
        rgba = ICON.render_rgba(48)
        for index in range(48 * 48):
            if rgba[index * 4 + 3] == 0:
                assert rgba[index * 4 : index * 4 + 3] == b"\x00\x00\x00"


class TestBuildWiring:
    def test_build_desktop_writes_the_icon_into_packaging(self) -> None:
        """Not into ``jarvis/ui/web`` — that directory is gitignored, so an icon
        kept there would vanish on a fresh checkout."""
        spec = importlib.util.spec_from_file_location(
            "build_desktop", REPO_ROOT / "scripts" / "build_desktop.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.ICON_PATH == REPO_ROOT / "packaging" / "app.ico"
        assert module.verify() == 0

    def test_the_spec_references_the_icon(self) -> None:
        spec_text = (REPO_ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
        assert "app.ico" in spec_text
        assert "icon=None" not in spec_text

    @pytest.mark.skipif(not ICON_PATH.is_file(), reason="图标尚未生成")
    def test_the_shipped_icon_is_small_enough_to_embed(self) -> None:
        """It goes inside the executable's resources; a megabyte of icon would be
        a strange thing to carry."""
        assert ICON_PATH.stat().st_size < 256 * 1024
