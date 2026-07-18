"""toto-gateway — Phase 0 OpenAI-compatible passthrough gateway.

The spine of the Toto routing product: ingest an OpenAI-compatible request, resolve it
to a lane + upstream via a data-driven catalog, dispatch through a Runner adapter, tee the
(possibly streamed) response back to the client, and emit a complete provenance/audit record.

No routing intelligence yet — that is Phase 1. Phase 0 proves the trace record is correct
from request #1, because the record is the one thing later phases cannot retrofit.
"""

__version__ = "0.2.0"
