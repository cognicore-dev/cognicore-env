# README Hero Section

> Paste the content below directly into the top of your `README.md`, replacing or preceding the existing hero content.

---

<!-- BEGIN HERO SECTION -->

# 🧠 CogniCore — Cognitive Runtime for AI Agents

**Agents learn from execution history instead of repeating mistakes.**

[![PyPI](https://img.shields.io/pypi/v/cognicore-env?color=orange)](https://pypi.org/project/cognicore-env/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)]()
[![Tests](https://img.shields.io/badge/tests-470%20passing-brightgreen)]()
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Environments](https://img.shields.io/badge/environments-60%2B-purple)]()
[![GitHub stars](https://img.shields.io/github/stars/Kaushalt2004/cognicore-my-openenv?style=social)](https://github.com/Kaushalt2004/cognicore-my-openenv)

> *The model stays the same. The runtime gets smarter.*

---

### Why CogniCore?

| | Feature | What it does |
|---|---|---|
| 🧠 | **Persistent Memory** | Agents remember what worked and what failed — across episodes and sessions. Solve rate goes from 38% to 95% with zero model changes. |
| 🔁 | **Replay & Time Travel** | Every agent decision is an immutable event. Replay past runs, branch from any point, compare outcomes. Git for agent cognition. |
| 🛡️ | **Agent Immune System** | Built-in defense against prompt injection, jailbreaks, and resource attacks. RL-trained defender that learns from every interaction. |

---

### Benchmarks

Tested on 20 SWE-bench-lite tasks across 4 routing policies with 280+ recorded trajectories:

| Policy | Solved | Rate | Tokens | Tokens/Solve |
|---|---|---|---|---|
| `test_first` | **19/20** | **95.0%** | 32,854 | 1,729 |
| `minimal` | 19/20 | 95.0% | **27,476** | **1,446** |
| `standard` (with reviewer) | 18/20 | 90.0% | 37,118 | 2,062 |
| `review_first` | 18/20 | 90.0% | 45,591 | 2,533 |

**Key finding:** Adding a reviewer agent *reduces* solve rate by 5% while increasing token cost by 35%. Runtime cognition matters more than adding more agents. [Read the paper →](docs/paper/nexus_paper.tex)

---

### Quick Start

```bash
pip install cognicore-env
```

```python
import cognicore as cc
from cognicore.agents.smart import AutoLearner

# Enable memory and reflection — that's the only change
config = cc.CogniCoreConfig(enable_memory=True, enable_reflection=True)
env = cc.make("SafetyClassification-v1", config=config)
agent = AutoLearner()

for ep in range(20):
    obs = env.reset()
    while True:
        action = agent.act(obs)
        obs, reward, done, _, info = env.step(action)
        agent.learn(reward, info)
        if done:
            print(f"Episode {ep+1}: {env.episode_stats().accuracy:.0%}")
            break
```

```
Episode  1: 40%
Episode  5: 55%
Episode 10: 75%
Episode 15: 90%
Episode 20: 95%   ← same model, smarter runtime
```

---

**60+ environments** · **Works with any LLM** (OpenAI, Gemini, Claude, Ollama, HuggingFace, OpenRouter) · **470 tests passing** · **Gymnasium-compatible** · **Apache 2.0 License**

<!-- END HERO SECTION -->
