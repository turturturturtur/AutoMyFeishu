"""Async subprocess executor for generated experiment scripts."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
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

    @property
    def success(self) -> bool:
        return self.returncode == 0


class ScriptExecutor:
    """Runs Python scripts in isolated subprocesses without blocking the event loop.

    Uses asyncio.create_subprocess_exec (non-blocking) rather than
    subprocess.run (which would block the entire event loop thread).
    """

    def __init__(self, default_timeout: float = 3600.0) -> None:
        self._default_timeout = default_timeout

    async def run(
        self,
        experiment_dir: Path,
        timeout: float | None = None,
    ) -> ExecutionResult:
        """Execute setting/main.py inside experiment_dir, streaming output to log files.

        The subprocess's cwd is set to experiment_dir so any relative-path
        artefacts produced by the script land inside the experiment directory.

        stdout is written in real-time to output/run.log.
        stderr is written in real-time to output/error.log.
        Both streams are also accumulated in memory and returned.

        Args:
            experiment_dir: Path to the experiment root (Experiments/exp_<uuid>/).
            timeout:        Max wall-clock seconds to wait.
                            Defaults to self._default_timeout.
                            Pass 0 or None to use the default.

        Returns:
            ExecutionResult with returncode, stdout, stderr, duration_seconds,
            run_log_path, and error_log_path.

        Raises:
            asyncio.TimeoutError: Re-raised after the subprocess is killed.
        """
        effective_timeout = timeout if timeout else self._default_timeout
        script_path = experiment_dir / "setting" / "main.py"
        output_dir = experiment_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        run_log_path = output_dir / "run.log"
        error_log_path = output_dir / "error.log"

        logger.info(
            "Executing script: %s  (timeout=%.0fs, cwd=%s)",
            script_path,
            effective_timeout,
            experiment_dir,
        )

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "python3",
            str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(experiment_dir),
        )

        async def _drain_stream(
            stream: asyncio.StreamReader,
            log_path: Path,
        ) -> str:
            """Read a stream line-by-line, writing each line to a log file.

            Returns all captured output as a single string.
            """
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
            duration = time.monotonic() - start
            logger.error("Script timed out after %.1fs: %s", duration, script_path)
            raise

        duration = time.monotonic() - start
        result = ExecutionResult(
            returncode=proc.returncode,  # type: ignore[arg-type]
            stdout=stdout_str,
            stderr=stderr_str,
            duration_seconds=duration,
            run_log_path=str(run_log_path.resolve()),
            error_log_path=str(error_log_path.resolve()),
        )

        logger.info(
            "Script finished: returncode=%d  duration=%.2fs  stdout=%d bytes  stderr=%d bytes",
            result.returncode,
            result.duration_seconds,
            len(result.stdout),
            len(result.stderr),
        )
        return result
