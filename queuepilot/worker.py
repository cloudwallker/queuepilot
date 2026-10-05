"""Run outside database transactions; stale workers cannot commit results."""

import logging
from collections.abc import Callable
from threading import Event

from queuepilot.config import Settings
from queuepilot.providers import ProviderError, execute
from queuepilot.store import Store

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, store: Store, settings: Settings, executor: Callable = execute):
        self.store, self.settings, self.executor = store, settings, executor

    def run_once(self) -> bool:
        self.store.recover_expired(self.settings.retry_base)
        job = self.store.claim(self.settings.lease_seconds)
        if job is None:
            return False
        try:
            result = self.executor(job, self.settings)
        except ProviderError as exc:
            self.store.fail(job["id"], job["lease_token"], str(exc), self.settings.retry_base)
        except Exception:
            self.store.fail(
                job["id"], job["lease_token"], "unexpected_error", self.settings.retry_base
            )
        else:
            self.store.complete(job["id"], job["lease_token"], result)
        return True

    def run(self, stop: Event):
        while not stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                # Never include arbitrary upstream exceptions or credentials in logs.
                logger.warning("Worker iteration unavailable; will retry")
                worked = False
            if not worked:
                stop.wait(self.settings.poll_seconds)
