"""优雅退出：容器与 systemd 发 SIGTERM 时，进程要等在途请求收尾再退出。

两条断言：
- `_Server.drain()` 在没有在途请求时立即返回，不拖住退出；
- `store serve` 收到 SIGTERM 后以 0 退出（不是被信号打断的 143），
  此时 docker stop / systemctl stop 才会等它，而不是直接 KILL。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from store.http_api import serve
from store.service import Store

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


class DrainTests(unittest.TestCase):
    def test_drain_returns_at_once_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "qilin.db", root / "blobs")
            server = serve(store, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(server.active_requests(), 0)
                started = time.monotonic()
                self.assertTrue(server.drain(timeout=2))
                self.assertLess(time.monotonic() - started, 1.0)
            finally:
                server.shutdown()
                server.server_close()
                store.close()
                thread.join(timeout=5)


@unittest.skipIf(os.name == "nt", "Windows 没有 SIGTERM 语义")
class SigtermTests(unittest.TestCase):
    def test_serve_exits_zero_on_sigterm(self) -> None:
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(REPO)
            process = subprocess.Popen(
                [
                    sys.executable, "-m", "store.cli", "serve",
                    "--host", "127.0.0.1", "--port", str(port),
                    "--db", str(root / "qilin.db"), "--blobs", str(root / "blobs"),
                ],
                cwd=str(REPO),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                self.assertTrue(_wait_for_port(port, timeout=20), "服务没有在 20 秒内起来")
                process.send_signal(signal.SIGTERM)
                code = process.wait(timeout=25)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                else:
                    process.communicate()
            self.assertEqual(code, 0, "SIGTERM 应当走正常退出，而不是被信号打断")


if __name__ == "__main__":
    unittest.main()
