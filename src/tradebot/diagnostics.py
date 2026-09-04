"""Thread-safe integration health history for the developer diagnostics endpoint."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import Lock
import traceback

@dataclass
class IntegrationHealth:
    status: str = "not_checked"
    last_success: str | None = None
    last_error: str | None = None
    last_traceback: str | None = None

class Diagnostics:
    def __init__(self):
        self._lock, self._items = Lock(), {}
    def success(self, name: str) -> None:
        with self._lock:
            item = self._items.setdefault(name, IntegrationHealth())
            item.status = "connected"
            item.last_success = datetime.now(timezone.utc).isoformat()
            item.last_error = None
            item.last_traceback = None
    def failure(self, name: str, exc: BaseException) -> None:
        with self._lock:
            item = self._items.setdefault(name, IntegrationHealth())
            item.status = "error"
            item.last_error = f"{type(exc).__name__}: {exc}"
            item.last_traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    def snapshot(self, names: tuple[str, ...]) -> dict:
        with self._lock:
            return {name: asdict(self._items.get(name, IntegrationHealth())) for name in names}

diagnostics = Diagnostics()
