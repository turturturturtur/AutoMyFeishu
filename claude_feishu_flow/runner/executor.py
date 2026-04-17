"""Async subprocess executor for generated experiment scripts."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of running a generated experiment script."""

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    run_log_path: str
    error_log_path: str
    was_killed: bool = False  # True when this run was terminated by a newer restart

    @property
    def success(self) -> bool:
        return self.returncode == 0


class ScriptExecutor:
    """Runs Python scripts in isolated subprocesses without blocking the event loop.

    Uses asyncio.create_subprocess_exec (non-blocking) rather than
    subprocess.run (which would block the entire event loop thread).

    Supports process lifecycle management: calling run() with the same task_id
    will kill any previously-running process for that task before starting a new one.

    Script discovery order (checked at experiment root first, then setting/ for
    backward compatibility with older experiments):
      1. run.sh       → bash run.sh
      2. train.py     → python3 train.py
      3. main.py      → python3 main.py
      4. setting/run.sh   → bash setting/run.sh   (legacy)
      5. setting/main.py  → python3 setting/main.py (legacy)

    Logs are written to run.log / error.log at the experiment root (new) or
    output/run.log / output/error.log (legacy, if output/ dir already exists).
    """

    def __init__(self, default_timeout: float = 3600.0) -> None:
        self._default_timeout = default_timeout
        # Maps task_id -> running subprocess (for lifecycle management)
        self.active_processes: dict[str, asyncio.subprocess.Process] = {}
        # task_ids that were intentionally killed by a newer restart request
        self._killed_tasks: set[str] = set()

    async def run(
        self,
        experiment_dir: Path,
        task_id: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        """Execute the experiment inside experiment_dir, streaming output to log files.

        Script discovery order (root-first, then setting/ for legacy compat):
          1. run.sh            → bash run.sh
          2. train.py          → python3 train.py
          3. main.py           → python3 main.py
          4. setting/run.sh    → bash setting/run.sh   (legacy)
          5. setting/main.py   → python3 setting/main.py (legacy)

        Log files:
          - New layout:    <exp_dir>/run.log, <exp_dir>/error.log
          - Legacy layout: <exp_dir>/output/run.log, <exp_dir>/output/error.log
            (used when <exp_dir>/output/ already exists)

        Process lifecycle:
          If task_id already has an active process (from a previous run), it is
          killed (SIGKILL) before the new process is started.  The *old* run's
          ExecutionResult will have ``was_killed=True`` so callers can silently
          discard it instead of emitting error notifications.

        Args:
            experiment_dir: Path to the experiment root (Experiments/exp_<uuid>/).
            task_id:        Unique identifier for this experiment (used for lifecycle).
            timeout:        Max wall-clock seconds to wait.
                            Defaults to self._default_timeout.
                            Pass 0 or None to use the default.

        Returns:
            ExecutionResult with returncode, stdout, stderr, duration_seconds,
            run_log_path, error_log_path, and was_killed flag.

        Raises:
            asyncio.TimeoutError: Re-raised after the subprocess is killed.
        """
        effective_timeout = timeout if timeout else self._default_timeout

        # --- Kill any existing process for this task_id ---
        if task_id in self.active_processes:
            old_proc = self.active_processes.pop(task_id)
            logger.info("Killing existing process for task_id=%s (pid=%s)", task_id, old_proc.pid)
            self._killed_tasks.add(task_id)
            try:
                old_proc.kill()
                await old_proc.wait()
            except ProcessLookupError:
                pass  # Already dead

        # --- Determine log paths (new root layout vs legacy output/ subdir) ---
        legacy_output_dir = experiment_dir / "output"
        if legacy_output_dir.exists():
            # Preserve existing layout for old experiments
            log_dir = legacy_output_dir
        else:
            log_dir = experiment_dir
        run_log_path = log_dir / "run.log"
        error_log_path = log_dir / "error.log"

        # --- Determine launch command (root-first, then legacy setting/) ---
        if (experiment_dir / "run.sh").exists():
            cmd = ["bash", "run.sh"]
            logger.info("Found run.sh at root, using bash run.sh (cwd=%s)", experiment_dir)
        elif (experiment_dir / "train.py").exists():
            cmd = ["python3", "train.py"]
            logger.info("Found train.py at root (cwd=%s)", experiment_dir)
        elif (experiment_dir / "main.py").exists():
            cmd = ["python3", "main.py"]
            logger.info("Found main.py at root (cwd=%s)", experiment_dir)
        elif (experiment_dir / "setting" / "run.sh").exists():
            cmd = ["bash", "setting/run.sh"]
            logger.info("Found legacy setting/run.sh (cwd=%s)", experiment_dir)
        elif (experiment_dir / "setting" / "main.py").exists():
            script_path = experiment_dir / "setting" / "main.py"
            cmd = ["python3", str(script_path)]
            logger.info("Found legacy setting/main.py: %s (timeout=%.0fs)", script_path, effective_timeout)
        else:
            # --- No entry point found: write diagnostic to error.log and return
            # a non-zero result so the self-healing retry loop can ask Sub Agent
            # to generate the missing run.sh instead of crashing the pipeline. ---
            error_msg = (
                "【执行器错误】未找到可执行入口文件。\n"
                f"已检查路径（基于 {experiment_dir}）：\n"
                "  - run.sh（根目录）\n"
                "  - train.py（根目录）\n"
                "  - main.py（根目录）\n"
                "  - setting/run.sh\n"
                "  - setting/main.py\n\n"
                "请 Sub Agent 检查实验目录结构（如 find . -name '*.py' -maxdepth 3），\n"
                "并在实验根目录生成正确的 run.sh 以启动训练。"
            )
            logger.error("No entry point in %s — writing diagnostic to error.log", experiment_dir)
            error_log_path.parent.mkdir(parents=True, exist_ok=True)
            error_log_path.write_text(error_msg, encoding="utf-8")
            run_log_path.write_text("", encoding="utf-8")
            return ExecutionResult(
                returncode=1,
                stdout="",
                stderr=error_msg,
                duration_seconds=0.0,
                run_log_path=str(run_log_path.resolve()),
                error_log_path=str(error_log_path.resolve()),
                was_killed=False,
            )

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(experiment_dir),
        )
        self.active_processes[task_id] = proc

        async def _drain_stream(
            stream: asyncio.StreamReader,
            log_path: Path,
        ) -> str:
            """Read a stream line-by-line, writing each line to a log file."""
            lines: list[str] = []
            with log_path.open("w", encoding="utf-8") as fh:
                async for raw_line in stream:
                    line = raw_line.decode("utf-8", errors="replace")
                    fh.write(line)
                    fh.flush()
                    lines.append(line)
            return "".join(lines)

        try:
            stdout_task = asyncio.create_task(_drain_stream(proc.stdout, run_log_path))   # type: ignore[arg-type]
            stderr_task = asyncio.create_task(_drain_stream(proc.stderr, error_log_path))  # type: ignore[arg-type]

            await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
            stdout_str = await stdout_task
            stderr_str = await stderr_task

        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self.active_processes.pop(task_id, None)
            duration = time.monotonic() - start
            logger.error("Script timed out after %.1fs", duration)
            raise

        finally:
            self.active_processes.pop(task_id, None)

        duration = time.monotonic() - start

        # Determine if this run was killed by a newer restart
        was_killed = task_id in self._killed_tasks
        if was_killed:
            self._killed_tasks.discard(task_id)
            logger.info("Run for task_id=%s was killed by a restart request, marking was_killed=True", task_id)

        result = ExecutionResult(
            returncode=proc.returncode,  # type: ignore[arg-type]
            stdout=stdout_str,
            stderr=stderr_str,
            duration_seconds=duration,
            run_log_path=str(run_log_path.resolve()),
            error_log_path=str(error_log_path.resolve()),
            was_killed=was_killed,
        )

        logger.info(
            "Script finished: returncode=%d  duration=%.2fs  stdout=%d bytes  stderr=%d bytes  was_killed=%s",
            result.returncode,
            result.duration_seconds,
            len(result.stdout),
            len(result.stderr),
            result.was_killed,
        )
        return result
