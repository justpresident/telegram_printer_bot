"""StateStore: a tiny persistent key/value dict with atomic updates."""

import os
import json
import threading
import fcntl
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any


class StateStore(ABC):
    """A tiny persistent dict. Implementations decide where bytes live.

    `update()` performs an atomic read-modify-write so that independent users
    of the same store (e.g. auth + settings) can't clobber each other's keys.
    """

    @abstractmethod
    def load(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    def save(self, data: Dict[str, Any]) -> None:
        pass

    @abstractmethod
    def update(self, mutator) -> Any:
        """Atomically load the data, pass it to `mutator(data)` (which mutates
        the dict in place), persist it, and return whatever the mutator returns."""
        pass


class InMemoryStateStore(StateStore):
    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self._data: Dict[str, Any] = dict(data or {})
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Any]:
        return dict(self._data)

    def save(self, data: Dict[str, Any]) -> None:
        self._data = dict(data)

    def update(self, mutator) -> Any:
        with self._lock:
            data = dict(self._data)
            result = mutator(data)
            self._data = dict(data)
            return result


class JsonFileStore(StateStore):
    """JSON file store with atomic writes. Missing/corrupt file reads as {}."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()  # guards same-process threads

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: Dict[str, Any]) -> None:
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.path)

    def update(self, mutator) -> Any:
        # In-process threads serialize on the lock; separate processes serialize
        # on an exclusive flock over a sidecar lock file. Together these make the
        # load-modify-save sequence atomic.
        with self._lock:
            with open(self.path + ".lock", "w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    data = self.load()
                    result = mutator(data)
                    self.save(data)
                    return result
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)


