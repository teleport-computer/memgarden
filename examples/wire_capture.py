"""Real JSON Lines subprocess, synthetic replies, no network or credentials.

Run: python examples/wire_capture.py
The host owns RPC deadlines and process cleanup. This serial demo is not a
multi-request production transport; keep durable input/retry work in your host.
"""
from __future__ import annotations

import json
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread


class JsonLinesClient:
    def __init__(self, database: Path):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "memgarden.cli", "serve", "--storage",
             "sqlite:///" + str(database)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
        )
        self.responses: Queue = Queue()
        self.sequence = 0
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def call(self, method, **params):
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        try:
            # Demo RPC deadline in seconds; choose your production deadline.
            line = self.responses.get(timeout=10)
        except Empty as exc:
            raise TimeoutError("MemGarden RPC deadline exceeded") from exc
        if line is None:
            raise RuntimeError("MemGarden exited before replying")
        response = json.loads(line)
        assert response["id"] == self.sequence
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["result"]

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.process.stdout.close()


def main():
    scope = {"tenant_id": "demo", "memory_owner_id": "user-42"}
    with TemporaryDirectory(prefix="memgarden-wire-demo-") as directory:
        client = JsonLinesClient(Path(directory) / "garden.db")
        try:
            manifest = client.call("manifest.get")
            assert manifest["capabilities"]["history_import"] is False
            state = client.call("capture.begin", scope=scope, locale="en",
                                window="User: Spicy food hurts my stomach.",
                                idempotency_key="conversation-1:turn-1")
            assert state["status"] == "needs_model" and state["next_prompt"]
            # In your runtime: call YOUR model with state['next_prompt'].
            reply = json.dumps({"cards": [{
                "action": "add", "summary": "Avoids spicy food",
                "content": "Spicy food causes stomach pain; choose mild dishes.",
                "retrieval_cues": ["spicy food", "mild dishes"],
                "importance_level": 3,
            }]})
            state = client.call("capture.feed", session_id=state["session_id"],
                                reply=reply, truncated=False)
            # A real host loops on needs_model; this fixed reply must finish once.
            assert state["status"] == "completed", state
            receipt = state["result"]
            assert not receipt.get("error") and receipt["written"], receipt
            exported = client.call("records.export", scope=scope)
            records = exported["items"]["records"]
            assert len(records) == 1 and records[0]["retrieval_cues"]
            context = client.call("context.get", scope=scope, query="spicy food")
            assert context["record_ids"] and context["blocks"]
            print("JSON Lines: handshake -> begin/feed -> stored -> exported -> recalled: PASS")
        finally:
            client.close()


if __name__ == "__main__":
    main()
