"""Unit tests for runner/executor.py (ScriptExecutor)."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from claude_feishu_flow.runner.executor import ExecutionResult, ScriptExecutor


def _make_experiment_dir(tmp_path: Path, code: str, name: str = "exp_test") -> Path:
    """Create a minimal experiment directory with setting/main.py."""
    exp_dir = tmp_path / name
    setting_dir = exp_dir / "setting"
    setting_dir.mkdir(parents=True)
    (setting_dir / "main.py").write_text(textwrap.dedent(code), encoding="utf-8")
    return exp_dir


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_captures_stdout(tmp_path: Path):
    exp_dir = _make_experiment_dir(tmp_path, 'print("hello world")\n')
    result = await ScriptExecutor().run(exp_dir)

    assert result.returncode == 0
    assert "hello world" in result.stdout
    assert result.success is True


@pytest.mark.asyncio
async def test_run_captures_stderr(tmp_path: Path):
    exp_dir = _make_experiment_dir(tmp_path, """\
        import sys
        sys.stderr.write("oops\\n")
    """, "exp_stderr")
    result = await ScriptExecutor().run(exp_dir)

    assert result.returncode == 0
    assert "oops" in result.stderr


@pytest.mark.asyncio
async def test_run_nonzero_returncode(tmp_path: Path):
    exp_dir = _make_experiment_dir(tmp_path, """\
        import sys
        sys.exit(42)
    """, "exp_fail")
    result = await ScriptExecutor().run(exp_dir)

    assert result.returncode == 42
    assert result.success is False


@pytest.mark.asyncio
async def test_run_captures_multiline_stdout(tmp_path: Path):
    exp_dir = _make_experiment_dir(tmp_path, """\
        for i in range(5):
            print(f"line {i}")
    """, "exp_multi")
    result = await ScriptExecutor().run(exp_dir)

    assert result.returncode == 0
    for i in range(5):
        assert f"line {i}" in result.stdout


@pytest.mark.asyncio
async def test_run_records_duration(tmp_path: Path):
    exp_dir = _make_experiment_dir(tmp_path, "pass\n", "exp_dur")
    result = await ScriptExecutor().run(exp_dir)

    assert result.duration_seconds >= 0.0


@pytest.mark.asyncio
async def test_run_exception_in_script(tmp_path: Path):
    """A script that raises an exception exits with returncode 1 and stderr."""
    exp_dir = _make_experiment_dir(tmp_path, """\
        raise ValueError("deliberate error")
    """, "exp_exc")
    result = await ScriptExecutor().run(exp_dir)

    assert result.returncode != 0
    assert "ValueError" in result.stderr


@pytest.mark.asyncio
async def test_run_writes_log_files(tmp_path: Path):
    """stdout/stderr are written to output/run.log and output/error.log."""
    exp_dir = _make_experiment_dir(tmp_path, """\
        import sys
        print("stdout line")
        sys.stderr.write("stderr line\\n")
    """, "exp_logs")
    result = await ScriptExecutor().run(exp_dir)

    assert Path(result.run_log_path).read_text() == result.stdout
    assert Path(result.error_log_path).read_text() == result.stderr
    assert "stdout line" in Path(result.run_log_path).read_text()
    assert "stderr line" in Path(result.error_log_path).read_text()


@pytest.mark.asyncio
async def test_run_cwd_is_experiment_dir(tmp_path: Path):
    """The subprocess cwd is the experiment root, so relative paths land there."""
    exp_dir = _make_experiment_dir(tmp_path, """\
        with open("results/output.txt", "w") as f:
            f.write("artifact")
    """, "exp_cwd")
    (exp_dir / "results").mkdir()
    result = await ScriptExecutor().run(exp_dir)

    assert result.returncode == 0
    assert (exp_dir / "results" / "output.txt").read_text() == "artifact"


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_timeout_kills_process(tmp_path: Path):
    """Script that sleeps longer than timeout raises asyncio.TimeoutError."""
    exp_dir = _make_experiment_dir(tmp_path, """\
        import time
        time.sleep(60)
    """, "exp_slow")
    with pytest.raises(asyncio.TimeoutError):
        await ScriptExecutor().run(exp_dir, timeout=0.5)


@pytest.mark.asyncio
async def test_run_completes_within_timeout(tmp_path: Path):
    """Fast script completes successfully even with a short timeout."""
    exp_dir = _make_experiment_dir(tmp_path, 'print("done")\n', "exp_fast")
    result = await ScriptExecutor(default_timeout=5.0).run(exp_dir)

    assert result.returncode == 0
    assert "done" in result.stdout


# ---------------------------------------------------------------------------
# ExecutionResult helpers
# ---------------------------------------------------------------------------

def test_execution_result_success_true():
    r = ExecutionResult(
        returncode=0, stdout="", stderr="", duration_seconds=1.0,
        run_log_path="/tmp/run.log", error_log_path="/tmp/error.log",
    )
    assert r.success is True


def test_execution_result_success_false():
    r = ExecutionResult(
        returncode=1, stdout="", stderr="err", duration_seconds=0.1,
        run_log_path="/tmp/run.log", error_log_path="/tmp/error.log",
    )
    assert r.success is False
