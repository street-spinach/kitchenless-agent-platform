"""The agent-to-agent protocol (agent-protocol P1).

`check`, `check_static`, `ProtocolError` and `OutboundInvalid` are added here with
`check.py` (P4).
"""

from services.common.protocol.catalog import CATALOG, render_summary

__all__ = ["CATALOG", "render_summary"]
