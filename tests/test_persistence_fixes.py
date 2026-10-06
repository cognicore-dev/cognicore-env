"""Comprehensive tests for persistence and retrieval fixes (Items 1 & 2).

Verifies:
  1a. Path conflict — persistence_path as directory does not raise FileExistsError.
  1b. load() and save() signatures — accept an optional path consistently across backends.
  1c. save(path) — accepts either a directory or a file path without error.
  1d. Per-call writes — dirty tracking avoids unnecessary disk writes.
  1e. Atomic save — writes to temp file and renames, preventing corruption.
  1f. Fresh process persistence — verified across separate Python subprocesses.
  2a. Empty memory key — context['memory'] is populated via search(task, category=...).
  2b. README keys — context.get('experience'), 'relevant_memories', 'past_failures' work.
  2c. Task similarity recall — failures_to_avoid ranks by similarity to the current task.
"""

import json
import os
import subprocess
import sys
import time

import pytest

# ---------------------------------------------------------------------------
# Ensure the project root is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cognicore.runtime import CogniCoreRuntime, RuntimeConfig
from cognicore.memory.tfidf_backend import TFIDFMemoryBackend
from cognicore.memory.multihop_backend import MultiHopMemoryBackend
from cognicore.memory.embedding_backend import BasicEmbeddingBackend
from cognicore.memory.hybrid_backend import HybridMemoryBackend
from cognicore.memory.scoped import ScopedMemoryBackend, MemoryScope
from cognicore.memory.base import MemoryEntry, MemoryBackend


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def tmp_dir(tmp_path):
    """Yield a fresh temp directory and clean up after."""
    d = tmp_path / "cognicore_test"
    d.mkdir()
    yield str(d)


# ======================================================================
# 1a. Path conflict — FileExistsError
# ======================================================================

class TestPathConflict:
    """Ensure persistence_path is always treated as a directory."""

    def test_execute_does_not_crash_with_persistence(self, tmp_dir):
        """The original bug: first execute writes a file, second execute
        calls mkdir on that file -> FileExistsError."""
        cfg = RuntimeConfig(persistence_path=tmp_dir)
        rt = CogniCoreRuntime(config=cfg, name="crash-test")

        r1 = rt.execute(agent_fn=lambda t, c: "ok", task="task-1", category="a")
        r2 = rt.execute(agent_fn=lambda t, c: "ok", task="task-2", category="a")

        assert r1.success
        assert r2.success

    def test_memory_file_is_inside_directory(self, tmp_dir):
        """The backend's persistence_path must be a *file* inside the
        directory given as config.persistence_path."""
        cfg = RuntimeConfig(persistence_path=tmp_dir)
        rt = CogniCoreRuntime(config=cfg, name="path-check")

        rt.execute(agent_fn=lambda t, c: "ok", task="task-1", category="a")

        expected_file = os.path.join(tmp_dir, "path-check_memory.json")
        assert os.path.isfile(expected_file), (
            f"Expected memory file at {expected_file}, "
            f"got dir listing: {os.listdir(tmp_dir)}"
        )
        assert os.path.isdir(tmp_dir)


# ======================================================================
# 1b & 1c. load() / save(path) signatures & directory vs file handling
# ======================================================================

class TestLoadSaveSignatures:
    """Verify load and save accept a path (or none) consistently across backends."""

    def test_load_state_does_not_type_error(self, tmp_dir):
        cfg = RuntimeConfig(persistence_path=tmp_dir)
        rt1 = CogniCoreRuntime(config=cfg, name="sig-test")
        rt1.execute(agent_fn=lambda t, c: "ok", task="task-1", category="a")
        rt1._save_state()

        rt2 = CogniCoreRuntime(config=cfg, name="sig-test")
        assert rt2.backend.count() > 0, "Entries should have been loaded"

    def test_save_accepts_directory_or_file(self, tmp_dir):
        backend = TFIDFMemoryBackend()
        backend.store(MemoryEntry(text="entry 1", category="cat1"))

        # Save to directory
        dir_path = os.path.join(tmp_dir, "saved_dir")
        backend.save(dir_path)
        assert os.path.isfile(os.path.join(dir_path, "memory.json"))

        # Load from directory
        backend2 = TFIDFMemoryBackend()
        backend2.load(dir_path)
        assert backend2.count() == 1

        # Save to specific file
        file_path = os.path.join(tmp_dir, "specific_mem.json")
        backend.save(file_path)
        assert os.path.isfile(file_path)

        # Load from specific file
        backend3 = TFIDFMemoryBackend()
        backend3.load(file_path)
        assert backend3.count() == 1

    def test_all_backends_accept_path_signature(self, tmp_dir):
        """All backends should accept save(path) and load(path) without TypeError."""
        class DummyProvider:
            def embed(self, text): return [0.1, 0.2]
            def embed_batch(self, texts): return [[0.1, 0.2] for _ in texts]
            @property
            def dimension(self): return 2

        backends = [
            TFIDFMemoryBackend(),
            MultiHopMemoryBackend(),
            BasicEmbeddingBackend(provider=DummyProvider()),
            HybridMemoryBackend(),
            ScopedMemoryBackend(TFIDFMemoryBackend(), scope=MemoryScope.GLOBAL, scope_id="global"),
        ]
        dummy_file = os.path.join(tmp_dir, "backend_test.json")
        for b in backends:
            # Both with path and without path should execute cleanly
            b.save(dummy_file)
            b.save()
            b.load(dummy_file)
            b.load()


# ======================================================================
# 1d. Per-call writes / dirty tracking
# ======================================================================

class TestDirtyTracking:
    """Verify that save() is only performed when state has changed."""

    def test_store_sets_dirty(self):
        backend = TFIDFMemoryBackend()
        assert not backend._dirty
        backend.store(MemoryEntry(text="hello", category="test"))
        assert backend._dirty

    def test_save_clears_dirty(self, tmp_dir):
        path = os.path.join(tmp_dir, "mem.json")
        backend = TFIDFMemoryBackend(persistence_path=path)
        backend.store(MemoryEntry(text="hello", category="test"))
        assert backend._dirty
        backend.save()
        assert not backend._dirty

    def test_save_skips_when_clean(self, tmp_dir):
        path = os.path.join(tmp_dir, "mem.json")
        backend = TFIDFMemoryBackend(persistence_path=path)
        backend.store(MemoryEntry(text="hello", category="test"))
        backend.save()

        mtime_after_first = os.path.getmtime(path)
        time.sleep(0.05)

        # Second save with no changes — file should NOT be rewritten
        backend.save()
        mtime_after_second = os.path.getmtime(path)
        assert mtime_after_first == mtime_after_second

    def test_force_save_writes_even_when_clean(self, tmp_dir):
        path = os.path.join(tmp_dir, "mem.json")
        backend = TFIDFMemoryBackend(persistence_path=path)
        backend.store(MemoryEntry(text="hello", category="test"))
        backend.save()
        mtime1 = os.path.getmtime(path)
        time.sleep(0.05)
        backend.save(force=True)
        mtime2 = os.path.getmtime(path)
        assert mtime2 > mtime1


# ======================================================================
# 1e. Atomic save
# ======================================================================

class TestAtomicSave:
    """Verify save uses temp+rename and leaves no temp files on success."""

    def test_no_temp_files_after_save(self, tmp_dir):
        path = os.path.join(tmp_dir, "mem.json")
        backend = TFIDFMemoryBackend(persistence_path=path)
        for i in range(10):
            backend.store(MemoryEntry(text=f"entry-{i}", category="test"))
        backend.save()

        files = os.listdir(tmp_dir)
        assert files == ["mem.json"], f"Unexpected files: {files}"

    def test_saved_file_is_valid_json(self, tmp_dir):
        path = os.path.join(tmp_dir, "mem.json")
        backend = TFIDFMemoryBackend(persistence_path=path)
        backend.store(MemoryEntry(text="important", category="test"))
        backend.save()

        with open(path) as f:
            data = json.load(f)
        assert len(data["entries"]) == 1
        assert data["entries"][0]["text"] == "important"


# ======================================================================
# 1f. Fresh process persistence
# ======================================================================

class TestFreshProcessPersistence:
    """Run code in an entirely fresh OS process via subprocess to guarantee round-trip."""

    def test_fresh_process_round_trip(self, tmp_dir):
        code_write = f"""
import sys
sys.path.insert(0, r"{os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))}")
from cognicore.runtime import CogniCoreRuntime, RuntimeConfig

cfg = RuntimeConfig(persistence_path=r"{tmp_dir}")
rt = CogniCoreRuntime(config=cfg, name="fresh_proc")
rt.execute(lambda t, c: "fixed", task="Fix race condition", category="concurrency")
rt.save()
"""
        code_read = f"""
import sys
sys.path.insert(0, r"{os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))}")
from cognicore.runtime import CogniCoreRuntime, RuntimeConfig

cfg = RuntimeConfig(persistence_path=r"{tmp_dir}")
rt = CogniCoreRuntime(config=cfg, name="fresh_proc")
assert rt.backend.count() >= 1, f"Expected entries, found {{rt.backend.count()}}"
entry = rt.backend.get_all()[0]
assert "race" in entry.text.lower(), f"Unexpected text: {{entry.text}}"
print("SUCCESS")
"""
        # Run process 1: write
        proc1 = subprocess.run([sys.executable, "-c", code_write], capture_output=True, text=True)
        assert proc1.returncode == 0, f"Process 1 failed: {proc1.stderr}"

        # Run process 2: read in fresh process
        proc2 = subprocess.run([sys.executable, "-c", code_read], capture_output=True, text=True)
        assert proc2.returncode == 0, f"Process 2 failed: {proc2.stderr}"
        assert "SUCCESS" in proc2.stdout


# ======================================================================
# 2a, 2b, 2c. Context retrieval, aliases, and similarity ranking
# ======================================================================

class TestContextRetrieval:
    """Verify context population, README compatibility keys, and ranking."""

    def test_memory_populated_after_store(self, tmp_dir):
        cfg = RuntimeConfig(persistence_path=tmp_dir)
        rt = CogniCoreRuntime(config=cfg, name="ctx-test")

        rt.backend.store(MemoryEntry(
            text="Always use parameterized queries to prevent SQL injection",
            category="security",
        ))

        ctx = rt._build_context("security", task="Prevent SQL injection")
        assert len(ctx["memory"]) > 0, "Expected at least one memory entry in context['memory']"
        assert rt.stats.memory_retrievals > 0

    def test_readme_compatibility_aliases(self, tmp_dir):
        """Verify context.get('experience'), 'relevant_memories', and 'past_failures'."""
        cfg = RuntimeConfig(persistence_path=tmp_dir)
        rt = CogniCoreRuntime(config=cfg, name="alias-test")

        rt.backend.store(MemoryEntry(
            text="Handle None before calling strip",
            category="null_safety",
            correct=False,
            action="call_strip_directly",
        ))

        ctx = rt._build_context("null_safety", task="Fix AttributeError on None strip")

        # Check all documented / user-facing keys
        assert ctx.get("experience") == ctx["memory"]
        assert ctx.get("relevant_memories") == ctx["memory"]
        assert ctx.get("past_failures") == ctx["failures_to_avoid"]
        assert len(ctx.get("experience")) > 0

    def test_failures_to_avoid_ranked_by_task_similarity(self, tmp_dir):
        """failures_to_avoid should rank failures similar to the task higher than unrelated failures."""
        cfg = RuntimeConfig(persistence_path=tmp_dir)
        rt = CogniCoreRuntime(config=cfg, name="sim-rank-test")

        # Two failures in same category: one about timeout, one about memory leak
        rt.backend.store(MemoryEntry(
            text="Socket timeout error occurred when connecting to DB",
            category="backend_bug",
            correct=False,
            action="retry_without_backoff",
        ))
        rt.backend.store(MemoryEntry(
            text="Out of memory leak occurred during file buffer streaming",
            category="backend_bug",
            correct=False,
            action="load_entire_file_to_ram",
        ))

        # Query specifically about timeout
        ctx_timeout = rt._build_context("backend_bug", task="Fix database socket timeout crash")
        assert len(ctx_timeout["failures_to_avoid"]) >= 1
        # The timeout failure must be ranked FIRST
        assert ctx_timeout["failures_to_avoid"][0] == "retry_without_backoff"

        # Query specifically about memory buffer
        ctx_mem = rt._build_context("backend_bug", task="Fix memory buffer streaming leak")
        assert len(ctx_mem["failures_to_avoid"]) >= 1
        # The memory failure must be ranked FIRST
        assert ctx_mem["failures_to_avoid"][0] == "load_entire_file_to_ram"

    def test_successful_patterns_populated(self, tmp_dir):
        cfg = RuntimeConfig(persistence_path=tmp_dir)
        rt = CogniCoreRuntime(config=cfg, name="success-test")

        rt.backend.store(MemoryEntry(
            text="Good approach: extract helper function",
            category="refactor",
            correct=True,
            action="extract_helper",
        ))

        ctx = rt._build_context("refactor", task="Refactor the utils module")
        assert "extract_helper" in ctx["successful_patterns"]
