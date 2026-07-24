"""Threaded dispatcher: per-conversation FIFO ordering + a global concurrency cap.

slack_sdk delivers events on its own threads; the handler only ACKs + enqueues.
At most one job per conversation runs at a time (coherent threads); across
conversations up to max_parallel run (no head-of-line blocking between people)."""

import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor


class Dispatcher:
    def __init__(self, run_job, *, max_parallel=3):
        self._run_job = run_job
        self._pool = ThreadPoolExecutor(max_workers=max_parallel)
        self._lock = threading.Lock()
        self._queues = defaultdict(deque)
        self._busy = set()
        self._futures = []

    def submit(self, conv, job) -> None:
        with self._lock:
            self._queues[conv].append(job)
            self._maybe_dispatch(conv)

    def _maybe_dispatch(self, conv) -> None:
        # caller holds the lock
        if conv in self._busy:
            return
        q = self._queues.get(conv)
        if not q:
            return
        job = q.popleft()
        self._busy.add(conv)
        # Drop completed futures so the list stays bounded over a long daemon life.
        self._futures = [f for f in self._futures if not f.done()]
        self._futures.append(self._pool.submit(self._run, conv, job))

    def _run(self, conv, job) -> None:
        try:
            self._run_job(job)
        finally:
            with self._lock:
                self._busy.discard(conv)
                self._maybe_dispatch(conv)

    def shutdown(self) -> None:
        # Wait for in-flight jobs (and any they re-dispatch) to finish before
        # closing the pool, so a job's finally-block re-dispatch never races the
        # pool shutdown. Job exceptions are swallowed here — shutdown must not
        # raise a worker's error — and the pool is always closed.
        try:
            while True:
                with self._lock:
                    pending = [f for f in self._futures if not f.done()]
                if not pending:
                    break
                for future in pending:
                    try:
                        future.result()
                    except Exception:
                        pass
        finally:
            self._pool.shutdown(wait=True)
