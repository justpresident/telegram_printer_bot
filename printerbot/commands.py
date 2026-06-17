"""CommandRunner: the single seam for running external processes."""

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(ABC):
    """Abstraction over running external commands. A single seam keeps the
    adapters free of shell quoting and makes them trivially testable."""

    @abstractmethod
    def run(self, args: List[str], timeout: Optional[int] = None) -> CommandResult:
        pass


class SubprocessCommandRunner(CommandRunner):
    """Runs commands via subprocess with an argument list (no shell), avoiding
    shell-injection and capturing stdout/stderr."""

    def run(self, args: List[str], timeout: Optional[int] = None) -> CommandResult:
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return CommandResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as e:
            return CommandResult(124, e.stdout or "", f"timed out after {timeout}s")
        except FileNotFoundError as e:
            return CommandResult(127, "", str(e))
        except Exception as e:
            return CommandResult(1, "", str(e))


