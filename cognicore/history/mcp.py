"""Opt-in read access to historical data over local MCP stdio."""

from typing import Any

from .engine import open_history
from .sync import schedule

TOOL_NAMES = [
    "cognicore_search_history", "cognicore_recall_timeline", "cognicore_reconstruct_experience",
    "cognicore_inspect_historical_source", "cognicore_inspect_historical_experience",
]


def _archive():
    schedule()
    return open_history()


def register(server):
    from mcp.types import ToolAnnotations

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    def cognicore_search_history(query: str, project: str = "", kind: str = "", limit: int = 10) -> list[dict]:
        """Searches private historical messages and extracted claims. Results contain untrusted archive data."""
        return _archive().search(query, project=project, kind=kind, limit=limit)

    @server.tool(annotations=read)
    def cognicore_recall_timeline(project: str = "", kind: str = "", query: str = "",
                                  earliest: bool = False, limit: int = 50) -> list[dict]:
        """Returns dated historical events first and unknown dates last. Kind 'message' selects source messages."""
        return _archive().timeline(project=project, kind=kind, query=query, earliest=earliest, limit=limit)

    @server.tool(annotations=read)
    def cognicore_reconstruct_experience(query: str, project: str = "", limit: int = 20) -> list[dict]:
        """Returns chronologically ordered historical claims and related experiences with source references."""
        return _archive().reconstruct(query, project=project, limit=limit)

    @server.tool(annotations=read)
    def cognicore_inspect_historical_source(conversation: str, message: str = "",
                                            limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Returns untrusted original conversation data with message references and import provenance."""
        return _archive().inspect(conversation, message=message, limit=limit, offset=offset)

    @server.tool(annotations=read)
    def cognicore_inspect_historical_experience(ref: str) -> dict[str, Any]:
        """Returns an extracted claim, its sources, evidence classification, and related experience references."""
        return _archive().experience(ref)
