"""
CogniCore MCP Server — Model Context Protocol integration.

Exposes CogniCore's memory, reflection, and threat detection as MCP tools
that any MCP-compatible client (Claude, Cursor, Windsurf, etc.) can use.

Usage::

    cognicore mcp serve
    # or
    python -m cognicore.mcp.server
"""

import json
import os
import sys
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

from cognicore.memory.context_preservation import (
    TokenBudget,
    compress_context,
    save_session,
    resume_session,
    handle_token_triggers,
)

logger = logging.getLogger("cognicore.mcp")

# ---------------------------------------------------------------------------
# MCP SDK import (optional dependency)
# ---------------------------------------------------------------------------
try:
    from mcp.server.fastmcp import FastMCP

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

# ---------------------------------------------------------------------------
# Lazy-initialized singletons
# ---------------------------------------------------------------------------
_runtime = None
_reflection = None
_detector = None
_data_dir: Optional[Path] = None

# Internal counters for stats
_stats = {
    "total_stored": 0,
    "total_recalled": 0,
    "total_reflections": 0,
    "total_scans": 0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_data_dir() -> Path:
    """Return (and create) the persistence directory.

    Reads ``COGNICORE_DATA_DIR`` from the environment.  Falls back to
    ``~/.cognicore/mcp``.
    """
    global _data_dir
    if _data_dir is not None:
        return _data_dir

    raw = os.environ.get("COGNICORE_DATA_DIR", "")
    if raw:
        _data_dir = Path(raw)
    else:
        _data_dir = Path.home() / ".cognicore" / "mcp"

    _data_dir.mkdir(parents=True, exist_ok=True)
    logger.info("CogniCore MCP data dir: %s", _data_dir)
    return _data_dir


def _ensure_initialized():
    """Lazy-initialize all CogniCore components on first tool call."""
    global _runtime, _reflection, _detector

    if _runtime is not None:
        return

    from cognicore.runtime import CogniCoreRuntime, RuntimeConfig
    
    path = _get_data_dir() / "mcp_memories.db"
    _runtime = CogniCoreRuntime(
        config=RuntimeConfig(persistence_path=str(path), memory_backend="sqlite"),
        name="mcp-server"
    )

    from cognicore.middleware.reflection import ReflectionEngine
    _reflection = ReflectionEngine(_runtime.memory)

    from cognicore.immune.detector import ThreatDetector
    _detector = ThreatDetector()

    logger.info("CogniCore MCP components initialized")


def _entry_from_parts(
    task: str,
    category: str,
    success: bool,
    action: str = "",
    error: str = "",
    metadata: Optional[dict] = None,
) -> dict:
    """Build the dict shape expected by Memory and SemanticMemory."""
    text = f"{task} {action} {error}".strip()
    return {
        "task": task,
        "category": category,
        "success": success,
        "correct": success,
        "action": action,
        "error": error,
        "metadata": metadata or {},
        "text": text,
    }


# Sent to every client at initialize. Clients that show server instructions to the model
# (and agents without CogniCore hooks, such as Codex) otherwise never think to resume:
# in a Claude Code -> Codex test, Codex had the tools but never called them.
MEMORY_INSTRUCTIONS = (
    "Persistent memory shared across sessions and agents (Claude, Codex, Cursor, ChatGPT). "
    "At the start of a task, or whenever the user says to continue earlier work, call "
    "cognicore_resume_session with a short query describing the task. It returns the previous "
    "session's goal, state, open tasks, decisions and exact values. Before you finish, call "
    "cognicore_save_session with handoff_json (goal, current_state, decisions with why, "
    "open_tasks, key_facts, files), keeping ids, versions, names and numbers verbatim."
)


def strip_schema_titles(server: Any) -> None:
    """Drop the auto-generated "title" annotations from every tool's input schema.

    pydantic adds one per parameter ("Session Name", "Environment Json") and one
    per schema. They only restate the property name, yet clients load every tool
    schema into the model's context on every turn: about 20% of all tool tokens.
    Arguments are still validated by FastMCP's own model, which is untouched.
    The tool-level ``title`` annotation (shown to users) is not affected.
    """
    def strip(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("title"), str):  # a property *named* "title" maps to a dict, and is kept
                del node["title"]
            for value in node.values():
                strip(value)
        elif isinstance(node, list):
            for value in node:
                strip(value)

    for tool in getattr(getattr(server, "_tool_manager", None), "_tools", {}).values():
        strip(getattr(tool, "parameters", None))


def limit_toolsets(server: Any, spec: str, groups: Dict[str, List[str]]) -> List[str]:
    """Keep only the tools in the named groups; return the names removed.

    ``spec`` is a comma-separated list such as "memory,session". Empty or "all"
    keeps everything. Every tool definition is loaded into the model's context on
    every turn, so unused groups cost tokens all the time. Tools not listed in
    any group are always kept.
    """
    wanted = {s.strip().lower() for s in (spec or "").split(",") if s.strip()}
    if not wanted or "all" in wanted:
        return []
    unknown = wanted - set(groups)
    if unknown:
        logger.warning("Unknown COGNICORE_TOOLSETS entries ignored: %s", ", ".join(sorted(unknown)))
    drop = {name for group, names in groups.items() if group not in wanted for name in names}
    tools = getattr(getattr(server, "_tool_manager", None), "_tools", {})
    removed = [name for name in list(tools) if name in drop]
    for name in removed:
        del tools[name]
    return removed


def _with_local_os(environment_json: str) -> Dict[str, Any]:
    """Parse an environment fingerprint and fill in the OS.

    The stdio server runs on the user's machine, so its OS is the agent's OS.
    (The hosted server cannot know this and leaves it to the client.)
    """
    import platform

    try:
        env = json.loads(environment_json) if environment_json else {}
    except json.JSONDecodeError:
        env = {}
    if not isinstance(env, dict):
        env = {}
    env.setdefault("os", platform.system())
    return env


def _state_path() -> Path:
    """Path to the JSON file that holds serialised memory."""
    return _get_data_dir() / "cognicore_memory.json"



# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_mcp_server() -> "FastMCP":
    """Create and return a configured :class:`FastMCP` instance.

    Raises:
        ImportError: If the ``mcp`` package is not installed.

    Returns:
        A ready-to-run ``FastMCP`` server with all CogniCore tools registered.

    Example::

        server = create_mcp_server()
        server.run(transport="stdio")
    """
    if not _MCP_AVAILABLE:
        raise ImportError(
            "The 'mcp' package is required for MCP server support. "
            "Install it with:  pip install mcp"
        )

    mcp = FastMCP("cognicore", instructions=MEMORY_INSTRUCTIONS)

    # ------------------------------------------------------------------
    # Tool 1: cognicore_remember
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_remember(
        task: str,
        category: str,
        success: bool,
        action: str = "",
        error: str = "",
        metadata: str = "",
    ) -> str:
        """Store an execution outcome in CogniCore memory.

        Args:
            task: Description of the task that was attempted.
            category: Category / domain (e.g. 'code_review', 'deployment').
            success: Whether the task succeeded.
            action: The action that was taken.
            error: Error message if the task failed.
            metadata: Optional JSON string with extra key-value pairs.

        Returns:
            Confirmation message.
        """
        _ensure_initialized()

        meta_dict = {}
        if metadata:
            try:
                meta_dict = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                meta_dict = {"raw": metadata}

        from cognicore.memory.base import MemoryEntry
        entry = MemoryEntry(
            text=f"{task} {action} {error}".strip(),
            category=category,
            correct=success,
            action=action,
            metadata={
                "task": task,
                "error": error,
                **meta_dict
            }
        )
        _runtime.memory.store(entry)

        _stats["total_stored"] += 1

        outcome = "SUCCESS" if success else "FAILURE"
        lines = [
            f"Stored [{outcome}] in category '{category}'.",
            f"  Task   : {task}",
        ]
        if action:
            lines.append(f"  Action : {action}")
        if error:
            lines.append(f"  Error  : {error}")
        lines.append(f"  Total memories: {_stats['total_stored']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool 2: cognicore_recall
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_recall(
        query: str,
        category: str = "",
        top_k: int = 5,
    ) -> str:
        """Retrieve relevant past experiences from CogniCore memory.

        Uses semantic search (TF-IDF) when *query* is provided.  Falls
        back to category-based retrieval when only *category* is given.

        Args:
            query: Free-text search query.
            category: Optional category filter.
            top_k: Maximum number of results to return.

        Returns:
            Formatted list of matching memories.
        """
        _ensure_initialized()
        _stats["total_recalled"] += 1

        results = []

        # Prefer semantic search when a query is provided
        if query:
            try:
                hits = _runtime.memory.search(query, top_k=top_k, category=category if category else None)
                for hit in hits:
                    results.append((hit.score, hit.entry))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Semantic search failed: %s — falling back", exc)

        # Fall back / supplement with category-based retrieval
        if not results and category:
            try:
                ctx = _runtime.memory.get_by_category(category, top_k=top_k)
                if isinstance(ctx, list):
                    for item in ctx[:top_k]:
                        results.append((1.0, item))
                elif ctx:
                    results.append((1.0, ctx))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Category recall failed: %s", exc)

        if not results:
            return f"No memories found for query='{query}', category='{category}'."

        lines = [f"Found {len(results)} relevant memories:\n"]
        for idx, (score, doc) in enumerate(results, 1):
            if hasattr(doc, "correct"):
                outcome = "SUCCESS" if doc.correct else "FAILURE"
                lines.append(f"  {idx}. [{outcome}] (relevance {score:.2f})")
                lines.append(f"     Task    : {doc.metadata.get('task', doc.text)}")
                if getattr(doc, "action", None):
                    lines.append(f"     Action  : {doc.action}")
                if doc.metadata.get("error"):
                    lines.append(f"     Error   : {doc.metadata['error']}")
                if getattr(doc, "category", None):
                    lines.append(f"     Category: {doc.category}")
            else:
                lines.append(f"  {idx}. (relevance {score:.2f}) {doc}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool 3: cognicore_recall_failures
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_recall_failures(
        category: str,
        top_k: int = 5,
    ) -> str:
        """Retrieve past failures for a category.

        Useful for learning what NOT to do.

        Args:
            category: The category to search in.
            top_k: Maximum number of failures to return.

        Returns:
            Formatted list of past failures.
        """
        _ensure_initialized()
        _stats["total_recalled"] += 1

        try:
            failures = _runtime.memory.get_by_category(category, top_k=top_k, success_filter=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("retrieve_failures error: %s", exc)
            return f"Could not retrieve failures for '{category}': {exc}"

        if not failures:
            return f"No recorded failures in category '{category}'."

        failures = failures[:top_k]
        lines = [
            f"Found {len(failures)} past failures in '{category}' — avoid these:\n"
        ]
        for idx, fail in enumerate(failures, 1):
            lines.append(f"  {idx}. Task  : {fail.metadata.get('task', fail.text)}")
            if fail.action:
                lines.append(f"     Action: {fail.action}")
            if fail.metadata.get("error"):
                lines.append(f"     Error : {fail.metadata['error']}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool 4: cognicore_recall_successes
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_recall_successes(
        category: str,
        top_k: int = 5,
    ) -> str:
        """Retrieve past successes for a category.

        Useful for learning what worked well.

        Args:
            category: The category to search in.
            top_k: Maximum number of successes to return.

        Returns:
            Formatted list of past successes.
        """
        _ensure_initialized()
        _stats["total_recalled"] += 1

        try:
            successes = _runtime.memory.get_by_category(category, top_k=top_k, success_filter=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("retrieve_successes error: %s", exc)
            return f"Could not retrieve successes for '{category}': {exc}"

        if not successes:
            return f"No recorded successes in category '{category}'."

        successes = successes[:top_k]
        lines = [
            f"Found {len(successes)} past successes in '{category}' — replicate these:\n"
        ]
        for idx, succ in enumerate(successes, 1):
            lines.append(f"  {idx}. Task  : {succ.metadata.get('task', succ.text)}")
            if succ.action:
                lines.append(f"     Action: {succ.action}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool 5: cognicore_reflect
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_reflect(category: str) -> str:
        """Analyze behavioural patterns and produce a recommendation.

        Uses the CogniCore ReflectionEngine to identify recurring good
        and bad patterns, then offers a concrete hint.

        Args:
            category: The category to analyze.

        Returns:
            Pattern analysis and actionable recommendation.
        """
        _ensure_initialized()
        _stats["total_reflections"] += 1

        try:
            analysis = _reflection.analyze(category)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reflection analysis failed: %s", exc)
            return f"Reflection analysis failed for '{category}': {exc}"

        try:
            hint = _reflection.get_hint(category)
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_hint failed: %s", exc)
            hint = None

        lines = [f"Reflection for category '{category}':\n"]

        if isinstance(analysis, dict):
            good = analysis.get("good_patterns", analysis.get("good_predictions", {}))
            bad = analysis.get("bad_patterns", analysis.get("bad_predictions", {}))
            if good:
                lines.append("  Good patterns:")
                for pattern, count in good.items():
                    lines.append(f"    + {pattern}: {count}")
            if bad:
                lines.append("  Bad patterns:")
                for pattern, count in bad.items():
                    lines.append(f"    - {pattern}: {count}")
            if not good and not bad:
                lines.append("  No strong patterns detected yet.")
            if analysis.get("recommendation"):
                lines.append(f"  Learned recommendation: {analysis['recommendation']}")
        else:
            lines.append(f"  Analysis: {analysis}")

        if hint:
            lines.append("")
            lines.append(f"  Recommendation: {hint}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool 6: cognicore_scan_threat
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_scan_threat(text: str) -> str:
        """Scan text for prompt injection, jailbreak, or data exfiltration.

        Uses CogniCore's immune-system ThreatDetector.

        Args:
            text: The text to scan.

        Returns:
            Threat assessment with score, category, and indicators.
        """
        _ensure_initialized()
        _stats["total_scans"] += 1

        try:
            result = _detector.detect(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Threat detection failed: %s", exc)
            return f"Threat detection error: {exc}"

        if isinstance(result, dict):
            score = result.get("score", result.get("threat_score", 0.0))
            category = result.get("category", result.get("threat_type", "unknown"))
            indicators = result.get("indicators", result.get("details", []))
        else:
            score = getattr(result, "score", 0.0)
            category = getattr(result, "category", "unknown")
            indicators = getattr(result, "indicators", [])

        if score > 0.7:
            level = "HIGH"
        elif score > 0.4:
            level = "MEDIUM"
        else:
            level = "LOW"

        lines = [
            f"Threat scan result: {level}",
            f"  Score    : {score:.2f}",
            f"  Category : {category}",
        ]
        if indicators:
            lines.append("  Indicators:")
            if isinstance(indicators, list):
                for ind in indicators:
                    lines.append(f"    - {ind}")
            else:
                lines.append(f"    - {indicators}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool 7: cognicore_stats
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_stats() -> str:
        """Return runtime statistics for the CogniCore MCP server.

        Returns:
            Formatted statistics including memory size, totals, and
            persistence location.
        """
        _ensure_initialized()

        mem_size = 0
        if hasattr(_runtime.memory, "entries"):
            mem_size = len(_runtime.memory.entries)
        elif hasattr(_runtime.memory, "episodes"):
            mem_size = len(_runtime.memory.episodes)
        elif hasattr(_runtime.memory, "_episodes"):
            mem_size = len(_runtime.memory._episodes)

        sem_size = 0
        if hasattr(_runtime.backend, "entries"):
            sem_size = len(_runtime.backend.entries)
        elif hasattr(_runtime.backend, "_documents"):
            sem_size = len(_runtime.backend._documents)
        elif hasattr(_runtime.backend, "documents"):
            sem_size = len(_runtime.backend.documents)

        lines = [
            "CogniCore MCP Server Statistics:",
            f"  Data directory    : {_get_data_dir()}",
            f"  Episodic memories : {mem_size}",
            f"  Semantic documents: {sem_size}",
            f"  Total stored      : {_stats['total_stored']}",
            f"  Total recalled    : {_stats['total_recalled']}",
            f"  Total reflections : {_stats['total_reflections']}",
            f"  Total scans       : {_stats['total_scans']}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool 8: cognicore_compress_context
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_compress_context(
        conversation: List[Dict[str, Any]],
        keep_last_n: int = 5,
    ) -> str:
        """Compress messages older than the last keep_last_n into a dense stored summary (no LLM calls). Returns JSON with the summary and token counts."""
        _ensure_initialized()
        return compress_context(_runtime.backend, conversation, keep_last_n=keep_last_n)

    # ------------------------------------------------------------------
    # Tool 9: cognicore_save_session
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_save_session(
        conversation: Optional[List[Dict[str, Any]]] = None,
        session_name: str = "",
        agent: str = "",
        goal: str = "",
        current_state: str = "",
        environment_json: str = "",
        handoff_json: str = "",
    ) -> str:
        """Save work so any agent can resume it later. Cheapest: send handoff_json (written by you) instead of the full conversation.

        handoff_json: {"goal","current_state","decisions":[str|{"what","why"}],"open_tasks":[str],"key_facts":[str],"files":[str]}. Keep ids, versions, paths and numbers verbatim; prefix rejected approaches with "Rejected:".
        environment_json: {"os","python_version","framework","framework_version","dependencies":{"name":"ver"}}.
        agent: e.g. "claude-code". Secrets are redacted; saving the same content twice is a no-op."""
        _ensure_initialized()
        return save_session(
            _runtime.backend, conversation, session_name=session_name if session_name else None,
            agent=agent, goal=goal, current_state=current_state,
            environment=_with_local_os(environment_json), supplied_handoff=handoff_json or None,
        )

    # ------------------------------------------------------------------
    # Tool 10: cognicore_resume_session
    # ------------------------------------------------------------------
    @mcp.tool()
    def cognicore_resume_session(
        query: str = "",
        last_n_sessions: int = 3,
        include_action_items: bool = True,
        max_tokens: int = 0,
        format: str = "text",
        session_id: str = "",
        section: str = "",
        environment_json: str = "",
    ) -> str:
        """Start of a conversation: resume past work, most important first (goal, state, open tasks, decisions, facts).

        query: picks the matching session, else the latest. max_tokens: budget (default 1500); omitted items are listed, fetch them with session_id + section. format: "text" or "json". environment_json: your environment, to get mismatch warnings."""
        _ensure_initialized()
        return resume_session(
            _runtime.backend,
            query=query if query else None,
            last_n_sessions=last_n_sessions,
            include_action_items=include_action_items,
            max_tokens=max_tokens or None,
            format=format,
            session_id=session_id or None,
            section=section or None,
            environment=_with_local_os(environment_json) if environment_json else None,
        )

    strip_schema_titles(mcp)
    return mcp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    """Launch the CogniCore MCP server over stdio transport.

    This is the main entry point used by ``cognicore mcp serve`` or by
    running this module directly.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Starting CogniCore MCP server …")
    server = create_mcp_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
