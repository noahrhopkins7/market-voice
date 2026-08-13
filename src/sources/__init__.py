"""Data source modules.

Shared interface: every source exposes one or more callables taking the run
context and returning the fragment of market_data.json that it owns. A source
either returns good data or raises ``SourceError`` — it never returns a partial
or defaulted value. All failure handling lives in ``fetch_data.py``.
"""

from .common import SourceError

__all__ = ["SourceError"]
