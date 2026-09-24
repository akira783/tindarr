#!/usr/bin/env python3
"""Rebuild the console's self-hosted fonts (design/tokens.css declares them).

The console runs under ``font-src 'self'`` (ADR 0009): no Google Fonts, no CDN,
no ``data:`` URI. The two families the refonte uses are both under the SIL Open
Font License 1.1 and are taken from Google's own font repository, not from the
Fonts API, so what is committed is the upstream variable font rather than a
per-subset slice:

    ofl/newsreader/Newsreader[opsz,wght].ttf
    ofl/newsreader/Newsreader-Italic[opsz,wght].ttf
    ofl/instrumentsans/InstrumentSans[wdth,wght].ttf

Each is cut down to the Latin characters the two catalogues need, its axes are
limited to the ranges the stylesheet asks for, and it is written out as variable
WOFF2. The arrows U+2190-2193 the keyboard hints draw only survive in Instrument
Sans — Newsreader has no arrow to keep — which is why ``.kbd`` pins itself to
``--font-ui``. U+202F, the French narrow no-break space, is in none of the three
upstream fonts, so the catalogues use U+00A0 instead. Newsreader keeps
its optical-size axis: browsers vary it by themselves, and the card title is set
at 52 px while the rationale is set at 26 px. Instrument Sans is pinned to
``wdth: 100``, the only width the console uses.

The licences travel with the files, in ``web/public/fonts/OFL-*.txt``.

Usage:
    uv run --with 'fonttools[woff]' --with brotli python scripts/build-fonts.py <dir-of-ttfs>
"""

import sys
from pathlib import Path

from fontTools.subset import Options, Subsetter, parse_unicodes
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

#: Latin and Latin Extended, the punctuation the catalogues use, the currency
#: signs a provider name may carry, and U+2190–2193: the arrows the verdict bar
#: prints on its keys.
UNICODES = (
    "U+0000-00FF,U+0100-024F,U+0259,U+1E00-1EFF,"
    "U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+02C7,U+02D8-02DD,"
    "U+2000-206F,U+2070,U+2074,U+20A0-20CF,U+2113,U+2122,U+2190-2193,"
    "U+2212,U+2215,U+2018-201F,U+2026,U+2013-2014,U+00A0,U+00AB,U+00BB,"
    "U+2022,U+2030,U+2039-203A,U+FEFF,U+FFFD,U+2C60-2C7F,U+A720-A7FF"
)

OUT = Path(__file__).resolve().parent.parent / "public" / "fonts"

BUILDS = [
    ("Newsreader[opsz,wght].ttf", "newsreader-var.woff2", {"wght": (300, 400, 700), "opsz": (14, 18, 60)}),
    ("Newsreader-Italic[opsz,wght].ttf", "newsreader-italic-var.woff2", {"wght": (300, 400, 700), "opsz": (14, 18, 60)}),
    ("InstrumentSans[wdth,wght].ttf", "instrument-sans-var.woff2", {"wdth": 100}),
]


def build(source: Path, target: Path, axes: dict[str, object]) -> None:
    """Subset and instance one family into a variable WOFF2."""
    font = instantiateVariableFont(TTFont(source), axes, updateFontNames=False)
    options = Options()
    options.flavor = "woff2"
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.notdef_outline = True
    subsetter = Subsetter(options=options)
    subsetter.populate(unicodes=parse_unicodes(UNICODES))
    subsetter.subset(font)
    font.flavor = "woff2"
    font.save(target)
    print(f"{target.name}: {target.stat().st_size // 1024} KiB")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    sources = Path(sys.argv[1])
    for name, target, axes in BUILDS:
        build(sources / name, OUT / target, axes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
