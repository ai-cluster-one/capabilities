import threading
import time
from dispatcher import Dispatcher


def test_same_conversation_runs_serially():
    order = []
    gate = threading.Event()

    def run_job(job):
        order.append(("start", job))
        if job == "a":
            gate.wait(2)          # hold job a until released
        order.append(("end", job))

    d = Dispatcher(run_job, max_parallel=4)
    d.submit("C1", "a")
    d.submit("C1", "b")
    time.sleep(0.2)
    # b must not have started while a is still running
    assert ("start", "b") not in order
    gate.set()
    d.shutdown()
    assert order == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]


def test_different_conversations_run_in_parallel():
    running = set()
    peak = [0]
    lock = threading.Lock()
    hold = threading.Event()

    def run_job(job):
        with lock:
            running.add(job)
            peak[0] = max(peak[0], len(running))
        hold.wait(2)
        with lock:
            running.discard(job)

    d = Dispatcher(run_job, max_parallel=4)
    d.submit("C1", "a")
    d.submit("C2", "b")
    time.sleep(0.2)
    assert peak[0] == 2
    hold.set()
    d.shutdown()


def test_global_cap_limits_concurrency():
    running = [0]
    peak = [0]
    lock = threading.Lock()
    hold = threading.Event()

    def run_job(job):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        hold.wait(2)
        with lock:
            running[0] -= 1

    d = Dispatcher(run_job, max_parallel=2)
    for i in range(5):
        d.submit(f"C{i}", i)
    time.sleep(0.3)
    assert peak[0] <= 2
    hold.set()
    d.shutdown()
