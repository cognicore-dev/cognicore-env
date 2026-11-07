"""
CogniCore — Cognitive Environments for AI.

Every environment gets Memory, Reflection, Structured Rewards,
and PROPOSE→Revise built in. Any AI agent (LLM, RL, classifier,
rule-based) can plug in and learn from experience.

Quick start::

    import cognicore

    env = cognicore.make("SafetyClassification-v1", difficulty="easy")
    obs = env.reset()

    while True:
        action = {"classification": "SAFE"}  # your agent here
        obs, reward, done, truncated, info = env.step(action)
        print(f"Reward: {reward.total:.2f}")
        if done:
            break

    print(env.episode_stats())
"""

__version__ = "0.12.0"

# Old module paths (e.g. cognicore.advanced_memory) keep working after the
# restructure; importing _compat installs them. See cognicore/_compat.py.
from cognicore import _compat

# Core
from cognicore.core.base_env import CogniCoreEnv
from cognicore.core.types import (
    CogniCoreConfig,
    EpisodeStats,
    EvalResult,
    ProposalFeedback,
    StepResult,
    StructuredReward,
)
from cognicore.core.spaces import DiscreteSpace, DictSpace, TextSpace

# Runtime — universal cognition wrapper
from cognicore.runtime import CogniCoreRuntime, RuntimeConfig, ExecutionResult
from cognicore.core.api import train, evaluate
from cognicore.core.errors import (
    CogniCoreError,
    InvalidEnvironmentError,
    InvalidActionError,
    InvalidConfigError,
    EnvironmentNotResetError,
    EpisodeFinishedError,
    AgentInterfaceError,
)

# Registry
from cognicore.envs.registry import make, register, list_envs

# Agents — RL
from cognicore.agents.base_agent import BaseAgent, RandomAgent, AgentProtocol
from cognicore.agents.rl_agents import QLearningAgent, SARSAAgent, GeneticAgent, BanditAgent
# Agents — Real ML (trains locally, no APIs)
from cognicore.agents.ml_agents import DeepQAgent, SklearnAgent, XGBoostAgent, PolicyGradientAgent
# Agents — Company APIs (optional, needs API keys)
from cognicore.agents.company_models import (
    OpenAIAgent, GeminiAgent, ClaudeAgent,
    OllamaAgent, HuggingFaceAgent, OpenAICompatibleAgent,
)

# Core Features
from cognicore.core.cognitive_boost import CognitiveBoost, Arena, AutoCurriculum, TransferAgent

# Memory Architecture
from cognicore.memory.base import (
    MemoryScope,
    MemoryEntry,
    SearchResult,
    MemoryBackend,
    EmbeddingProvider,
)
from cognicore.memory.tfidf_backend import TFIDFMemoryBackend
from cognicore.memory.tfidf_embedder import TFIDFEmbeddingProvider

# Middleware (importable for custom usage)
from cognicore.middleware.reflection import ReflectionEngine
from cognicore.middleware.safety_monitor import SafetyMonitor

# Adapters
from cognicore.adapters.gymnasium import GymnasiumAdapter


# Persistence & Leaderboard
from cognicore.memory.manager import MemoryManager
from cognicore.evaluation.leaderboard import Leaderboard

# Fine-tuning
from cognicore.training.finetuning import EpisodeRecorder

# Multi-agent
from cognicore.envs.multi_agent_base import MultiAgentEnv, DebateEnv

# Advanced features
from cognicore.training.curriculum import CurriculumRunner
from cognicore.evaluation.benchmark import benchmark_agent, BenchmarkResult
from cognicore.envs.compose import Pipeline
from cognicore.evaluation.analytics import PerformanceAnalyzer
from cognicore.evaluation.experiment import Experiment

# Premium features
from cognicore.memory.semantic import SemanticMemory
Memory = SemanticMemory
from cognicore.observability.explainer import Explainer
from cognicore.safety.adversarial import AdversarialTester
from cognicore.agents.smart import AutoLearner, SafeAgent, AdaptiveAgent
from cognicore.training.auto_improve import auto_improve
from cognicore.safety.layer import SafetyLayer, Policy
from cognicore.observability.cost_tracker import CostTracker

# Research-grade features
from cognicore.observability.predictive import FailurePredictor
from cognicore.memory.multi_tier import CognitiveMemory, UnifiedMemory
from cognicore.safety.red_blue import RedVsBlue
from cognicore.observability.debugger import AIDebugger
from cognicore.evaluation.intelligence import IntelligenceScorer
from cognicore.observability.thought_trace import ThoughtTracer
from cognicore.agents.knowledge_transfer import transfer_knowledge, MentorStudent
from cognicore.training.evolution import EvolutionEngine

# Platform features
from cognicore.agents.persistence import save_agent, load_agent
from cognicore.evaluation.report import ReportGenerator
from cognicore.replay import EventRecorder, TaskReplayer, TaskBrancher, RLNavigator
from cognicore.immune import NexusShield, RLDefender
from cognicore.core.profiles import get_profile, list_profiles
from cognicore.training.prompt_optimizer import PromptOptimizer
from cognicore.observability.webhooks import AlertSystem
from cognicore.evaluation.augmentation import DataAugmenter
from cognicore.agents.fingerprint import AgentFingerprint
from cognicore.evaluation.difficulty import DifficultyEstimator
from cognicore.utils.rate_limiter import RateLimiter
from cognicore.utils.cache import ResponseCache

# Phase 8 — Roadmap features
from cognicore.training.meta_rewards import MetaRewardOptimizer
from cognicore.observability.causal import CausalEngine
from cognicore.agents.builder import build_agent, describe_agent
from cognicore.agents.strategy import StrategySwitcher
from cognicore.agents.lifelong import LifelongAgent
from cognicore.agents.swarm import Swarm

__all__ = [
    # Version
    "__version__",
    # Core
    "CogniCoreEnv",
    "train",
    "evaluate",
    "CogniCoreConfig",
    "StructuredReward",
    "EvalResult",
    "StepResult",
    "EpisodeStats",
    "ProposalFeedback",
    # Spaces
    "DiscreteSpace",
    "DictSpace",
    "TextSpace",
    # Registry
    "make",
    "register",
    "list_envs",
    # Agents
    "BaseAgent",
    "RandomAgent",
    "AgentProtocol",
    # Errors
    "CogniCoreError",
    "InvalidEnvironmentError",
    "InvalidActionError",
    "InvalidConfigError",
    "EnvironmentNotResetError",
    "EpisodeFinishedError",
    "AgentInterfaceError",
    # Middleware
    "Memory",
    "ReflectionEngine",
    "SafetyMonitor",
    # Adapters
    "GymnasiumAdapter",
    # Persistence
    "MemoryManager",
    "Leaderboard",
    # Fine-tuning
    "EpisodeRecorder",
    # Multi-agent
    "MultiAgentEnv",
    "DebateEnv",
    # Advanced
    "CurriculumRunner",
    "benchmark_agent",
    "BenchmarkResult",
    "Pipeline",
    "PerformanceAnalyzer",
    "Experiment",
    # Premium
    "SemanticMemory",
    "Explainer",
    "AdversarialTester",
    "AutoLearner",
    "SafeAgent",
    "AdaptiveAgent",
    "auto_improve",
    "SafetyLayer",
    "Policy",
    "CostTracker",
    # Research-grade
    "FailurePredictor",
    "CognitiveMemory",
    "UnifiedMemory",
    "RedVsBlue",
    "AIDebugger",
    "IntelligenceScorer",
    "ThoughtTracer",
    "transfer_knowledge",
    "MentorStudent",
    "EvolutionEngine",
    # Platform
    "save_agent",
    "load_agent",
    "ReportGenerator",
    "EventRecorder",
    "TaskReplayer",
    "TaskBrancher",
    "RLNavigator",
    "NexusShield",
    "RLDefender",
    "get_profile",
    "list_profiles",
    "PromptOptimizer",
    "AlertSystem",
    "DataAugmenter",
    "AgentFingerprint",
    "DifficultyEstimator",
    "RateLimiter",
    "ResponseCache",
    # Phase 8 — Roadmap
    "MetaRewardOptimizer",
    "CausalEngine",
    "build_agent",
    "describe_agent",
    "StrategySwitcher",
    "LifelongAgent",
    "Swarm",
]

_compat.alias_loaded()
