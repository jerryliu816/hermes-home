"""The MCP server, mounted alongside the HTTP API.

One process serves both the Home Assistant webhook and Hermes' MCP connection.
They share the database, the config, and the process lifetime, and separating
them would buy nothing for a single-home deployment.

Transport is Streamable HTTP because that is what Hermes' MCP client expects from
a `url:` entry in `~/.hermes/config.yaml`.
"""

from __future__ import annotations

import structlog

from hermes_home.api.deps import AppState
from hermes_home.mcp.tools import register_tools

logger = structlog.get_logger(__name__)

MCP_PATH = "/mcp"
SERVER_NAME = "hermes-home"

INSTRUCTIONS = """\
Persistent memory of what has happened around this home: camera observations,
what was seen in each, where it happened, and how places connect.

WHEN TO USE THESE TOOLS

Any question about the PAST or about what was OBSERVED. Reach for them first,
before any other approach:

  "What happened at the front door?"          -> home_recent_events
  "What happened while I was away?"           -> home_recent_events
  "Did anyone come to the house?"             -> home_recent_events
  "Was a package delivered?"                  -> home_recent_events
  "What happened yesterday afternoon?"        -> home_search_events
  "When was the last person at the driveway?" -> home_search_events
  "Summarize today's activity."               -> home_summarize_activity
  "How many events this morning?"             -> home_summarize_activity
  "Which cameras are there / is X covered?"   -> home_describe_home

Answering these does NOT require inspecting Home Assistant, containers, logs,
the filesystem, or the desktop, and does not require writing a script to reach
this server. Call the tool directly.

WHEN NOT TO USE THEM

  Current state of a device ("is the camera online right now?", "is the door
  locked?")            -> your Home Assistant tools
  Controlling anything ("turn on the porch light")
                       -> your Home Assistant tools

This server holds history only. It has no live device state and cannot control
anything.

READING RESULTS

- An empty result is not a dead end. It reports latest_event_at and a hint;
  widen the window and call again rather than concluding there is no data.
- A null count or flag means "could not be determined from the image", NOT
  zero. Say "unknown" rather than asserting nothing was there.
- Observations describe appearance only. They never identify people, and you
  should not infer or assert who someone is from them.
- Each observation carries the provider and model that produced it. If it came
  from a mock or test provider, say so when reporting the interpretation.
- A zone in unobserved_zones has no camera. Silence from it means nothing was
  recorded, not that nothing happened.
- A zone in partially_observed_zones is only partly covered -- one camera sees
  the near half of the yard, say. No events there is weaker evidence than in a
  fully covered zone; qualify the answer rather than giving an all-clear.
"""


def create_mcp_server(state: AppState):  # type: ignore[no-untyped-def]
    """Build the MCP server and register every tool against ``state``."""
    from mcp.server import MCPServer

    mcp = MCPServer(
        name=SERVER_NAME,
        title="hermes-home",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )
    register_tools(mcp, state)
    logger.info("mcp.server.created", path=MCP_PATH, server=SERVER_NAME)
    return mcp


def build_transport_security(settings):  # type: ignore[no-untyped-def]
    """DNS-rebinding protection, kept enabled with an explicit allowlist.

    The protection is worth keeping: this endpoint sits on a home LAN and a
    browser on that network could otherwise be induced to reach it. Hermes runs
    on the same machine and connects via localhost, which is always allowed.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_host_allowlist(),
        allowed_origins=[],
    )
