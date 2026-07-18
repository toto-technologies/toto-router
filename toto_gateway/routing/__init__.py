"""Routing — the raw passthrough's safety floor (GuardRouter + Policy).

Content-based lane selection moved to the driver's metadata classifier
(`toto_gateway/driver/classify.py`), which superseded the old exemplar/cosine router.
"""

from .decision import GuardRouter
from .policy import Policy

__all__ = ["GuardRouter", "Policy"]
