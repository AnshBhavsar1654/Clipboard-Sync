"""Built-exe smoke test (plan §5.2): launch the packaged app headless, pair,
upload a file, download it back byte-identical, sync a clip over the socket.

Run:  python build_exe.py --target headless
      pytest tests/smoke_exe.py --exe=dist/ClipBoardSyncHeadless/ClipBoardSyncHeadless.exe
Skipped when --exe is unset. A missing hidden import fails here, not in release.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest


def _wait_for(predicate, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for: {what}")


def test_built_exe_smoke(request, tmp_path):
    exe = request.config.getoption("--exe")
    if not exe:
        pytest.skip("needs --exe=path/to/built/exe")
    exe = Path(exe)
    assert exe.is_file(), f"exe not found: {exe}"

    from server.net import find_open_port

    port = find_open_port(8123, count=20)
    base = f"http://127.0.0.1:{port}"
    state_file = tmp_path / "state.json"

    env = dict(os.environ)
    env["CLIPBOARDSYNC_STATE_FILE"] = str(state_file)
    env["CLIPBOARDSYNC_SHARED_DIR"] = str(tmp_path / "shared")
    env["CLIPBOARDSYNC_HISTORY_DB"] = str(tmp_path / "history.db")

    proc = subprocess.Popen(
        [str(exe), "--headless", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    # Drain stdout so a chatty child can never block on a full pipe.
    threading.Thread(target=proc.stdout.read, daemon=True).start()
    try:
        import httpx

        def _healthy():
            try:
                r = httpx.get(f"{base}/api/health", timeout=2)
                return r.status_code == 200
            except Exception:
                return False

        _wait_for(_healthy, 90, "server health")

        pin = _wait_for(
            lambda: json.loads(state_file.read_text())["pairing_pin"]
            if state_file.is_file() else None,
            30, "pairing PIN in state file",
        )
        with httpx.Client(base_url=base) as c:
            assert c.post("/api/pair", json={"pin": pin, "name": "smoke"}).status_code == 200
            hist = c.get("/api/history")
            assert hist.status_code == 200

            blob = b"smoke-bytes-" * 1000
            up = c.post("/api/upload", files={"file": ("smoke.bin", blob)})
            assert up.status_code == 200, up.text
            url = up.json()["url"]
            down = c.get(url)
            assert down.status_code == 200 and down.content == blob

            async def _ws_roundtrip():
                import websockets

                cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/ws", max_size=None,
                    additional_headers={"Cookie": cookie},
                ) as ws:
                    await ws.send(json.dumps({"type": "hello", "device_id": "smoke", "kind": "test"}))
                    await ws.send(json.dumps({"device_id": "smoke", "type": "text", "content": "smoke-clip"}))
                    await asyncio.sleep(1)

            asyncio.run(_ws_roundtrip())
            items = c.get("/api/history").json()["items"]
            assert any(i.get("content") == "smoke-clip" for i in items), items
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
