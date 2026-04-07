from __future__ import annotations

import os
import signal

from .errors import ActorError
from .interfaces import ProcessManager


class RealProcessManager(ProcessManager):
    def is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, PermissionError):
            return False

    def kill(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            raise ActorError(f"failed to kill pid {pid}: {e}")
