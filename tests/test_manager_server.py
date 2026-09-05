"""Real API/worker integration with a deterministic browser, no account required."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from selenium.common.exceptions import InvalidSessionIdException
from starlette.websockets import WebSocketDisconnect

from xhs_console.browser import NeedsInteraction, XHS_HOME
from xhs_console.config import Settings
from xhs_console.manager import JobManager
from xhs_console.server import create_app

URL = "https://www.xiaohongshu.com/explore/69d886d30000000020038960?xsec_token=test-secret"


class FakeBrowser:
    def __init__(self, project_dir, emit, checkpoint):
        self.checkpoint = checkpoint
        self.owner = threading.get_ident()
        self.url = "about:blank"
        self.gated = False
        self.failures = 0
        self.closed = False
        self.images = []
        self.open_options = {}
        self.visited = []

    def owned(self):
        assert threading.get_ident() == self.owner, "Concurrent WebDriver access"

    def open(self, **kwargs):
        self.owned()
        self.open_options = dict(kwargs)

    def close(self):
        self.owned()
        self.closed = True

    def info(self):
        self.owned()
        if self.closed:
            raise InvalidSessionIdException("Browser closed")
        return dict(url=self.url, title="Test", width=1280, height=800, browser="fake",
                    network_mode="direct" if self.open_options.get("direct_connection", True) else "system")

    def screenshot(self):
        self.owned()
        return b"\xff\xd8\xff\xd9"

    def navigate(self, url):
        self.owned()
        self.url = url
        self.visited.append(url)

    def prepare_collection(self, config):
        self.owned()
        self.navigate(XHS_HOME)
        if self.gated:
            raise NeedsInteraction("请登录后继续")

    def interact(self, action):
        self.owned()
        if action["type"] == "click":
            self.gated = False

    def search(self, config):
        self.owned()
        self.checkpoint(0.3)
        return [URL]

    def extract(self, url, config):
        self.owned()
        self.url = url
        if self.gated:
            raise NeedsInteraction("请登录后继续")
        self.checkpoint(0.25)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("test extraction error")
        return dict(note_id="69d886d30000000020038960", url=url, title="测试笔记", author="作者",
                    content="实际测试正文", published_at="", location="", type="text", images=self.images, source="test")


class FakeRemote:
    """A deterministic transport; input never touches the WebDriver fixture."""
    def __init__(self, connected=True):
        self.connected = connected
        self.actions = []
        self.dispatch_threads = []
        self.released = threading.Event()
        self.barrier_calls = 0
        self._lock = threading.Lock()

    def snapshot(self):
        return dict(data=b"\xff\xd8\xff\xd9" if self.connected else None, sequence=1,
                    width=1280, height=800, connected=self.connected, error=None)

    def dispatch(self, action):
        if not self.connected:
            raise ValueError("浏览器实时连接尚未就绪")
        with self._lock:
            self.actions.append(dict(action))
            self.dispatch_threads.append(threading.get_ident())
        if action["type"] == "release":
            self.released.set()

    def barrier(self, timeout=2):
        self.barrier_calls += 1
        if not self.connected:
            raise AssertionError("A fresh browser must not wait for a manual-input barrier")
        return True


class StreamingFakeBrowser(FakeBrowser):
    def __init__(self, *args):
        super().__init__(*args)
        self.remote = FakeRemote()

    def close(self):
        super().close()
        self.remote.connected = False


class ColdStreamingFakeBrowser(StreamingFakeBrowser):
    def __init__(self, *args):
        super().__init__(*args)
        self.remote.connected = False


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = JobManager(self.root, browser_factory=FakeBrowser)
        self.config = Settings(keyword="测试资料", mode="urls", urls=[URL], download_images=False, retries=0)

    def tearDown(self):
        self.manager.shutdown()
        self.tmp.cleanup()

    def wait(self, status, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.manager.snapshot()
            if snapshot["status"] in status:
                return snapshot
            time.sleep(0.02)
        self.fail(str(self.manager.snapshot()))

    def test_run_export_skip_and_single_driver_owner(self):
        self.manager.start(self.config)
        result = self.wait({"completed", "error"})
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["counts"]["success"], 1)
        self.assertTrue(result["exports"])
        self.assertNotIn("test-secret", str(result["results"]))
        self.assertEqual(self.manager._browser.visited[0], XHS_HOME)
        self.assertTrue(self.manager._browser.open_options["direct_connection"])
        self.manager.start(self.config)
        result = self.wait({"completed", "error"})
        self.assertEqual(result["counts"]["skipped"], 1)

    def test_gate_manual_action_resume_same_session(self):
        self.manager.open_browser()
        opened = self.wait({"ready"})
        self.assertEqual(opened["browser"]["url"], "about:blank")
        self.assertIn("可自由访问网页", opened["message"])
        browser = self.manager._browser
        browser.gated = True
        self.manager.start(self.config)
        self.wait({"waiting_login"})
        self.manager.interact({"type": "click"})
        time.sleep(0.25)
        self.manager.resume()
        result = self.wait({"completed", "error"})
        self.assertEqual(result["counts"]["success"], 1)
        self.assertIs(self.manager._browser, browser)

    def test_pause_stop_and_conflicting_start(self):
        self.manager.start(self.config.model_copy(update={"mode": "search"}))
        with self.assertRaises(ValueError):
            self.manager.start(self.config)
        self.manager.pause()
        self.wait({"paused"})
        self.manager.stop()
        self.wait({"stopped"})

    def test_failed_retry(self):
        self.manager.open_browser()
        self.wait({"ready"})
        self.manager._browser.failures = 1
        self.manager.start(self.config)
        result = self.wait({"completed"})
        self.assertEqual(result["counts"]["failed"], 1)
        self.manager.retry()
        result = self.wait({"completed"})
        self.assertEqual(result["counts"]["success"], 1)

    def test_short_link_skips_canonical_existing_note(self):
        self.manager.start(self.config)
        self.wait({"completed"})
        config = self.config.model_copy(update={"urls": ["https://xhslink.com/a/TestLink"]})
        self.manager.start(config)
        result = self.wait({"completed"})
        self.assertEqual(result["counts"]["skipped"], 1)

    def test_text_only_run_can_add_images_and_partial_is_retryable(self):
        self.manager.open_browser()
        self.wait({"ready"})
        self.manager._browser.images = ["https://invalid.example/image.jpg"]
        self.manager.start(self.config)
        self.wait({"completed"})
        self.manager.start(self.config.model_copy(update={"download_images": True}))
        result = self.wait({"completed"})
        self.assertEqual(result["counts"]["skipped"], 0)
        self.assertEqual(result["counts"]["partial"], 1)
        self.assertEqual(result["retryable_count"], 1)
        self.assertEqual(result["results"][0]["status"], "partial")

    def test_stop_during_image_preserves_and_reports_partial_text(self):
        self.manager.open_browser()
        self.wait({"ready"})
        self.manager._browser.images = ["https://sns-webpic-qc.xhscdn.com/test.jpg"]

        def cancel_download(url, destination, config, checkpoint):
            self.manager.stop()
            checkpoint()

        with patch("xhs_console.storage._download_image", cancel_download):
            self.manager.start(self.config.model_copy(update={"download_images": True}))
            result = self.wait({"stopped", "error"})
        self.assertEqual(result["status"], "stopped", result)
        self.assertEqual(result["counts"]["partial"], 1)
        self.assertTrue(result["exports"])
        self.assertIsNone(result["error"])

    def test_closed_browser_can_be_reopened(self):
        self.manager.open_browser()
        self.wait({"ready"})
        old = self.manager._browser
        old.closed = True
        result = self.wait({"error"})
        self.assertFalse(result["browser_open"])
        self.manager.open_browser()
        self.wait({"ready"})
        self.assertIsNot(self.manager._browser, old)

    def test_fresh_job_does_not_wait_for_unconnected_stream_barrier(self):
        self.manager.shutdown()
        self.manager = JobManager(self.root, browser_factory=ColdStreamingFakeBrowser)
        self.manager.start(self.config)
        result = self.wait({"completed", "error"})
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["counts"]["success"], 1)
        self.assertEqual(self.manager._browser.remote.barrier_calls, 0)
        self.assertTrue(self.manager._thread.is_alive())

    def test_open_unconnected_stream_does_not_block_later_job(self):
        self.manager.shutdown()
        self.manager = JobManager(self.root, browser_factory=ColdStreamingFakeBrowser)
        self.manager.open_browser(headless=self.config.headless, browser=self.config.browser)
        self.wait({"ready"})
        browser = self.manager._browser
        self.manager.start(self.config)
        result = self.wait({"completed", "error"})
        self.assertEqual(result["status"], "completed", result)
        self.assertIs(self.manager._browser, browser)
        self.assertEqual(browser.remote.barrier_calls, 0)


class ApiTests(unittest.TestCase):
    def test_corrupt_config_does_not_block_repair_via_console(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime").mkdir()
            broken = root / "runtime/config.json"
            broken.write_text("invalid JSON", encoding="utf-8")
            app = create_app(root, manager_factory=lambda path: JobManager(path, FakeBrowser))
            with TestClient(app, base_url="http://127.0.0.1") as client:
                self.assertTrue(client.get("/api/state").json()["error"])
                self.assertEqual(client.get("/api/config").status_code, 200)
                self.assertEqual(broken.read_text(encoding="utf-8"), "invalid JSON")
                self.assertEqual(client.put("/api/config", json=Settings().model_dump()).status_code, 200)

    def test_validation_origin_frame_and_download_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(root, manager_factory=lambda path: JobManager(path, FakeBrowser))
            with TestClient(app, base_url="http://127.0.0.1") as client:
                self.assertEqual(client.get("/api/state").status_code, 200)
                self.assertEqual(client.get("/api/frame.jpg").status_code, 204)
                self.assertEqual(client.post("/api/jobs/stop").status_code, 409)
                settings = Settings().model_dump()
                settings["max_notes"] = 100000
                self.assertEqual(client.put("/api/config", json=settings).status_code, 422)
                self.assertEqual(client.post("/api/browser/open", json={}, headers={"Origin": "https://evil.example"}).status_code, 403)
                self.assertEqual(client.get("/api/files/..%2F..%2Fparameter.txt").status_code, 404)
                self.assertEqual(client.post("/api/browser/action", json={"type": "click", "x": 2}).status_code, 422)
                config = Settings(keyword="API测试", mode="urls", urls=[URL], download_images=False).model_dump()
                self.assertEqual(client.post("/api/jobs/start", json=config).status_code, 200)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = client.get("/api/state").json()
                    if state["status"] in {"completed", "error"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(state["status"], "completed", state)
                source = client.get(state["results"][0]["url"], follow_redirects=False)
                self.assertEqual(source.status_code, 307)
                self.assertIn("xsec_token=test-secret", source.headers["location"])
                self.assertTrue(state["exports"])
                for export in state["exports"]:
                    response = client.get(export["url"])
                    self.assertEqual(response.status_code, 200, export)
                    self.assertTrue(response.content)


class BrowserStreamApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xhs-stream-api-")
        self.root = Path(self.tmp.name)
        static = self.root / "static"
        static.mkdir()
        for name in ("index.html", "browser.html"):
            (static / name).write_text("<!doctype html><title>Local fixture</title>", encoding="utf-8")
        self.app = create_app(self.root, manager_factory=lambda path: JobManager(path, StreamingFakeBrowser))
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.manager = self.app.state.manager

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def open_browser(self):
        self.assertEqual(self.client.post("/api/browser/open", json={}).status_code, 200)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.manager.snapshot()["status"] == "ready":
                return self.manager._browser.remote
            time.sleep(.02)
        self.fail(str(self.manager.snapshot()))

    def receive_packet(self, socket, kind):
        # State heartbeats bound each iteration even if an expected ack is absent.
        for _ in range(30):
            message = socket.receive()
            self.assertEqual(message["type"], "websocket.send", message)
            if "text" in message:
                packet = json.loads(message["text"])
                if packet.get("type") == kind:
                    return packet
        self.fail(f"No {kind} message arrived")

    def test_same_origin_stream_sends_state_jpeg_and_dispatches_native_input(self):
        remote = self.open_browser()
        with self.client.websocket_connect("ws://127.0.0.1/api/browser/stream", headers={"Origin": "http://127.0.0.1"}) as socket:
            state = socket.receive_json()
            self.assertEqual(state["type"], "state")
            self.assertTrue(state["stream_available"])
            self.assertTrue(state["manual_enabled"])
            self.assertEqual(socket.receive_bytes(), b"\xff\xd8\xff\xd9")
            actions = [
                {"type": "pointer", "event": "down", "x": .2, "y": .3, "button": "left", "buttons": 1},
                {"type": "pointer", "event": "move", "x": .4, "y": .5, "buttons": 1},
                {"type": "pointer", "event": "up", "x": .4, "y": .5, "buttons": 0},
                {"type": "text", "text": "中文测试"},
                {"type": "key", "key": "Enter", "code": "Enter", "event": "down"},
                {"type": "key", "key": "Enter", "code": "Enter", "event": "up"},
                {"type": "wheel", "x": .4, "y": .5, "delta_y": 400},
                {"type": "resize", "width": 1100, "height": 640},
                {"type": "resize", "width": 1100, "height": 640, "quality": "high"},
                {"type": "resize", "width": 1100, "height": 640, "quality": "smooth"},
            ]
            for action in actions:
                socket.send_json({"type": "input", "action": action})
                self.assertEqual(self.receive_packet(socket, "ack"), {"type": "ack"})
            self.assertEqual(remote.actions, actions)
            self.assertNotIn(self.manager._browser.owner, remote.dispatch_threads)
        self.assertTrue(remote.released.wait(1), "WebSocket disconnect did not release held input")

    def test_widest_connected_viewer_controls_shared_browser_viewport(self):
        remote = self.open_browser()
        headers = {"Origin": "http://127.0.0.1"}
        with self.client.websocket_connect("ws://127.0.0.1/api/browser/stream", headers=headers) as wide:
            self.receive_packet(wide, "state")
            wide.send_json({"type": "input", "action": {"type": "resize", "width": 1500, "height": 760, "quality": "high"}})
            self.assertEqual(self.receive_packet(wide, "ack"), {"type": "ack"})
            with self.client.websocket_connect("ws://127.0.0.1/api/browser/stream", headers=headers) as narrow:
                self.receive_packet(narrow, "state")
                narrow.send_json({"type": "input", "action": {"type": "resize", "width": 420, "height": 720, "quality": "high"}})
                self.assertEqual(self.receive_packet(narrow, "ack"), {"type": "ack"})
                narrow.send_json({"type": "input", "action": {"type": "resize", "width": 1680, "height": 820, "quality": "high"}})
                self.assertEqual(self.receive_packet(narrow, "ack"), {"type": "ack"})
            deadline = time.monotonic() + 1
            while len([item for item in remote.actions if item["type"] == "resize"]) < 3 and time.monotonic() < deadline:
                time.sleep(.01)
        sizes = [(item["width"], item["height"]) for item in remote.actions if item["type"] == "resize"]
        self.assertEqual(sizes, [(1500, 760), (1680, 820), (1500, 760)])
        self.assertEqual(remote.actions[-1], {"type": "release"})

    def test_missing_cross_host_cross_port_and_cross_scheme_origins_are_rejected(self):
        for origin in (None, "null", "https://evil.example", "http://localhost", "http://127.0.0.1:8766", "https://127.0.0.1"):
            headers = {} if origin is None else {"Origin": origin}
            with self.subTest(origin=origin), self.assertRaises(WebSocketDisconnect) as rejected:
                with self.client.websocket_connect("ws://127.0.0.1/api/browser/stream", headers=headers):
                    self.fail("Cross-origin browser control was accepted")
            self.assertEqual(rejected.exception.code, 1008)

    def test_invalid_input_packets_never_reach_transport_and_connection_recovers(self):
        remote = self.open_browser()
        with self.client.websocket_connect("ws://127.0.0.1/api/browser/stream", headers={"Origin": "http://127.0.0.1"}) as socket:
            invalid = [
                "not-json", "[]", json.dumps({"type": "unknown"}),
                json.dumps({"type": "input", "action": {"type": "pointer", "event": "down", "x": 1.1}}),
                json.dumps({"type": "input", "action": {"type": "wheel", "y": -.1}}),
                json.dumps({"type": "input", "action": {"type": "pointer", "event": "down", "x": float("nan")}}),
                json.dumps({"type": "input", "action": {"type": "text", "text": "test", "script": "alert(1)"}}),
                json.dumps({"type": "input", "action": {"type": "key", "key": "Enter", "event": "move"}}),
                json.dumps({"type": "input", "action": {"type": "resize", "width": 3000, "height": 600}}),
                json.dumps({"type": "input", "action": {"type": "resize", "width": 1100, "height": 600, "quality": "invalid"}}),
                json.dumps({"type": "input", "action": {"type": "text", "text": "x" * 4001}}),
                "x" * 24001,
            ]
            for message in invalid:
                with self.subTest(message=message[:100]):
                    socket.send_text(message)
                    self.assertTrue(self.receive_packet(socket, "error")["message"])
                    self.assertEqual(remote.actions, [])
            valid = {"type": "text", "text": "继续输入"}
            socket.send_json({"type": "input", "action": valid})
            self.receive_packet(socket, "ack")
            self.assertEqual(remote.actions, [valid])

    def test_running_job_rejects_manual_input_but_allows_release(self):
        remote = self.open_browser()
        self.manager._update(status="running")
        with self.client.websocket_connect("ws://127.0.0.1/api/browser/stream", headers={"Origin": "http://127.0.0.1"}) as socket:
            self.assertFalse(socket.receive_json()["manual_enabled"])
            socket.send_json({"type": "input", "action": {"type": "pointer", "event": "down", "x": .2, "y": .3}})
            self.assertIn("暂停", self.receive_packet(socket, "error")["message"])
            self.assertEqual(remote.actions, [])
            socket.send_json({"type": "input", "action": {"type": "release"}})
            self.receive_packet(socket, "ack")
            self.assertEqual(remote.actions, [{"type": "release"}])

    def test_only_embedded_browser_routes_allow_same_origin_framing(self):
        for route in ("/browser", "/static/browser.html"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
                self.assertEqual(response.headers["Content-Security-Policy"], "frame-ancestors 'self'")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Content-Security-Policy"], "frame-ancestors 'none'")


if __name__ == "__main__":
    unittest.main()
