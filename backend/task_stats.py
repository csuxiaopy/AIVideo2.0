"""Cached completion watermarks; updated transactionally by database triggers."""
from __future__ import annotations

import threading
import time

from sqlalchemy import select

from backend.database import session_scope
from backend.models import BackgroundTaskCompletion


class CompletionStats:
    def __init__(self, sessions=session_scope, ttl=15.0, clock=time.monotonic):
        self.sessions, self.ttl, self.clock = sessions, ttl, clock
        self._lock = threading.Lock()
        self._expires = 0.0
        self._values = {}

    def latest(self):
        # Single-flight refresh: concurrent callers share one small-table read.
        with self._lock:
            if self.clock() >= self._expires:
                with self.sessions() as session:
                    values = dict(session.execute(select(
                        BackgroundTaskCompletion.kind,
                        BackgroundTaskCompletion.last_completed_at,
                    )).all())
                self._values = values
                self._expires = self.clock() + self.ttl
            return dict(self._values)
