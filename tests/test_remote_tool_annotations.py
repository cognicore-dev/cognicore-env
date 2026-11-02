"""Every hosted MCP tool must declare a title and behaviour hints.

Clients use the hints to decide when to ask the user before a call, and the
Claude connector directory requires a title plus readOnlyHint/destructiveHint
on every tool. A new tool without them fails here.
"""
import asyncio

from cognicore.connectors import remote

TOOLS = {t.name: t for t in asyncio.run(remote.mcp.list_tools())}


def test_every_tool_has_title_and_hints():
    missing = [
        name for name, t in TOOLS.items()
        if not t.title or t.annotations is None
        or t.annotations.readOnlyHint is None or t.annotations.destructiveHint is None
    ]
    assert not missing, f"tools without title/annotations: {missing}"


def test_read_only_and_destructive_are_exclusive():
    both = [n for n, t in TOOLS.items() if t.annotations.readOnlyHint and t.annotations.destructiveHint]
    assert not both


def test_deleting_tools_are_marked_destructive():
    assert TOOLS["cognicore_forget"].annotations.destructiveHint is True


def test_writing_tools_are_not_marked_read_only():
    for name in ("cognicore_remember", "cognicore_save_session", "cognicore_record_experience",
                 "cognicore_purchase_memory", "cognicore_share_experience", "cognicore_figma_sync"):
        assert TOOLS[name].annotations.readOnlyHint is False, name


def test_tools_calling_external_apis_are_open_world():
    for name in ("cognicore_figma_sync", "cognicore_fabric_sync_figma"):
        assert TOOLS[name].annotations.openWorldHint is True, name


def _untyped_params(schema, path=""):
    """Yield schema paths where a property declares no type, anyOf or $ref."""
    for key, prop in (schema.get("properties") or {}).items():
        if not any(k in prop for k in ("type", "anyOf", "oneOf", "allOf", "$ref")):
            yield f"{path}{key}"


def test_every_tool_parameter_declares_a_type():
    """The directory portal flags a parameter with no type ("Add a type to this parameter")."""
    untyped = {
        name: list(_untyped_params(t.inputSchema))
        for name, t in TOOLS.items()
    }
    untyped = {n: p for n, p in untyped.items() if p}
    assert not untyped, f"tool parameters without a type: {untyped}"
