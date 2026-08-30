"""
CogniCore — Embedding-Based Memory Retrieval.

Upgrades the simple string-matching memory to use sentence embeddings
for semantic similarity. Agents retrieve memories by MEANING, not keywords.

This is CogniCore's core differentiator:
  - Agent hits a dead end → memory stores the experience with its embedding
  - Next time agent sees a SIMILAR situation → retrieves relevant memories
  - Works with any embedding model (sentence-transformers, OpenAI, etc.)
"""
from __future__ import annotations
import numpy as np
import logging
from typing import Any, Dict, List, Optional, Tuple
from collections import deque
import threading

logger = logging.getLogger("cognicore.memory.embedding")

try:
    from sentence_transformers import SentenceTransformer
    HAS_SBERT = True
except ImportError:
    HAS_SBERT = False

# Gymnasium is an optional dependency. Selecting the base class once at import
# time keeps the wrapper usable in minimal installs while preserving normal
# gym.Wrapper behavior whenever the RL extra is installed.
try:
    import gymnasium as gym
except ImportError:
    gym = None


class EmbeddingMemory:
    """Semantic memory using vector embeddings for retrieval.

    Stores experiences as (embedding, data) pairs.
    Retrieves by cosine similarity — finds experiences
    that are semantically SIMILAR to the current situation.

    Usage::
        memory = EmbeddingMemory(model_name="all-MiniLM-L6-v2")
        memory.store("Agent hit wall at position (3,4)", {"pos": (3,4), "reward": -1})
        
        # Later, retrieve similar experiences
        results = memory.retrieve("Near wall at position (3,5)", top_k=3)
        # Returns experiences about walls, even though position is different
    """


    def _with_lock(func):
        def wrapper(self, *args, **kwargs):
            with self._lock:
                return func(self, *args, **kwargs)
        return wrapper

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        max_size: int = 10000,
        embedding_dim: int = 384,
    ):
        self.max_size = max_size
        self.embedding_dim = embedding_dim
        self.model_name = model_name

        # Lazy load model
        self._model = None

        # Storage
        self._embeddings: List[np.ndarray] = []
        self._data: List[Dict[str, Any]] = []
        self._texts: List[str] = []

        # Stats
        self._stores = 0
        self._retrievals = 0
        self._hits = 0
        self._lock = threading.RLock()

    @property
    def model(self):
        if self._model is None:
            if HAS_SBERT:
                self._model = SentenceTransformer(self.model_name)
                logger.info(f"Loaded embedding model: {self.model_name}")
            else:
                logger.warning("sentence-transformers not installed. Using random embeddings.")
                self._model = "random"
        return self._model

    def _embed(self, text: str) -> np.ndarray:
        """Get embedding for text."""
        if self.model == "random":
            # Deterministic hash-based embedding as fallback
            np.random.seed(hash(text) % 2**31)
            vec = np.random.randn(self.embedding_dim).astype(np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec /= norm
            return vec
        return self.model.encode(text, normalize_embeddings=True)

    def _embed_observation(self, obs: Any) -> Tuple[str, np.ndarray]:
        """Convert any observation to text + embedding."""
        if isinstance(obs, str):
            text = obs
        elif isinstance(obs, dict):
            text = " ".join(f"{k}={v}" for k, v in obs.items())
        elif isinstance(obs, np.ndarray):
            text = f"state=[{','.join(f'{x:.1f}' for x in obs[:10])}]"
        else:
            text = str(obs)
        return text, self._embed(text)

    @_with_lock
    def store(
        self,
        observation: Any,
        data: Dict[str, Any],
        episode: int = 0,
    ) -> None:
        """Store an experience with its embedding.

        Parameters
        ----------
        observation : any
            The situation (string, dict, or numpy array).
        data : dict
            Associated data (reward, action, outcome, etc.).
        episode : int
            Episode number for tracking.
        """
        text, embedding = self._embed_observation(observation)

        if len(self._embeddings) >= self.max_size:
            self._embeddings.pop(0)
            self._data.pop(0)
            self._texts.pop(0)

        self._embeddings.append(embedding)
        self._data.append({**data, "_episode": episode, "_text": text})
        self._texts.append(text)
        self._stores += 1

    @_with_lock
    def retrieve(
        self,
        query: Any,
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> List[Dict[str, Any]]:
        """Retrieve most similar experiences.

        Parameters
        ----------
        query : any
            Current situation to match against.
        top_k : int
            Max results to return.
        min_similarity : float
            Minimum cosine similarity threshold.

        Returns
        -------
        list of dict
            Similar experiences, sorted by relevance.
        """
        if not self._embeddings:
            return []

        self._retrievals += 1
        _, query_emb = self._embed_observation(query)

        # Cosine similarity against all stored embeddings
        emb_matrix = np.stack(self._embeddings)
        similarities = emb_matrix @ query_emb

        # Filter and sort
        if len(similarities) > top_k:
            indices = np.argpartition(similarities, -top_k)[-top_k:]
            # Sort just the top k
            sorted_k = np.argsort(similarities[indices])[::-1]
            indices = indices[sorted_k]
        else:
            indices = np.argsort(similarities)[::-1]
        results = []
        for idx in indices:
            sim = float(similarities[idx])
            if sim >= min_similarity:
                results.append({
                    **self._data[idx],
                    "_similarity": round(sim, 4),
                    "_matched_text": self._texts[idx],
                })
                self._hits += 1

        return results

    @_with_lock
    def get_advice(self, observation: Any, top_k: int = 3) -> Optional[str]:
        """Get human-readable advice from memory.

        Returns a string summarizing what the agent learned
        from similar past experiences.
        """
        memories = self.retrieve(observation, top_k=top_k)
        if not memories:
            return None

        advice_parts = []
        for mem in memories:
            sim = mem["_similarity"]
            text = mem["_matched_text"]
            reward = mem.get("reward", "?")
            action = mem.get("action", "?")
            correct = mem.get("correct", "?")
            advice_parts.append(
                f"  [{sim:.0%} match] In '{text}': action={action}, reward={reward}, correct={correct}"
            )

        return "Memory says:\n" + "\n".join(advice_parts)

    @property
    def size(self) -> int:
        return len(self._embeddings)

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "stored": self._stores,
            "retrievals": self._retrievals,
            "hits": self._hits,
            "size": self.size,
            "hit_rate": self._hits / max(self._retrievals, 1),
            "model": self.model_name,
        }

    @_with_lock
    def clear(self) -> None:
        self._embeddings.clear()
        self._data.clear()
        self._texts.clear()


class CognitiveGymWrapper(gym.Wrapper if gym is not None else object):
    """Gymnasium wrapper that adds embedding-based memory to ANY env.

    This is the core value proposition:
      env = gym.make("cognicore/MazeRunner-v0")
      env = CognitiveGymWrapper(env)
      # Now the env automatically:
      #   1. Stores experiences after each step
      #   2. Retrieves similar past experiences
      #   3. Adds memory context to info dict
    """
    def __init__(self, env, memory_size: int = 5000, top_k: int = 3):
        # object.__init__ cannot accept the wrapped environment, so minimal
        # installs retain it directly instead of calling super().
        if gym is not None:
            super().__init__(env)
        else:
            self.env = env

        self.memory = EmbeddingMemory(max_size=memory_size)
        self.top_k = top_k
        self._episode = 0
        self._step_count = 0

    def reset(self, **kwargs):
        self._episode += 1
        self._step_count = 0
        obs, info = self.env.reset(**kwargs)
        
        # Add memory context
        memories = self.memory.retrieve(obs, top_k=self.top_k)
        info["cognicore_memory"] = memories
        info["cognicore_advice"] = self.memory.get_advice(obs)

        return obs, info

    def step(self, action):
        obs, reward, terminated, truncated, info = self.env.step(action)
        self._step_count += 1

        # Store experience
        self.memory.store(obs, {
            "action": int(action) if hasattr(action, '__int__') else action,
            "reward": float(reward),
            "correct": info.get("event") == "goal",
            "step": self._step_count,
        }, episode=self._episode)

        # Retrieve similar experiences for next decision
        memories = self.memory.retrieve(obs, top_k=self.top_k)
        info["cognicore_memory"] = memories
        info["cognicore_advice"] = self.memory.get_advice(obs)
        info["cognicore_memory_stats"] = self.memory.stats

        return obs, reward, terminated, truncated, info
