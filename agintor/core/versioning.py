from __future__ import annotations

from .. import __version__

RUNTIME_CONTRACT_VERSION = __version__

# Canonical identities are persisted independently of Python's in-process hash
# implementation.  Bump this value only when the canonical byte format changes.
CANONICAL_IDENTITY_VERSION = "agintor.identity.v1"

