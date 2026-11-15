import asyncio
import json
import socket
import threading
import time

import pytest
from mcp.client import Client

from snapshotkit.mcp_server import build_server
from snapshotkit.snapshots import SnapshotCache
from snapshotkit.store import InMemoryStore, digest_index_key, manifest_key, snapshot_key
from snapshotkit.tarball import Entry, write_normalized_targz

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "a" * 64
SNAP_ID = "sha256-" + "a" * 64


def test_streamable_http_wire(tmp_path):
    store = InMemoryStore()
    tar = tmp_path / "s.tar.gz"
    write_normalized_targz([Entry("src/app.py", 0o644, b"print(1)\n")], tar)
    store.put(snapshot_key("proj", DIGEST), tar.read_bytes())
    store.put(manifest_key("proj", DIGEST), b'{"project": "proj"}')
    store.put(digest_index_key(DIGEST), json.dumps({"project": "proj", "snapshot_digest": DIGEST}).encode())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = build_server({}, {}, SnapshotCache(store, tmp_path / "cache"), host="127.0.0.1", port=port)
    threading.Thread(
        target=lambda: server.run(transport="streamable-http", host="127.0.0.1", port=port), daemon=True
    ).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    async def run():
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            names = {t.name for t in (await c.list_tools()).tools}
            await c.call_tool("get_snapshot", {"ref": DIGEST})
            r = await c.call_tool("read_file", {"snapshot_id": SNAP_ID, "path": "src/app.py"})
            return names, json.loads(r.content[0].text)

    names, out = asyncio.run(run())
    assert "read_file" in names and "search_code" in names
    assert out["content"] == "print(1)\n"
