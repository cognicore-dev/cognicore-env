"""The MCP tools for session transfer keep their old parameters; new ones are optional."""
import pytest

OLD_PARAMS = {
    "cognicore_save_session": {"conversation", "session_name"},
    "cognicore_resume_session": {"query", "last_n_sessions", "include_action_items"},
    "cognicore_compress_context": {"conversation", "keep_last_n"},
}
NEW_OPTIONAL = {
    "cognicore_save_session": {"agent", "goal", "current_state", "environment_json"},
    "cognicore_resume_session": {"max_tokens", "format", "session_id", "section", "environment_json"},
}


def _schemas(tools):
    return {t.name: t.inputSchema for t in tools}


@pytest.mark.parametrize("server", ["local", "remote"])
def test_mcp_tools_keep_old_parameters_and_new_ones_are_optional(server, monkeypatch, tmp_path):
    import asyncio

    if server == "remote":
        remote = pytest.importorskip("cognicore.connectors.remote")
        schemas = _schemas(asyncio.run(remote.mcp.list_tools()))
        exp = schemas["cognicore_record_experience"]
        assert "os_name" in exp["properties"] and "os_name" not in exp.get("required", [])
        for name in ("cognicore_recall_experience", "cognicore_check_experience"):
            assert "os_name" not in schemas[name].get("required", [])
    else:
        pytest.importorskip("mcp")
        from cognicore.mcp import server as mcp_server

        monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path))
        schemas = _schemas(asyncio.run(mcp_server.create_mcp_server().list_tools()))
    for name, params in OLD_PARAMS.items():
        assert params <= set(schemas[name]["properties"]), name
    for name, params in NEW_OPTIONAL.items():
        props, required = set(schemas[name]["properties"]), set(schemas[name].get("required", []))
        assert params <= props and not (params & required), name


def test_local_server_fills_in_os():
    pytest.importorskip("mcp")
    import platform

    from cognicore.mcp.server import _with_local_os

    assert _with_local_os("") == {"os": platform.system()}
    assert _with_local_os('{"os": "Linux", "python_version": "3.12"}') == {"os": "Linux", "python_version": "3.12"}
    assert _with_local_os("not json") == {"os": platform.system()}
