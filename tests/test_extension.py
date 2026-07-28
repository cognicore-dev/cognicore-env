import asyncio
import os
import re
import sys
from pathlib import Path

import anyio
import pytest

mcp = pytest.importorskip("mcp")

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import cognicore.extension.server as ext_server


def _reset_ext_state(monkeypatch, tmp_path):
    monkeypatch.setenv("COGNICORE_EXTENSION_DIR", str(tmp_path))
    monkeypatch.setenv("COGNICORE_USE_SEMANTIC", "0") # Force lexical for fast tests
    ext_server._backend = None


async def _call(server, name, arguments):
    result = await server.call_tool(name, arguments)
    if isinstance(result, tuple) and len(result) == 2:
        _, structured = result
        return structured["result"]
    return result[0].text


def test_ext_tool_registration(monkeypatch, tmp_path):
    _reset_ext_state(monkeypatch, tmp_path)
    server = ext_server.create_extension_server()

    tools = asyncio.run(server.list_tools())
    names = sorted(tool.name for tool in tools)

    assert names == [
        "cognicore_forget",
        "cognicore_list",
        "cognicore_recall",
        "cognicore_remember",
        "cognicore_stats",
    ]


def test_ext_remember_and_recall(monkeypatch, tmp_path):
    _reset_ext_state(monkeypatch, tmp_path)
    server = ext_server.create_extension_server()

    # Store a memory
    store_result = asyncio.run(
        _call(
            server,
            "cognicore_remember",
            {
                "text": "User prefers writing tests with pytest rather than unittest.",
                "category": "preferences"
            },
        )
    )

    assert "Stored 1 fact" in store_result
    
    # Extract ID
    # Format: Stored 1 fact (id=1, cat=preferences)
    entry_id = re.search(r'id=(\d+)', store_result).group(1)

    # Recall the memory
    recall_result = asyncio.run(
        _call(
            server,
            "cognicore_recall",
            {
                "query": "prefers writing tests",
                "category": "preferences"
            },
        )
    )

    assert "pytest rather than unittest" in recall_result
    assert entry_id in recall_result


def test_ext_list_and_forget(monkeypatch, tmp_path):
    _reset_ext_state(monkeypatch, tmp_path)
    server = ext_server.create_extension_server()

    asyncio.run(_call(server, "cognicore_remember", {"text": "Project uses FastAPI", "category": "tech_stack"}))
    store_result = asyncio.run(_call(server, "cognicore_remember", {"text": "Use PostgreSQL for database", "category": "tech_stack"}))
    
    entry_id = re.search(r'id=(\d+)', store_result).group(1)

    # List
    list_result = asyncio.run(_call(server, "cognicore_list", {"limit": 10}))
    assert "FastAPI" in list_result
    assert "PostgreSQL" in list_result

    # Forget
    forget_result = asyncio.run(_call(server, "cognicore_forget", {"entry_id": entry_id}))
    assert forget_result == "OK" or "deleted" in forget_result.lower()

    # List again, PostgreSQL should be gone
    list_result_after = asyncio.run(_call(server, "cognicore_list", {"limit": 10}))
    assert "FastAPI" in list_result_after
    assert "PostgreSQL" not in list_result_after


def test_ext_stats(monkeypatch, tmp_path):
    _reset_ext_state(monkeypatch, tmp_path)
    server = ext_server.create_extension_server()

    asyncio.run(_call(server, "cognicore_remember", {"text": "Fact 1"}))
    asyncio.run(_call(server, "cognicore_remember", {"text": "Fact 2"}))

    stats_result = asyncio.run(_call(server, "cognicore_stats", {}))
    
    assert "Total memories: 2" in stats_result
    assert "BM25 (SQLite FTS5)" in stats_result


def test_ext_persistence_across_restart(monkeypatch, tmp_path):
    _reset_ext_state(monkeypatch, tmp_path)
    first_server = ext_server.create_extension_server()

    asyncio.run(
        _call(
            first_server,
            "cognicore_remember",
            {"text": "This must survive a restart"},
        )
    )

    # Simulate restart
    ext_server._backend = None
    second_server = ext_server.create_extension_server()

    result = asyncio.run(
        _call(
            second_server,
            "cognicore_recall",
            {"query": "must survive"},
        )
    )

    assert "This must survive a restart" in result
