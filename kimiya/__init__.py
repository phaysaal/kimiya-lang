"""Kimiya — a program logic for semantic computation with language models.

Versioning: MAJOR.MINOR.PATCH, pre-stable. Until 2.0 the language surface
may change between MINOR versions; each certificate records the version
that produced it, so runs are attributable to a language state.
"""

from ._version import __version__  # noqa: F401
from .cli import main  # noqa: F401,E402
