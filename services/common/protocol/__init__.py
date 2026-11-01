"""The agent-to-agent protocol (agent-protocol P1)."""

from services.common.protocol.catalog import CATALOG, render_summary
from services.common.protocol.check import OutboundInvalid, ProtocolError, check, check_static

__all__ = ["CATALOG", "OutboundInvalid", "ProtocolError", "check", "check_static", "render_summary"]
