"""
CogniCore integration for Claude Code via MCP (Model Context Protocol).

This module provides the necessary adapter for using CogniCore's Experience and
Memory layers directly within the Claude Code environment.
"""

import logging
try:
    from cognicore.extension.remote import (
        cognicore_record_experience,
        cognicore_verify_experience,
        cognicore_recall_experience,
        cognicore_share_experience,
        cognicore_check_experience
    )
except ImportError:
    # Handle graceful failure when optional dependencies aren't installed
    cognicore_record_experience = None
    cognicore_verify_experience = None
    cognicore_recall_experience = None
    cognicore_share_experience = None
    cognicore_check_experience = None

logger = logging.getLogger(__name__)

class ClaudeCodeIntegration:
    """Wrapper to expose CogniCore MCP tools to Claude Code."""
    
    def __init__(self, db_path: str = "cognicore_claude.db"):
        self.db_path = db_path
        
    def get_tools(self):
        """Returns the list of MCP-compatible tools for Claude Code."""
        if cognicore_record_experience is None:
            raise ImportError("Missing dependencies for Claude Code Integration. Please run: pip install cognicore-env[server,mcp]")
            
        return [
            cognicore_record_experience,
            cognicore_verify_experience,
            cognicore_recall_experience,
            cognicore_share_experience,
            cognicore_check_experience
        ]

    def setup(self):
        """Initializes the integration."""
        logger.info(f"Claude Code integration initialized with DB: {self.db_path}")
        return self.get_tools()
