"""A hello run on the live stack writes trace rows (Phase 0 task 5 exit criterion)."""

import json
import os
import threading
import urllib.request
import uuid

from services.agents.common.trace import TraceWriter, read_trace


def test_hello_run_writes_trace_rows() -> None:
    req = urllib.request.Request(
        "http://hello:8000/hello/run",
        data=json.dumps({"text": "smoke", "on_behalf_of": "smoke-test"}).encode(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        run_id = uuid.UUID(json.loads(resp.read())["run_id"])

    rows = read_trace(os.environ["DATABASE_URL"], run_id)

    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert [r["kind"] for r in rows] == ["message", "decision", "outcome"]
    assert {r["agent_id"] for r in rows} == {"hello"}
    assert {r["on_behalf_of"] for r in rows} == {"smoke-test"}
    assert rows[0]["payload"] == {"direction": "in", "text": "smoke"}


def test_concurrent_writers_in_one_run_get_unique_seqs() -> None:
    dsn = os.environ["DATABASE_URL"]
    run_id = uuid.uuid4()

    def write_many(agent_id: str) -> None:
        with TraceWriter(dsn, agent_id, run_id=run_id) as writer:
            for i in range(20):
                writer.write("message", {"i": i})

    threads = [threading.Thread(target=write_many, args=(f"agent-{n}",)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [r["seq"] for r in read_trace(dsn, run_id)] == list(range(1, 61))
