"""The brand assets Home Assistant serves for this domain.

Home Assistant 2026.3 dropped the requirement to get a PR accepted into
`home-assistant/brands` for a custom integration: `homeassistant.loader`
sets `has_branding` from the presence of a top-level `brand` directory, and
`components/brands` reads the files straight out of it, ahead of the CDN.
So these two PNGs are the whole of the estate's logo story for `tempest_wx`,
and nothing upstream validates them -- the brands repository's own CI never
sees them, and hassfest does not look at the directory at all.

Reads the PNG header with `struct` rather than Pillow: the pure-layer suite
declares its dependencies (tests/requirements.txt) and an image library
earns a line there only if it buys something this cannot do.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "brand"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# homeassistant/components/brands/const.py: ALLOWED_IMAGES. Anything else in
# the directory is never served, so a typo lands as silence rather than as an
# error -- which is exactly the failure this set exists to catch.
ALLOWED_IMAGES = frozenset(
    {
        "icon.png",
        "logo.png",
        "icon@2x.png",
        "logo@2x.png",
        "dark_icon.png",
        "dark_logo.png",
        "dark_icon@2x.png",
        "dark_logo@2x.png",
    }
)

# homeassistant/components/brands/const.py: IMAGE_FALLBACKS. Core walks this
# chain inside the local brand directory BEFORE it considers the CDN, which is
# why a square icon alone answers a request for a logo or a dark variant.
IMAGE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "logo.png": ("icon.png",),
    "icon@2x.png": ("icon.png",),
    "logo@2x.png": ("logo.png", "icon.png"),
    "dark_icon.png": ("icon.png",),
    "dark_logo.png": ("dark_icon.png", "logo.png", "icon.png"),
    "dark_icon@2x.png": ("icon@2x.png", "icon.png"),
    "dark_logo@2x.png": ("dark_icon@2x.png", "logo@2x.png", "logo.png", "icon.png"),
}

# The brands specification: a square icon is 256px, its hDPI twin 512px. The
# artwork is square, so the specification says to ship the icon alone and let
# the logo fall back to it -- hence no logo entry here.
EXPECTED = {"icon.png": 256, "icon@2x.png": 512}


def _png_size(path: Path) -> tuple[int, int, int]:
    """(width, height, colour_type) from the IHDR chunk."""
    raw = path.read_bytes()
    assert raw[:8] == PNG_MAGIC, f"{path.name} is not a PNG"
    # IHDR is required to be the first chunk: length, type, then the fields.
    length, chunk = struct.unpack(">I4s", raw[8:16])
    assert chunk == b"IHDR", f"{path.name} opens with {chunk!r}, not IHDR"
    width, height, _depth, colour_type = struct.unpack(">IIBB", raw[16:26])
    return width, height, colour_type


def test_brand_directory_exists() -> None:
    """`has_branding` is the presence of this directory and nothing else."""
    assert BRAND.is_dir(), "no brand/ directory: HA will not look for local images"


def test_no_unserved_files_in_brand() -> None:
    """A name outside ALLOWED_IMAGES is dead weight core will never read."""
    found = {p.name for p in BRAND.iterdir() if p.is_file()}
    assert found <= ALLOWED_IMAGES, f"never served: {sorted(found - ALLOWED_IMAGES)}"


def test_the_expected_set_is_what_is_shipped() -> None:
    """Both directions: nothing missing, nothing extra."""
    assert {p.name for p in BRAND.iterdir() if p.is_file()} == set(EXPECTED)


@pytest.mark.parametrize(("name", "side"), sorted(EXPECTED.items()))
def test_icon_is_a_square_png_at_its_declared_size(name: str, side: int) -> None:
    """Square, and the exact size the brands specification names."""
    width, height, colour_type = _png_size(BRAND / name)
    assert (width, height) == (side, side)
    assert colour_type == 6, f"{name} colour type {colour_type}, expected 6 (RGBA)"


@pytest.mark.parametrize("requested", sorted(ALLOWED_IMAGES))
def test_every_servable_image_resolves_inside_the_brand_directory(
    requested: str,
) -> None:
    """No request reaches the CDN.

    Core tries the requested name then its fallback chain, all within brand/.
    Shipping the icon alone is only correct while every other name still lands
    on a file that is here; renaming icon.png breaks eight requests at once and
    this is the check that says so.
    """
    chain = (requested, *IMAGE_FALLBACKS.get(requested, ()))
    assert any((BRAND / candidate).is_file() for candidate in chain), (
        f"{requested} resolves to nothing: tried {list(chain)}"
    )
