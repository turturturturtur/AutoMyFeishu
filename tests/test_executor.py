"""Unit tests for runner/executor.py (ScriptExecutor)."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from claude_feishu_flow.runner.executor import ExecutionResult, ScriptExecutor


def _write_script(tmp_path: Path, name: str, code: str) -> str:
    p = tmp_path / name
    p.write_text(textwrap.dedent(code), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_captures_stdout(tmp_path: Path):
    script = _write_script(tmp_path, "hello.py", """\
        print("hello world")
    """)
    result = await ScriptExecutor().run(script)

    assert result.returncode == 0
    assert "hello world" in result.stdout
    assert result.success is True


@pytest.mark.asyncio
async def test_run_captures_stderr(tmp_path: Path):
    script = _write_script(tmp_path, "err.py", """\
        import sys
        sys.stderr.write("oops\\n")
    """)
    result = await ScriptExecutor().run(script)

    assert result.returncode == 0
    assert "oops" in result.stderr


@pytest.mark.asyncio
async def test_run_nonzero_returncode(tmp_path: Path):
    script = _write_script(tmp_path, "fail.py", """\
        import sys
        sys.exit(42)
    """)
    result = await ScriptExecutor().run(script)

    assert result.returncode == 42
    assert result.success is False


@pytest.mark.asyncio
async def test_run_captures_multiline_stdout(tmp_path: Path):
    script = _write_script(tmp_path, "multi.py", """\
        for i in range(5):
            print(f"line {i}")
    """)
    result = await ScriptExecutor().run(script)

    assert result.returncode == 0
    for i in range(5):
        assert f"line {i}" in result.stdout


@pytest.mark.asyncio
async def test_run_records_duration(tmp_path: Path):
    script = _write_script(tmp_path, "dur.py", "pass\n")
    result = await ScriptExecutor().run(script)

    assert result.duration_seconds >= 0.0


@pytest.mark.asyncio
async def test_run_exception_in_script(tmp_path: Path):
    """A script that raises an exception exits with returncode 1 and stderr."""
    script = _write_script(tmp_path, "exc.py", """\
        raise ValueError("deliberate error")
    """)
    result = await ScriptExecutor().run(script)

    assert result.returncode != 0
    assert "ValueError" in result.stderr


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_timeout_kills_process(tmp_path: Path):
    """Script that sleeps longer than timeout raises asyncio.TimeoutError."""
    script = _write_script(tmp_path, "slow.py", """\
        import time
        time.sleep(60)
    """)
    with pytest.raises(asyncio.TimeoutError):
        await ScriptExecutor().run(script, timeout=0.5)


@pytest.mark.asyncio
async def test_run_completes_within_timeout(tmp_path: Path):
    """Fast script completes successfully even with a short timeout."""
    script = _write_script(tmp_path, "fast.py", 'print("done")\n')
    result = await ScriptExecutor(default_timeout=5.0).run(script)

    assert result.returncode == 0
    assert "done" in result.stdout


# ---------------------------------------------------------------------------
# ExecutionResult helpers
# ---------------------------------------------------------------------------

def test_execution_result_success_true():
    r = ExecutionResult(returncode=0, stdout="", stderr="", duration_seconds=1.0)
    assert r.success is True


def test_execution_result_success_false():
    r = ExecutionResult(returncode=1, stdout="", stderr="err", duration_seconds=0.1)
    assert r.success is False
