"""CLI smoke tests for base-install commands."""
import builtins

from cognicore.cli import cmd_doctor, cmd_list


def test_cmd_list_shows_native_envs(capsys):
    cmd_list(None)
    out = capsys.readouterr().out
    assert "SafetyClassification-v1" in out
    assert "Total:" in out


def test_cmd_list_without_gymnasium(capsys, monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "gymnasium" or name.startswith("gymnasium.") or name == "cognicore.gym":
            raise ImportError("No module named gymnasium")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    cmd_list(None)
    out = capsys.readouterr().out
    assert "SafetyClassification-v1" in out
    assert "cognicore-env[rl]" in out


def test_cmd_doctor_reports_real_backends(capsys):
    cmd_doctor(None)
    out = capsys.readouterr().out
    assert "BasicEmbeddingBackend" in out
    assert "EmbeddingMemoryBackend" not in out
    assert "TFIDFMemoryBackend" in out
    assert "SQLiteMemoryBackend" in out
    for line in out.splitlines():
        if "BasicEmbeddingBackend" in line:
            assert "✓" in line
            break
    else:
        raise AssertionError("BasicEmbeddingBackend line missing from doctor output")
