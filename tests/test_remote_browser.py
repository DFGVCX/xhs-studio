"""Native-input regression coverage; optional Chrome tests use only local fixtures."""

import base64
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from xhs_console.browser import BrowserSession
from xhs_console.remote_browser import RemoteBrowserTransport, normalize_action


class FakeSocket:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))


class RemoteBrowserProtocolTests(unittest.TestCase):
    def setUp(self):
        self.remote = RemoteBrowserTransport("127.0.0.1:9222", "CDwindow-ABC")
        self.remote._ws = FakeSocket()
        self.remote._width, self.remote._height = 1001, 801
        self.remote._connected = True

    def test_only_local_browser_debugging_endpoint_is_accepted(self):
        for address in ("example.com:9222", "192.168.1.5:9222", "127.0.0.1:9222/other", "user:pw@127.0.0.1:9222"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                RemoteBrowserTransport(address, "ABC")
        self.assertEqual(self.remote.target_id, "ABC")

    def test_invalid_input_and_nonfinite_coordinates_are_rejected(self):
        for action in (None, {"type": "script"}, {"type": "pointer", "event": "down", "x": float("nan")},
                       {"type": "pointer", "event": "cancel"}, {"type": "text", "text": "x" * 4001},
                       {"type": "key", "key": "a", "event": "press"},
                       {"type": "resize", "width": 1100, "height": 640, "quality": "fake-hd"}):
            with self.subTest(action=str(action)[:70]), self.assertRaises(ValueError):
                normalize_action(action)

    def test_move_coalescing_preserves_click_and_drag_edges(self):
        for event, x in (("down", .1), ("move", .2), ("move", .3), ("up", .3), ("down", .5), ("up", .5)):
            self.remote.dispatch({"type": "pointer", "event": event, "x": x, "y": .5})
        actions = list(self.remote._actions)
        self.assertEqual([action["event"] for action in actions], ["down", "move", "up", "down", "up"])
        for action in actions:
            self.remote._execute(action)
        sent = [command["params"] for command in self.remote._ws.sent]
        self.assertEqual([event["buttons"] for event in sent], [1, 1, 0, 1, 0])
        self.assertEqual((sent[1]["x"], sent[1]["y"]), (300, 400))
        self.assertEqual(sent[1]["type"], "mouseMoved")

    def test_frame_is_acknowledged_and_dimensions_and_sequence_are_published(self):
        self.remote._quality = "smooth"
        self.remote._metrics_ready = True
        self.remote._receive({"method": "Page.screencastFrame", "params": {
            "data": base64.b64encode(b"jpeg-data").decode(), "sessionId": 45,
            "metadata": {"deviceWidth": 1100, "deviceHeight": 640}}})
        state = self.remote.snapshot()
        self.assertEqual((state["data"], state["sequence"], state["width"], state["height"]), (b"jpeg-data", 1, 1100, 640))
        self.assertEqual(self.remote._ws.sent[0]["method"], "Page.screencastFrameAck")
        self.assertEqual(self.remote._ws.sent[0]["params"], {"sessionId": 45})

    def test_release_clears_all_held_keys_and_buttons(self):
        for action in ({"type": "key", "event": "down", "key": "Control", "code": "ControlLeft", "modifiers": 2},
                       {"type": "pointer", "event": "down", "button": "left"},
                       {"type": "pointer", "event": "down", "button": "right"}, {"type": "release"}):
            self.remote._execute(normalize_action(action))
        self.assertEqual(self.remote._pressed_buttons, 0)
        self.assertEqual(self.remote._pressed_keys, {})
        releases = [item["params"] for item in self.remote._ws.sent[-3:]]
        self.assertEqual([item["type"] for item in releases], ["keyUp", "mouseReleased", "mouseReleased"])
        self.assertEqual(releases[-1]["buttons"], 0)

    def test_resize_bounds_and_native_select_all_command(self):
        self.remote._execute(normalize_action({"type": "resize", "width": 99999, "height": 1}))
        self.assertEqual(self.remote._ws.sent[0]["params"], {"width": 1920, "height": 240, "deviceScaleFactor": 2, "mobile": False})
        params = self.remote._key_params(normalize_action({"type": "key", "key": "a", "code": "KeyA", "modifiers": 2}))
        self.assertEqual(params["commands"], ["selectAll"])
        self.assertEqual(params["windowsVirtualKeyCode"], 65)
        self.assertNotIn("text", params)

    def test_quality_switch_native_capture_and_stale_response(self):
        resize = normalize_action({"type": "resize", "width": 1100, "height": 640})
        self.remote._execute(resize)
        self.remote._receive({"id": self.remote._metrics_id, "result": {}})
        self.assertEqual(self.remote._ws.sent[-1]["params"], {
            "format": "jpeg", "quality": 92, "maxWidth": 3840, "maxHeight": 2400, "everyNthFrame": 1})
        self.remote._compositor_ready = True
        self.remote._maybe_capture()
        capture = self.remote._ws.sent[-1]
        self.assertEqual(capture["method"], "Page.captureScreenshot")
        self.assertEqual(capture["params"], {"format": "jpeg", "quality": 92,
                         "fromSurface": True, "captureBeyondViewport": False})
        before = len(self.remote._ws.sent)
        self.remote._maybe_capture()
        self.assertEqual(len(self.remote._ws.sent), before, "Only one native capture may be pending")
        # Even an unexpectedly late response cannot replace a new mode's frame.
        self.remote._execute(normalize_action({**resize, "quality": "smooth"}))
        self.remote._receive({"id": capture["id"], "result": {"data": base64.b64encode(b"stale").decode()}})
        self.assertIsNone(self.remote.snapshot()["data"])
        self.remote._receive({"id": self.remote._metrics_id, "result": {}})
        self.assertEqual(self.remote._ws.sent[-1]["params"], {
            "format": "jpeg", "quality": 80, "maxWidth": 1920, "maxHeight": 1200, "everyNthFrame": 1})
        before = len(self.remote._ws.sent)
        self.remote._execute(normalize_action({**resize, "quality": "smooth"}))
        self.assertEqual(len(self.remote._ws.sent), before, "Identical resize must not restart the stream")
        self.remote._execute(resize)
        self.assertEqual(self.remote._ws.sent[-2]["method"], "Page.stopScreencast")
        self.assertEqual(self.remote._ws.sent[-1]["params"]["deviceScaleFactor"], 2)

    def test_high_mode_ignores_low_resolution_screencast_but_acknowledges_it(self):
        self.remote._receive({"method": "Page.screencastFrame", "params": {
            "data": base64.b64encode(b"low-res").decode(), "sessionId": 9,
            "metadata": {"deviceWidth": 2000, "deviceHeight": 1600}}})
        self.assertIsNone(self.remote.snapshot()["data"])
        self.assertEqual(self.remote._ws.sent[-1]["method"], "Page.screencastFrameAck")

    def test_high_mode_captures_before_edge_emits_a_screencast_frame(self):
        self.remote._metrics_ready = True
        self.remote._compositor_ready = False
        self.remote._maybe_capture()
        self.assertEqual(self.remote._ws.sent[-1]["method"], "Page.captureScreenshot")

    def test_resize_uses_css_dimensions_for_pointer_even_when_metadata_is_physical(self):
        self.remote._execute(normalize_action({"type": "resize", "width": 1100, "height": 640, "quality": "smooth"}))
        self.remote._receive({"id": self.remote._metrics_id, "result": {}})
        self.remote._receive({"method": "Page.screencastFrame", "params": {
            "data": base64.b64encode(b"jpeg").decode(), "sessionId": 3,
            "metadata": {"deviceWidth": 2200, "deviceHeight": 1280}}})
        self.remote._execute(normalize_action({"type": "pointer", "event": "down", "x": .5, "y": .5}))
        self.assertEqual((self.remote.snapshot()["width"], self.remote.snapshot()["height"]), (1100, 640))
        self.assertEqual((self.remote._ws.sent[-1]["params"]["x"], self.remote._ws.sent[-1]["params"]["y"]), (549.5, 319.5))


@unittest.skipUnless(os.environ.get("XHS_RUN_BROWSER_TESTS") == "1", "Set XHS_RUN_BROWSER_TESTS=1 for local browser fixture tests")
class RemoteBrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = tempfile.TemporaryDirectory(prefix="xhs-remote-fixture-")
        cls.session = BrowserSession(Path(cls.profile.name), lambda *_: None, time.sleep)
        cls.session.open(headless=True)
        cls.remote = cls.session.remote
        if cls.remote is None:
            cls.session.close()
            cls.profile.cleanup()
            raise AssertionError("Chrome did not expose a local DevTools transport")
        end = time.monotonic() + 8
        while not cls.remote.snapshot()["data"] and time.monotonic() < end:
            time.sleep(.03)
        cls.initial_frame = cls.remote.snapshot()
        cls.initial_dpr = cls.session.driver.execute_script("return devicePixelRatio")

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.profile.cleanup()

    def wait_for(self, predicate, message, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.03)
        self.fail(message + ": " + str({k: v for k, v in self.remote.snapshot().items() if k != "data"}))

    def setUp(self):
        self.session.driver.get("data:text/html;charset=utf-8," + quote("""<!doctype html><meta charset='utf-8'>
        <style>body{margin:0;height:2500px;background:linear-gradient(#fafafa,#aff)}
        input{position:absolute;left:30px;top:30px;width:300px;height:40px;font-size:22px}
        #box{position:absolute;left:30px;top:130px;width:100px;height:100px;background:#f80;touch-action:none}
        #resolution{position:absolute;left:400px;top:80px;width:160px;height:80px;
        background:repeating-linear-gradient(90deg,#000 0,#000 .5px,#fff .5px,#fff 1px)}
        #fine-text{position:absolute;left:400px;top:180px;font-size:12px}</style>
        <input id='entry'><div id='box'>DRAG</div><div id='resolution'></div><p id='fine-text'>原生高清小字 HD 123</p>
        <script>let active=false; const box=document.querySelector('#box');
        box.addEventListener('pointerdown',e=>{active=true;box.setPointerCapture(e.pointerId)});
        box.addEventListener('pointermove',e=>{if(active){box.style.left=(e.clientX-50)+'px';box.style.top=(e.clientY-50)+'px'}});
        box.addEventListener('pointerup',()=>{active=false;window.dragReleased=true});
        document.querySelector('input').addEventListener('keydown',e=>{if(e.key==='Enter')window.entered=true});</script>"""))
        self.wait_for(lambda: self.remote.snapshot()["connected"], "No live CDP frame")
        self.remote.dispatch({"type": "resize", "width": 1100, "height": 640})
        self.assertTrue(self.remote.barrier())
        self.wait_for(lambda: self.remote.snapshot()["width"] == 1100 and self.remote.snapshot()["height"] == 640
                      and self.frame_size() == (2200, 1280),
                      "Viewport resize was not delivered")

    def frame_size(self):
        from PIL import Image
        data = self.remote.snapshot()["data"]
        if not data:
            return None
        with Image.open(io.BytesIO(data)) as image:
            return image.size

    def switch_quality(self, quality):
        self.remote.dispatch({"type": "resize", "width": 1100, "height": 640, "quality": quality})
        self.assertTrue(self.remote.barrier())
        expected = (2200, 1280) if quality == "high" else (1100, 640)
        self.wait_for(lambda: self.frame_size() == expected, "Quality switch did not deliver its native image size")

    def pointer(self, event, x, y, **kwargs):
        state = self.remote.snapshot()
        self.remote.dispatch({"type": "pointer", "event": event, "x": x / (state["width"] - 1),
                              "y": y / (state["height"] - 1), **kwargs})

    def key(self, key, code, modifiers=0):
        for event in ("down", "up"):
            self.remote.dispatch({"type": "key", "event": event, "key": key, "code": code, "modifiers": modifiers})

    def test_live_frame_click_chinese_input_native_keys_and_select_all(self):
        self.pointer("down", 100, 50)
        self.pointer("up", 100, 50)
        self.remote.dispatch({"type": "text", "text": "中文输入"})
        self.key("a", "KeyA")
        self.assertTrue(self.remote.barrier())
        value = self.session.driver.execute_script("return document.querySelector('input').value")
        self.assertEqual(value, "中文输入a")
        self.key("a", "KeyA", 2)
        self.remote.dispatch({"type": "text", "text": "新的内容"})
        self.key("Enter", "Enter")
        self.assertTrue(self.remote.barrier())
        self.assertEqual(self.session.driver.execute_script("return document.querySelector('input').value"), "新的内容")
        self.assertTrue(self.session.driver.execute_script("return window.entered"))
        from PIL import Image
        with Image.open(io.BytesIO(self.remote.snapshot()["data"])) as frame:
            self.assertEqual(frame.size, (2200, 1280))
            self.assertEqual(frame.format, "JPEG")

    def test_startup_is_high_dpi_before_any_viewer_resize(self):
        from PIL import Image
        state = self.initial_frame
        self.assertEqual(self.initial_dpr, 2)
        self.assertEqual(state["quality"], "high")
        with Image.open(io.BytesIO(state["data"])) as frame:
            self.assertEqual(frame.size, (state["width"] * 2, state["height"] * 2))

    def test_high_dpi_resolves_half_css_pixel_details_without_software_upscaling(self):
        from PIL import Image
        self.assertEqual(self.session.driver.execute_script("return [innerWidth,innerHeight,devicePixelRatio]"), [1100, 640, 2])
        # A software enlargement of the 1x image cannot recover these alternating
        # half-CSS-pixel stripes. Each native 2x pixel must retain its own contrast.
        def detailed_frame():
            data = self.remote.snapshot()["data"]
            with Image.open(io.BytesIO(data)) as frame:
                if frame.size != (2200, 1280):
                    return False
                gray = frame.convert("L")
                values = [gray.getpixel((x, 200)) for x in range(830, 990)]
                return sum(abs(a - b) > 150 for a, b in zip(values, values[1:])) > 130
        self.wait_for(detailed_frame, "Native 2x raster did not resolve half-CSS-pixel detail")
        self.switch_quality("smooth")
        self.assertEqual(self.session.driver.execute_script("return [innerWidth,innerHeight,devicePixelRatio]"), [1100, 640, 1])
        self.pointer("down", 100, 50)
        self.pointer("up", 100, 50)
        self.remote.dispatch({"type": "text", "text": "流畅模式输入"})
        self.assertTrue(self.remote.barrier())
        self.assertEqual(self.session.driver.execute_script("return document.querySelector('input').value"), "流畅模式输入")
        self.switch_quality("high")
        self.assertEqual(self.session.driver.execute_script("return [innerWidth,innerHeight,devicePixelRatio]"), [1100, 640, 2])

    def test_quality_change_preserves_queued_drag_edges_and_css_coordinates(self):
        self.pointer("down", 80, 180, buttons=1)
        self.remote.dispatch({"type": "resize", "width": 1100, "height": 640, "quality": "smooth"})
        self.pointer("move", 260, 300, buttons=1)
        self.pointer("up", 260, 300, buttons=0)
        self.assertTrue(self.remote.barrier())
        box = self.session.driver.execute_script("return {left:document.querySelector('#box').offsetLeft,top:document.querySelector('#box').offsetTop,released:window.dragReleased}")
        self.assertEqual(box, {"left": 210, "top": 250, "released": True})
        self.wait_for(lambda: self.frame_size() == (1100, 640), "Smooth frame was not delivered after drag")

    def test_navigation_during_native_capture_recovers_without_a_stuck_barrier(self):
        self.wait_for(lambda: self.remote._capture_id is not None, "No native capture to exercise navigation")
        previous = self.remote.snapshot()["sequence"]
        self.session.driver.get("data:text/html,<h1>New local document</h1><input id='entry'>")
        self.wait_for(lambda: self.remote.snapshot()["connected"] and self.remote.snapshot()["sequence"] > previous,
                      "Navigation abandoned a pending native capture", timeout=4)
        self.assertTrue(self.remote.barrier())
        self.assertEqual(self.frame_size(), (2200, 1280))

    def test_native_pointer_drag_scroll_and_release(self):
        self.pointer("down", 80, 180, buttons=1)
        self.pointer("move", 260, 300, buttons=1)
        self.pointer("up", 260, 300, buttons=0)
        self.assertTrue(self.remote.barrier())
        box = self.session.driver.execute_script("return {left:document.querySelector('#box').offsetLeft,top:document.querySelector('#box').offsetTop,released:window.dragReleased}")
        self.assertEqual(box, {"left": 210, "top": 250, "released": True})
        self.remote.dispatch({"type": "wheel", "x": .8, "y": .5, "delta_y": 420})
        self.assertTrue(self.remote.barrier())
        self.wait_for(lambda: self.session.driver.execute_script("return scrollY") > 200, "Native wheel did not scroll")

    def test_follow_new_tab_reconnect_and_stop_thread(self):
        self.switch_quality("smooth")
        first_handle = self.session.driver.current_window_handle
        self.session.driver.execute_script("window.open('about:blank','_blank')")
        self.session.info()
        new_handle = self.session.driver.current_window_handle
        self.assertNotEqual(first_handle, new_handle)
        self.assertEqual(self.remote.target_id, new_handle.removeprefix("CDwindow-"))
        self.wait_for(lambda: self.remote.snapshot()["connected"], "New tab did not stream")
        self.wait_for(lambda: (self.remote.snapshot()["width"], self.remote.snapshot()["height"]) == (1100, 640),
                      "New tab did not retain the embedded viewport size")
        self.assertEqual(self.remote.snapshot()["quality"], "smooth")
        self.wait_for(lambda: self.frame_size() == (1100, 640), "New tab did not retain smooth quality")
        stream_thread = self.remote._thread
        self.remote.close()
        self.assertFalse(stream_thread.is_alive())
        self.session.driver.close()
        self.session.info()
        self.wait_for(lambda: self.remote.snapshot()["connected"], "Returning tab did not reconnect")
        self.assertEqual(self.session.driver.current_window_handle, first_handle)


@unittest.skipUnless(os.environ.get("XHS_RUN_BROWSER_TESTS") == "1", "Set XHS_RUN_BROWSER_TESTS=1 for local browser fixture tests")
class ManagedRemoteBrowserFixtureTests(unittest.TestCase):
    def test_production_manager_initial_preview_loop_preserves_native_high_dpi(self):
        from PIL import Image
        from xhs_console.manager import JobManager

        class LocalSession(BrowserSession):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.dpr_samples = []
                self.fallback_calls = 0

            def navigate(self, url):
                # Exercise the production worker's immediate navigation without
                # ever opening the external URL passed by its open command.
                self.driver.get("data:text/html;charset=utf-8," + quote("<p>本地高清初始化测试</p>"))
                self.checkpoint(.05)

            def info(self):
                result = super().info()
                self.dpr_samples.append(self.driver.execute_script("return devicePixelRatio"))
                return result

            def screenshot(self):
                self.fallback_calls += 1
                raise AssertionError("Manager must not compete with native CDP capture")

        with tempfile.TemporaryDirectory(prefix="xhs-managed-hd-fixture-") as profile:
            manager = JobManager(Path(profile), browser_factory=LocalSession)
            try:
                manager.open_browser(headless=True)
                end = time.monotonic() + 15
                while time.monotonic() < end:
                    if manager.snapshot()["frame_id"] >= 3 and manager.remote_snapshot()["data"]:
                        break
                    time.sleep(.05)
                state = manager.remote_snapshot()
                self.assertIsNotNone(state["data"], "Production preview loop never received its first HD frame")
                self.assertGreaterEqual(manager.snapshot()["frame_id"], 3)
                self.assertEqual(state["quality"], "high")
                with Image.open(io.BytesIO(state["data"])) as frame:
                    self.assertEqual(frame.size, (state["width"] * 2, state["height"] * 2))
                self.assertEqual(manager._browser.fallback_calls, 0)
                self.assertEqual(manager._browser.dpr_samples[-2:], [2, 2])
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
