"""An upload's bytes: ``UploadInputs.files`` (UTF-8 text) and ``binary`` (base64) as one map.

A path in both maps, or a ``binary`` value that is not base64 (RFC 4648, standard
alphabet), is refused before anything is written.
"""

from __future__ import annotations

import base64
import binascii

from aksha.runtime.session import named as n


def upload_bytes(command: n.UploadInputs) -> dict[str, bytes]:
    """Every uploaded path to its bytes; raises ``ValueError`` for a bad upload (module doc)."""
    out = {str(p): str(t).encode("utf-8") for p, t in command.files.items()}
    for p, encoded in command.binary.items():
        if p in out:
            raise ValueError(f"{p!r} is in both files and binary")
        try:
            out[str(p)] = base64.b64decode(str(encoded), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"{p!r}: binary is not base64") from None
    return out
