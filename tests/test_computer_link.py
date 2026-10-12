"""Two computers running ClipBoardSync can be linked and share clips both ways.

The host runs as a real server process; this process plays the joining
computer (its own hub plus a PeerLink) and connects over the LAN address, so
the host treats it as a remote, unpaired device.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets

from client.peer_link import PeerError, PeerLink
from server.main import hub
from server.net import find_open_port, lan_ips

pytestmark = pytest.mark.asyncio


async def _eventually(check, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met in time")


@pytest.fixture()
def host(tmp_path):
    ips = lan_ips()
    if not ips:
        pytest.skip("no LAN address to connect through")
    port = find_open_port(18200)
    state = tmp_path / "host_state.json"
    env = {
        **os.environ,
        "CLIPBOARDSYNC_STATE_FILE": str(state),
        "CLIPBOARDSYNC_HISTORY_DB": str(tmp_path / "host.db"),
        "CLIPBOARDSYNC_SHARED_DIR": str(tmp_path / "host_shared"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", str(port),
         "--log-level", "warning"],
        env=env, cwd=Path(__file__).parent.parent,
    )
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("host did not start")
        yield {"ip": ips[0], "port": port, "state": state}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _host_history(port: int) -> list[dict]:
    return httpx.get(f"http://127.0.0.1:{port}/api/history").json()["items"]


async def test_link_requires_the_right_code_then_syncs_both_ways(host, tmp_path):
    uploads = tmp_path / "local_shared"
    uploads.mkdir()
    hub.uploads_dir = uploads
    link = PeerLink(hub, uploads, "local-device", tmp_path)
    try:
        with pytest.raises(PeerError):
            await link.connect(host["ip"], host["port"], "000000")
        assert link.status == "disconnected"

        code = json.loads(host["state"].read_text(encoding="utf-8"))["pairing_pin"]
        await link.connect(host["ip"], host["port"], code)
        await _eventually(lambda: link.status == "connected")
        assert link.name == socket.gethostname()

        # this computer -> host (text)
        await hub.handle_message(None, {"type": "text", "content": "hello host", "device_id": "local-device"})
        await _eventually(lambda: any(i["content"] == "hello host" for i in _host_history(host["port"])))

        # host -> this computer (text)
        async with websockets.connect(f"ws://127.0.0.1:{host['port']}/ws") as ws:
            await ws.send(json.dumps({"type": "text", "content": "hello back", "device_id": "other-pc"}))
            await _eventually(lambda: any(i.content == "hello back" for i in hub._history))

        # this computer -> host (file bytes travel over HTTP and get a host-side link)
        (uploads / "abcdef0123.txt").write_bytes(b"local file body")
        await hub.handle_message(None, {
            "type": "file", "content": "File", "filename": "notes.txt", "filesize": 15,
            "file_url": "/api/files/abcdef0123.txt", "device_id": "local-device",
        })
        item = await _eventually(lambda: next(
            (i for i in _host_history(host["port"]) if i.get("filename") == "notes.txt"), None))
        body = httpx.get(f"http://127.0.0.1:{host['port']}{item['file_url']}").content
        assert body == b"local file body"

        # host -> this computer (file is copied into this computer's own store)
        up = httpx.post(f"http://127.0.0.1:{host['port']}/api/upload",
                        files={"file": ("report.pdf", b"host file body")}).json()
        async with websockets.connect(f"ws://127.0.0.1:{host['port']}/ws") as ws:
            await ws.send(json.dumps({
                "type": "file", "content": "File", "filename": "report.pdf", "filesize": up["filesize"],
                "file_url": up["url"], "device_id": "other-pc",
            }))
            got = await _eventually(
                lambda: next((i for i in hub._history if i.filename == "report.pdf"), None))
        assert got.file_url.startswith("/api/files/") and got.file_url != up["url"]
        assert (uploads / got.file_url.rsplit("/", 1)[-1]).read_bytes() == b"host file body"

        # clips that came from the host are not echoed back to it
        assert sum(1 for i in _host_history(host["port"]) if i["content"] == "hello back") == 1
    finally:
        await link.disconnect()
    assert not (tmp_path / "peer.json").exists()


async def test_link_is_remembered_and_resumes(host, tmp_path):
    uploads = tmp_path / "local_shared"
    uploads.mkdir()
    code = json.loads(host["state"].read_text(encoding="utf-8"))["pairing_pin"]
    first = PeerLink(hub, uploads, "local-device", tmp_path)
    await first.connect(host["ip"], host["port"], code)
    await first.disconnect(forget=False)

    second = PeerLink(hub, uploads, "local-device", tmp_path)
    try:
        assert second.resume()  # token persisted: no code needed
        await _eventually(lambda: second.status == "connected")
    finally:
        await second.disconnect()


async def test_unreachable_computer_gives_a_plain_error(tmp_path):
    link = PeerLink(hub, tmp_path, "local-device", tmp_path)
    with pytest.raises(PeerError, match="Could not reach"):
        await link.connect("127.0.0.1", find_open_port(18300), "123456")
