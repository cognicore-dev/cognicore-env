"""
CogniCore CLI — Train, evaluate, and benchmark from the command line.

Usage:
    python -m cognicore.cli train --env MazeRunner-v0 --algo PPO --steps 100000
    python -m cognicore.cli benchmark --env GridWorld-v0
    python -m cognicore.cli list
    python -m cognicore.cli arena --envs MazeRunner-v0,GridWorld-v0
    python -m cognicore.cli ui   # Start NEXUS dashboard
"""
import argparse
import sys
import time


def cmd_list(args):
    import cognicore

    print("\n  CogniCore Environments:")
    print("  " + "-" * 45)
    native = cognicore.list_envs()
    for spec in native:
        print(f"    {spec['id']}")
    print(f"\n  Total: {len(native)} environments")

    try:
        import cognicore.gym  # registers gymnasium-native envs
        import gymnasium as gym
    except ImportError:
        print("\n  Gymnasium-native envs require the RL extra:")
        print("    pip install 'cognicore-env[rl]'")
        return

    gym_envs = sorted(e for e in gym.envs.registry.keys() if e.startswith("cognicore/"))
    print("\n  Gymnasium Environments:")
    print("  " + "-" * 45)
    for eid in gym_envs:
        print(f"    {eid}")
    print(f"\n  Total: {len(gym_envs)} gymnasium-native envs")


def cmd_train(args):
    import cognicore.gym
    import gymnasium as gym
    from stable_baselines3 import PPO, DQN, A2C
    from stable_baselines3.common.monitor import Monitor
    from stable_baselines3.common.evaluation import evaluate_policy

    env_id = args.env if args.env.startswith("cognicore/") else f"cognicore/{args.env}"
    algos = {"ppo": PPO, "dqn": DQN, "a2c": A2C}
    algo_cls = algos.get(args.algo.lower())
    if not algo_cls:
        print(f"  Unknown algo: {args.algo}. Choose: {list(algos.keys())}")
        return

    print(f"\n  Training {args.algo.upper()} on {env_id} for {args.steps:,} steps...")
    env = Monitor(gym.make(env_id))
    t0 = time.time()
    model = algo_cls("MlpPolicy", env, verbose=0)
    model.learn(total_timesteps=args.steps)
    dt = time.time() - t0
    mean_r, std_r = evaluate_policy(model, env, n_eval_episodes=args.eval_episodes)
    print(f"  Done in {dt:.1f}s | Score: {mean_r:+.1f} +/- {std_r:.1f}")
    if args.save:
        model.save(args.save)
        print(f"  Saved to {args.save}")
    env.close()


def cmd_benchmark(args):
    import cognicore.gym
    import gymnasium as gym
    from stable_baselines3 import PPO, DQN, A2C
    from stable_baselines3.common.monitor import Monitor
    from stable_baselines3.common.evaluation import evaluate_policy
    import numpy as np

    env_id = args.env if args.env.startswith("cognicore/") else f"cognicore/{args.env}"
    print(f"\n  Benchmarking {env_id} ({args.steps:,} steps each)...")
    print(f"  {'Algo':<10} {'Score':>10} {'Std':>8} {'Time':>8}")
    print(f"  {'-'*10} {'-'*10} {'-'*8} {'-'*8}")

    env = Monitor(gym.make(env_id))
    rr = []
    for _ in range(20):
        o, _ = env.reset(); t, d = 0, False
        while not d:
            o, r, te, tr, _ = env.step(env.action_space.sample())
            t += r; d = te or tr
        rr.append(t)
    print(f"  {'Random':<10} {np.mean(rr):>+9.1f} {np.std(rr):>7.1f}        -")
    env.close()

    for name, cls in [("PPO", PPO), ("DQN", DQN), ("A2C", A2C)]:
        env = Monitor(gym.make(env_id))
        t0 = time.time()
        model = cls("MlpPolicy", env, verbose=0)
        model.learn(total_timesteps=args.steps)
        dt = time.time() - t0
        mr, sr = evaluate_policy(model, env, n_eval_episodes=20)
        print(f"  {name:<10} {mr:>+9.1f} {sr:>7.1f} {dt:>7.1f}s")
        env.close()


def cmd_arena(args):
    import cognicore as cc
    env_ids = [e.strip() for e in args.envs.split(",")]
    arena = cc.Arena()
    acts = ["UP","DOWN","LEFT","RIGHT","HOLD","BUY","SELL",
            "FORAGE","HUNT","BUILD_SHELTER","CRAFT_TOOL","REST","EXPLORE","DEFEND"]
    arena.add_agent("Random", cc.RandomAgent())
    arena.add_agent("Q-Learning", cc.QLearningAgent(acts, epsilon_decay=0.97))
    arena.add_agent("SARSA", cc.SARSAAgent(acts, epsilon_decay=0.97))
    arena.add_agent("Bandit", cc.BanditAgent(acts))
    arena.add_agent("Genetic", cc.GeneticAgent(acts, population_size=8))
    print(f"\n  Running arena on: {env_ids}")
    arena.run_tournament(env_ids, episodes_per_match=args.episodes)
    arena.print_leaderboard()


def cmd_ui(args):
    from cognicore.ui.server import start_server
    port = getattr(args, 'port', 7842)
    start_server(port=port, open_browser=True)


def cmd_integrations(args):
    action = getattr(args, 'action', 'status')
    if action == 'setup':
        from cognicore.integrations.setup_wizard import setup_wizard
        setup_wizard()
    elif action == 'test':
        from cognicore.integrations.setup_wizard import test_connections
        test_connections()
    elif action == 'status':
        from cognicore.integrations.task_queue import NexusTaskQueue
        q = NexusTaskQueue()
        s = q.stats()
        print("\n  NEXUS Integration Status")
        print(f"  Tasks: {s['total']} | Dead letter: {s['dead_letter']}")
        print(f"  By status: {s['by_status']}")
        print(f"  By source: {s['by_source']}\n")


def cmd_webhooks(args):
    from cognicore.ui.server import start_server
    port = getattr(args, 'port', 7842)
    print("  Starting webhook server (includes dashboard)...")
    start_server(port=port, open_browser=False)


def cmd_mcp(args):
    """Start the CogniCore MCP server."""
    action = getattr(args, 'action', 'serve')
    if action == 'serve':
        try:
            from cognicore.mcp.server import create_mcp_server
        except ImportError:
            print("  Error: MCP server requires the 'mcp' package.")
            print("  Install with: pip install cognicore-env[mcp]")
            sys.exit(1)

        transport = getattr(args, 'transport', 'stdio')
        print(f"  Starting CogniCore MCP server (transport={transport})...", file=sys.stderr)
        server = create_mcp_server()
        server.run(transport=transport)
    else:
        print(f"  Unknown MCP action: {action}")


def cmd_bench(args):
    """Lightweight benchmark suite (no heavy deps)."""
    from cognicore.benchmarks.suite import BenchmarkSuite

    action = getattr(args, 'action', 'run')
    if action == 'run':
        quick = getattr(args, 'quick', False)
        episodes = getattr(args, 'episodes', 5)
        seed = getattr(args, 'seed', 42)
        output = getattr(args, 'output', None)

        suite = BenchmarkSuite(episodes=episodes, seed=seed)
        print("\n  Running CogniCore benchmarks...")
        result = suite.run(quick=quick)
        print(result.summary())

        if output:
            result.to_json(output)
            print(f"  Results saved to {output}")
        else:
            result.to_json("benchmark_output/latest.json")
            result.to_csv("benchmark_output/latest.csv")
            print("  Results saved to benchmark_output/latest.json")

    elif action == 'compare':
        from cognicore.benchmarks.regression import _cli_main as reg_main
        reg_main()

    elif action == 'report':
        import json
        from pathlib import Path
        p = Path("benchmark_output/latest.json")
        if p.exists():
            data = json.loads(p.read_text())
            from cognicore.benchmarks.suite import BenchmarkResult
            # Quick summary from saved data
            print(f"\n  Last benchmark: {data.get('timestamp', '?')}")
            print(f"  Version: {data.get('version', '?')}")
            print(f"  Duration: {data.get('total_duration_s', 0):.1f}s")
            print(f"  Configs tested: {len(data.get('env_results', []))}")
        else:
            print("  No benchmark results found. Run: cognicore bench run")


def cmd_doctor(args):
    """Diagnostic check — verify installation health."""
    import sys
    print(f"\n  CogniCore Doctor")
    print(f"  {'=' * 45}")

    # Python version
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 10)
    print(f"  Python {py:20s} {'✓' if ok else '✗ (need 3.10+)'}")

    # CogniCore version
    try:
        import cognicore
        ver = getattr(cognicore, "__version__", "?")
        print(f"  CogniCore {ver:16s} ✓")
    except ImportError:
        print(f"  CogniCore {'not found':16s} ✗")
        return

    # Optional deps
    deps = [
        ("fastapi", "server"),
        ("uvicorn", "server"),
        ("mcp", "mcp"),
        ("langchain", "langchain"),
        ("crewai", "crewai"),
        ("gymnasium", "rl"),
        ("stable_baselines3", "rl"),
        ("torch", "rl"),
        ("sentence_transformers", "embeddings"),
        ("pytest", "dev"),
    ]
    print(f"\n  Optional Dependencies:")
    for mod, group in deps:
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "ok")
            print(f"    {mod:25s} {ver:10s} ✓  [{group}]")
        except ImportError:
            print(f"    {mod:25s} {'missing':10s} -  [{group}]")

    # Memory backends
    print(f"\n  Memory Backends:")
    backends = [
        ("TFIDFMemoryBackend", "cognicore.memory.tfidf_backend", "TFIDFMemoryBackend"),
        ("SQLiteMemoryBackend", "cognicore.memory.sqlite_backend", "SQLiteMemoryBackend"),
        ("BasicEmbeddingBackend", "cognicore.memory.embedding_backend", "BasicEmbeddingBackend"),
    ]
    for name, mod, cls_name in backends:
        try:
            m = __import__(mod, fromlist=[cls_name])
            cls = getattr(m, cls_name)
            if name == "SQLiteMemoryBackend":
                cls(db_path=":memory:")
            elif name == "TFIDFMemoryBackend":
                cls()
            print(f"    {name:30s} ✓")
        except Exception as e:
            print(f"    {name:30s} ✗ ({e})")

    # Environments
    try:
        env_count = len(cognicore.list_envs())
        print(f"\n  Environments: {env_count} registered ✓")
    except Exception:
        print(f"\n  Environments: could not list")

    # Tests
    import glob
    test_files = glob.glob("tests/test_*.py")
    print(f"  Test files: {len(test_files)}")

    print(f"\n  {'=' * 45}")
    print(f"  Run tests:  pytest tests/ -q")
    print(f"  Run bench:  cognicore bench run --quick\n")


def cmd_sleep(args):
    """Run memory sleep/consolidation."""
    from cognicore.memory.tfidf_backend import TFIDFMemoryBackend
    from cognicore.memory.sqlite_backend import SQLiteMemoryBackend
    from cognicore.memory.sleep import SleepProcessor
    import os
    
    print("\n  CogniCore Sleep Consolidation")
    print(f"  {'=' * 45}")
    
    if args.backend == "sqlite":
        db_path = args.db_path or "cognicore_memory.db"
        print(f"  Using SQLite memory backend at: {db_path}")
        backend = SQLiteMemoryBackend(db_path=db_path)
    else:
        path = args.path or os.path.expanduser("~/.cognicore/memory.json")
        print(f"  Using TF-IDF memory backend at: {path}")
        backend = TFIDFMemoryBackend(persistence_path=path)
        if os.path.exists(path):
            backend.load(path)
            
    processor = SleepProcessor(backend=backend)
    stats = processor.sleep()
    
    print("\n  Consolidation complete:")
    print(f"    Merged duplicate entries:       {stats.get('merged', 0)}")
    print(f"    Archived contradictions:        {stats.get('archived_contradictions', 0)}")
    print(f"    Compressed episodic memories:   {stats.get('compressed_episodes', 0)}")
    
    if args.backend == "sqlite":
        pass
    else:
        path = args.path or os.path.expanduser("~/.cognicore/memory.json")
        backend.save(path)
        print(f"\n  Saved updated memory to: {path}")
    print(f"  {'=' * 45}\n")


def cmd_passport(args):
    """Inspect or manage Agent Passports."""
    from cognicore.passport import AgentPassport
    import json
    
    if args.action == "inspect":
        print(f"\n  Passport Inspection: {args.file}")
        print(f"  {'=' * 45}")
        try:
            state = AgentPassport.restore(args.file)
            print(f"  Version: {state.get('manifest', {}).get('version', '?')}")
            print(f"  Agent:   {state.get('manifest', {}).get('agent_class', '?')}")
            print(f"  Env:     {state.get('manifest', {}).get('env_class', '?')}")
            print(f"  Config:  {json.dumps(state.get('manifest', {}).get('config', {}))}")
            print(f"  Memory Entries: {len(state.get('memory', []))}")
            print(f"  Replay Events:  {len(state.get('replay', []))}")
            print(f"  Immune State:   {len(state.get('immune', {}).get('antibodies', []))} antibodies")
            print(f"  Total Reward:   {sum(state.get('reward', [])):.2f}")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        print(f"  Unknown passport action: {args.action}")


def cmd_civilization(args):
    """Run a Civilization Server."""
    from cognicore.civilization import CivilizationServer
    import time
    
    if args.action == "serve":
        port = args.port
        print(f"\n  Starting Civilization Server on port {port}...")
        server = CivilizationServer(port=port)
        server.start()
        print(f"  Server running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n  Stopping server...")
            server.stop()
    else:
        print(f"  Unknown civilization action: {args.action}")


def cmd_studio(args):
    """Start the CogniCore Memory Observability Studio."""
    from cognicore.studio import run_studio
    port = getattr(args, 'port', 8060)
    run_studio(port=port)


def main():
    p = argparse.ArgumentParser(prog="cognicore")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("list", help="List environments")
    t = sub.add_parser("train", help="Train agent")
    t.add_argument("--env", required=True); t.add_argument("--algo", default="PPO")
    t.add_argument("--steps", type=int, default=50000)
    t.add_argument("--eval-episodes", type=int, default=20)
    t.add_argument("--save", default=None)
    b = sub.add_parser("benchmark", help="Benchmark algos (requires SB3)")
    b.add_argument("--env", required=True); b.add_argument("--steps", type=int, default=50000)
    a = sub.add_parser("arena", help="ELO tournament")
    a.add_argument("--envs", required=True); a.add_argument("--episodes", type=int, default=20)
    u = sub.add_parser("ui", help="Start NEXUS dashboard")
    u.add_argument("--port", type=int, default=7842)
    i = sub.add_parser("integrations", help="Manage integrations")
    i.add_argument("action", nargs="?", default="status", choices=["setup", "test", "status"])
    w = sub.add_parser("webhooks", help="Start webhook server")
    w.add_argument("--port", type=int, default=7842)
    m = sub.add_parser("mcp", help="MCP server")
    m.add_argument("action", nargs="?", default="serve", choices=["serve"])
    m.add_argument("--transport", default="stdio", choices=["stdio", "sse"])
    # New commands
    bn = sub.add_parser("bench", help="Lightweight benchmark suite (no heavy deps)")
    bn.add_argument("action", nargs="?", default="run", choices=["run", "compare", "report"])
    bn.add_argument("--quick", action="store_true", help="Quick run (easy, 2 episodes)")
    bn.add_argument("--episodes", type=int, default=5)
    bn.add_argument("--seed", type=int, default=42)
    bn.add_argument("--output", type=str, default=None)
    sub.add_parser("doctor", help="Check installation health")
    s = sub.add_parser("sleep", help="Run offline memory consolidation (sleep)")
    s.add_argument("--backend", default="tfidf", choices=["tfidf", "sqlite"])
    s.add_argument("--path", type=str, default=None, help="Path to TF-IDF json memory file")
    s.add_argument("--db-path", type=str, default=None, help="Path to SQLite database")
    
    pass_cmd = sub.add_parser("passport", help="Inspect Agent Passports")
    pass_cmd.add_argument("action", choices=["inspect"])
    pass_cmd.add_argument("file", help="Path to .passport file")
    
    civ_cmd = sub.add_parser("civilization", help="Manage Civilization Server")
    civ_cmd.add_argument("action", choices=["serve"])
    civ_cmd.add_argument("--port", type=int, default=9876, help="Port to serve on")
    
    std_cmd = sub.add_parser("studio", help="Start the Memory Observability Studio")
    std_cmd.add_argument("--port", type=int, default=8060, help="Port to serve on")
    
    args = p.parse_args()
    cmds = {"list": cmd_list, "train": cmd_train, "benchmark": cmd_benchmark,
            "arena": cmd_arena, "ui": cmd_ui, "integrations": cmd_integrations,
            "webhooks": cmd_webhooks, "mcp": cmd_mcp,
            "bench": cmd_bench, "doctor": cmd_doctor, "sleep": cmd_sleep,
            "passport": cmd_passport, "civilization": cmd_civilization,
            "studio": cmd_studio}
    if args.command in cmds:
        cmds[args.command](args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()

