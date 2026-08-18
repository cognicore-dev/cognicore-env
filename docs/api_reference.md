# CogniCore API Reference

> **Version:** 0.9.5 | **License:** MIT | **Python:** 3.10+

---

## Table of Contents

- [CogniCoreRuntime](#cognicoreruntime)
- [Memory System](#memory-system)
  - [MemoryBackend (ABC)](#memorybackend)
  - [MemoryEntry](#memoryentry)
  - [SearchResult](#searchresult)
  - [TFIDFMemoryBackend](#tfidfmemorybackend)
- [Middleware](#middleware)
  - [ReflectionEngine](#reflectionengine)
  - [RewardBuilder](#rewardbuilder)
  - [ProposeReviseProtocol](#proposereviseprotocol)
- [Environments](#environments)
- [CLI Commands](#cli-commands)

---

## CogniCoreRuntime

**Module:** `cognicore.runtime`

The main entry point for application developers. Wraps any callable agent function with persistent memory and reflection.

### Constructor

```python
from cognicore import CogniCoreRuntime

runtime = CogniCoreRuntime(
    config=RuntimeConfig(),   # Optional configuration
    name="my-runtime",        # Instance name for logging
    memory=None,              # Custom MemoryBackend (default: TFIDFMemoryBackend)
)
```

### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `execute()` | `(agent_fn, task, category, evaluator?, max_retries?) → ExecutionResult` | Execute with memory + reflection |
| `wrap()` | `(category, evaluator?, max_retries?) → decorator` | Decorator API for wrapping functions |
| `analyze()` | `() → dict` | Per-category failure analysis |

### Example

```python
from cognicore import CogniCoreRuntime

runtime = CogniCoreRuntime()

# Decorator style
@runtime.wrap(category="code_fix")
def fix_code(task, context):
    hint = context.get("reflection_hint", "")
    failures = context.get("failures_to_avoid", [])
    return llm.generate(f"{task}\nAvoid: {failures}\n{hint}")

result = fix_code("Fix the login bug")

# Direct style
result = runtime.execute(
    agent_fn=lambda task, ctx: "fixed",
    task="Fix bug #123",
    category="code_fix",
    evaluator=lambda out, task: out == "fixed",
    max_retries=2,
)
```

---

## Memory System

### MemoryBackend

**Module:** `cognicore.memory.base`

Abstract base class for all memory storage backends.

```python
from cognicore.memory.base import MemoryBackend, MemoryEntry, SearchResult
```

| Abstract Method | Signature | Description |
|----------------|-----------|-------------|
| `store()` | `(entry: MemoryEntry) → str` | Store entry, return entry_id |
| `search()` | `(query, top_k=5, category?, scope?, scope_id?) → List[SearchResult]` | Semantic search |
| `get_by_category()` | `(category, top_k=5, success_filter?) → List[MemoryEntry]` | Exact category retrieval |
| `count()` | `() → int` | Total entries |
| `clear()` | `() → None` | Delete all entries |

**Implementations:**

| Backend | Module | Dependencies | Best For |
|---------|--------|-------------|----------|
| `TFIDFMemoryBackend` | `cognicore.memory.tfidf_backend` | None | Default, lightweight |
| `SQLiteMemoryBackend` | `cognicore.memory.sqlite_backend` | None (stdlib) | Persistent, concurrent |
| `BasicEmbeddingBackend` | `cognicore.memory.embedding_backend` | sentence-transformers | Semantic accuracy |
| `GraphMemoryBackend` | `cognicore.memory.graph_backend` | None | Relationship tracking |

### MemoryEntry

**Module:** `cognicore.memory.base`

Canonical data type for all memory entries. **This is a dataclass — use attribute access, not dict access.**

```python
from cognicore.memory.base import MemoryEntry, MemoryScope

entry = MemoryEntry(
    text="Fix the login bug",          # Human-readable description
    category="code_fix",               # Grouping key
    correct=True,                      # Success/failure flag
    action="patched auth module",      # What the agent did
    scope=MemoryScope.GLOBAL,          # Isolation scope
    scope_id="",                       # Scope identifier
    metadata={"duration_ms": 1200},    # Arbitrary metadata
)

# Serialization
d = entry.to_dict()
entry2 = MemoryEntry.from_dict(d)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `text` | `str` | required | Description text |
| `category` | `str` | `"general"` | Category for grouping |
| `correct` | `bool?` | `None` | Whether outcome was correct |
| `action` | `str` | `""` | Agent's action/output |
| `scope` | `MemoryScope` | `GLOBAL` | Isolation scope |
| `metadata` | `dict` | `{}` | Arbitrary metadata |
| `entry_id` | `str` | auto | Unique ID (set by backend) |
| `timestamp` | `float` | auto | Unix timestamp (set by backend) |
| `relevance` | `float` | `1.0` | Decay-adjusted relevance |

### SearchResult

```python
@dataclass
class SearchResult:
    entry: MemoryEntry
    score: float          # Similarity score
    source: str = ""      # "semantic", "category", "graph", "temporal"
```

### TFIDFMemoryBackend

**Module:** `cognicore.memory.tfidf_backend`

Default zero-dependency backend using TF-IDF cosine similarity.

```python
from cognicore.memory.tfidf_backend import TFIDFMemoryBackend

backend = TFIDFMemoryBackend(
    max_size=10_000,              # Max entries before eviction
    decay_rate=0.95,              # Relevance decay per step
    similarity_threshold=0.01,    # Min similarity for search results
    persistence_path="memory.json",  # Auto-save/load path
)
```

---

## Middleware

### ReflectionEngine

**Module:** `cognicore.middleware.reflection`

Analyzes past failures to generate hints and action overrides.

```python
from cognicore.middleware.reflection import ReflectionEngine

engine = ReflectionEngine(
    memory=backend,              # Any MemoryBackend
    min_samples=2,               # Min entries before generating hints
    failure_threshold=2,         # Min failures to flag an action
)

analysis = engine.analyze("code_fix")
# → {"n_similar": 10, "good_predictions": {...}, "bad_predictions": {...}, "recommendation": "..."}

hint = engine.get_hint("code_fix")
# → "Reflection: In similar 'code_fix' tasks, action 'skip tests' was wrong 3 times. Consider 'run tests' instead."

action, source, confidence = engine.suggest_override("code_fix", "skip tests")
# → ("run tests", "reflection_override", 0.75)
```

### RewardBuilder

**Module:** `cognicore.middleware.rewards`

Builds 8-component structured rewards from evaluation results.

```python
from cognicore.middleware.rewards import RewardBuilder

reward = builder.build(
    eval_result,
    streak_penalty=-0.1,
    is_novel_group=True,
    confidence=0.9,
)
# reward.total → sum of all 8 components
# reward.base_score, reward.memory_bonus, reward.reflection_bonus, etc.
```

### ProposeReviseProtocol

**Module:** `cognicore.middleware.propose_revise`

Two-phase decision protocol: propose an action, get feedback, then commit.

```python
from cognicore.middleware.propose_revise import ProposeReviseProtocol

protocol.begin_step()
feedback = protocol.propose({"classification": "SAFE"}, "security")
# feedback.memory_context, feedback.reflection_hint, feedback.confidence_estimate

# Agent revises based on feedback...
improved = protocol.check_improvement("UNSAFE", eval_correct=True)
protocol.end_step()
```

---

## Environments

Create environments with `cognicore.make()`:

```python
import cognicore

env = cognicore.make("SafetyClassification-v1", difficulty="easy")
obs = env.reset()
obs, reward, done, truncated, info = env.step({"classification": "SAFE"})

# Available environments
envs = cognicore.list_envs()
```

| Environment | Description |
|-------------|-------------|
| `SafetyClassification-v1` | Classify AI responses as SAFE/UNSAFE |
| `CodeDebugging-v1` | Find and categorize bugs in code |
| `Planning-v1` | Multi-step task planning |
| `RealWorldSafety-v1` | Production safety scenarios |
| `RealWorldCodeBugs-v1` | Real production Python bugs |
| `WorkflowAgent-v1` | Multi-step workflow execution |
| `ConversationGuard-v1` | Conversational safety |

Each environment supports `difficulty` of `"easy"`, `"medium"`, or `"hard"`.

---

## CLI Commands

```bash
# Core
cognicore doctor                    # Check installation health
cognicore list                      # List registered environments

# Benchmarks (zero heavy deps)
cognicore bench run                 # Full benchmark suite
cognicore bench run --quick         # Quick run (easy, 2 episodes)
cognicore bench compare             # Check for regressions
cognicore bench report              # Show last results

# Training (requires SB3 + torch)
cognicore train --env SafetyClassification-v1 --algo PPO --steps 50000
cognicore benchmark --env GridWorld-v0
cognicore arena --envs MazeRunner-v0,GridWorld-v0

# Servers
cognicore ui                        # Start NEXUS dashboard
cognicore mcp serve                 # Start MCP server
cognicore webhooks                  # Start webhook server

# Integrations
cognicore integrations setup        # Setup wizard
cognicore integrations test         # Test connections
```
