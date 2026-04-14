"""Async subprocess executor for generated experiment scripts."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of running a generated experiment script."""

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

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
        script_path: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        """Execute a Python script and capture its output.

        Args:
            script_path: Absolute or relative path to the .py file to run.
            timeout:     Max wall-clock seconds to wait.
                         Defaults to self.default_timeout (constructor arg).
                         Pass 0 or None to use the default.

        Returns:
            ExecutionResult with returncode, stdout, stderr, and duration_seconds.

        Raises:
            asyncio.TimeoutError: Re-raised after the subprocess is killed,
                                  so callers can differentiate timeout from failure.
        """
        effective_timeout = timeout if timeout else self._default_timeout
        import os
        work_dir = os.path.dirname(os.path.abspath(script_path))

        logger.info(
            "Executing script: %s  (timeout=%.0fs, cwd=%s)",
            script_path,
            effective_timeout,
            work_dir,
        )

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "python3",
            script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            duration = time.monotonic() - start
            logger.error(
                "Script timed out after %.1fs: %s", duration, script_path
            )
            raise

        duration = time.monotonic() - start
        result = ExecutionResult(
            returncode=proc.returncode,  # type: ignore[arg-type]
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_seconds=duration,
        )

        logger.info(
            "Script finished: returncode=%d  duration=%.2fs  stdout=%d bytes  stderr=%d bytes",
            result.returncode,
            result.duration_seconds,
            len(result.stdout),
            len(result.stderr),
        )
        return result
