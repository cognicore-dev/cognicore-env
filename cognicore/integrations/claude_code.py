"""
CogniCore integration for Claude Code via MCP (Model Context Protocol).

This module provides the necessary adapter for using CogniCore's Experience and
Memory layers directly within the Claude Code environment.
"""

import logging
import os
import sys
from typing import List, Optional

try:
    from cognicore.extension.remote import (
        cognicore_record_experience,
        cognicore_verify_experience,
        cognicore_recall_experience,
        cognicore_share_experience,
        cognicore_check_experience,
    )
except ImportError:
    cognicore_record_experience = None
    cognicore_verify_experience = None
    cognicore_recall_experience = None
    cognicore_share_experience = None
    cognicore_check_experience = None

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    FastMCP = None

logger = logging.getLogger(__name__)


class ClaudeCodeIntegration:
    """Wrapper to expose CogniCore MCP tools to Claude Code."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.environ.get("COGNICORE_DB_PATH", "cognicore_claude.db")
        if self.db_path:
            os.environ["COGNICORE_DB_PATH"] = self.db_path

    def get_tools(self) -> List:
        """Returns the list of 5 MCP-compatible tools for Claude Code."""
        if cognicore_record_experience is None:
            raise ImportError(
                "Missing dependencies for Claude Code Integration. "
                "Please run: pip install cognicore-env[server,mcp]"
            )

        return [
            cognicore_record_experience,
            cognicore_verify_experience,
            cognicore_recall_experience,
            cognicore_share_experience,
            cognicore_check_experience,
        ]

    def setup(self):
        """Initializes the integration and sets environment variables."""
        if self.db_path:
            os.environ["COGNICORE_DB_PATH"] = self.db_path
        logger.info(f"Claude Code integration initialized with DB: {self.db_path}")
        return self.get_tools()

    def create_server(self, name: str = "cognicore-experience") -> "FastMCP":
        """Creates a FastMCP server pre-configured with the 5 experience tools."""
        if FastMCP is None:
            raise ImportError("FastMCP is not available. Install mcp: pip install mcp")

        self.setup()
        server = FastMCP(
            name,
            instructions=(
                "CogniCore Structured Validated Experience MCP Server for Claude Code. "
                "Provides verified experience capture, verification gate, failure warnings, "
                "staleness checks, and cross-agent memory transfer."
            ),
        )

        for tool_fn in self.get_tools():
            server.tool()(tool_fn)

        return server

    def run_stdio(self, name: str = "cognicore-experience"):
        """Runs the MCP server over stdio transport."""
        server = self.create_server(name=name)
        server.run(transport="stdio")


def main():
    """CLI entrypoint for running Claude Code MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    integration = ClaudeCodeIntegration()
    integration.run_stdio()


if __name__ == "__main__":
    main()
