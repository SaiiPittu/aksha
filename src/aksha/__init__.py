"""Aksha: a small, offline, pluggable agent harness.

The runtime core lives in :mod:`aksha.runtime`. It is agent-neutral: agents are
added as agent definitions and extensions, never by changing the runtime.

The distribution is ``aksha-harness`` (``pip install aksha-harness``); the import name and
the command stay ``aksha``. The version comes from the git tag at build time.
"""

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "aksha-harness"
"""The name on the package index; ``aksha`` there is an unrelated project."""

try:
    __version__ = version(DISTRIBUTION)
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
