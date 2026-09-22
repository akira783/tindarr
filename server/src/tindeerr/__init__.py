"""Tindeerr server: AI-picked movie and series cards to swipe, requests filed to Seerr."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("tindeerr")
except PackageNotFoundError:  # pragma: no cover - only when running from a bare checkout
    __version__ = "0.0.0+unknown"
