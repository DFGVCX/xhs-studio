r"""Opt-in LOCAL end-to-end embedded viewer regression.

PowerShell:
  $env:XHS_VIEWER_E2E='1'
  .\.venv\Scripts\python.exe -m unittest discover -s tests -p test_browser_viewer_e2e.py -v

Uses an OS-assigned loopback port and two disposable headless profiles. The UI
browser operates the real same-origin /browser iframe; the production WebSocket
route relays those actions into a second real Chromium browser through CDP.
Only the local fixture page is visited. No account, XHS traffic, project profile,
or production port is used. Native OS IME candidate selection remains a manual
check, while actual Chinese insertion is exercised without synthetic DOM events.
"""

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait

from xhs_console.browser import BrowserSession
from xhs_console.server import create_app


PROJECT = Path(__file__).resolve().parents[1]
FIXTURE_PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<style>*{box-sizing:border-box}html,body{margin:0;background:#fff;color:#222;font:18px sans-serif}
main{padding:28px}input{width:420px;height:46px;padding:10px;font:18px sans-serif}
#drag-area{position:relative;width:650px;height:170px;background:#eee;margin-top:26px}
#handle{position:absolute;left:20px;top:55px;width:70px;height:60px;display:grid;place-items:center;
background:#ff7200;color:white;touch-action:none;user-select:none;border-radius:10px}
.filler{height:1200px;background:linear-gradient(#fff,#e5e5e5)}</style>
<main><h1>本地交互验收</h1><input id="target-input" aria-label="中文输入验证" placeholder="在内嵌画面中点击后输入">
<div id="drag-area"><div id="handle">拖动</div></div><output id="drag-result">0</output>
<div class="filler"></div></main><script>
const handle=document.querySelector('#handle');let dragging=false,start=0,left=20;
document.addEventListener('pointerdown',event=>{window.lastPointer={x:event.clientX,y:event.clientY,target:event.target.id||event.target.tagName}});
handle.addEventListener('pointerdown',event=>{dragging=true;start=event.clientX;left=parseFloat(handle.style.left)||20;handle.setPointerCapture(event.pointerId)});
handle.addEventListener('pointermove',event=>{if(dragging){handle.style.left=Math.max(0,Math.min(580,left+event.clientX-start))+'px';document.querySelector('#drag-result').value=handle.style.left}});
handle.addEventListener('pointerup',()=>{dragging=false;document.body.dataset.dragComplete='true'});
</script></html>"""


class LocalFixtureManager:
    """Use the production streaming route with a real, independently owned browser."""

    def __init__(self, target):
        self.target = target
        self.url = ""
        self.actions = []

    def stream_state(self):
        frame = self.remote_snapshot()
        return {"type": "state", "status": "ready", "message": "本地交互测试", "browser_open": True,
                "manual_enabled": frame["connected"], "stream_available": frame["connected"],
                "browser": {"title": "本地交互验收", "url": self.url, "width": frame["width"], "height": frame["height"]}}

    def snapshot(self):
        return self.stream_state()

    def remote_snapshot(self):
        return {**self.target.remote.snapshot(), "transport_id": id(self.target.remote)}

    def remote_action(self, action):
        self.actions.append(action)
        self.target.remote.dispatch(action)

    def release_remote_input(self):
        self.target.remote.dispatch({"type": "release"})

    def shutdown(self):
        pass  # BrowserSession is closed by its owning test thread.


@unittest.skipUnless(os.environ.get("XHS_VIEWER_E2E") == "1", "Set XHS_VIEWER_E2E=1 for local two-browser E2E")
class BrowserViewerEndToEnd(unittest.TestCase):
    def test_same_origin_live_canvas_input_drag_and_resize(self):
        with tempfile.TemporaryDirectory(prefix="xhs-viewer-e2e-") as directory:
            temporary = Path(directory)
            target = BrowserSession(temporary / "target", lambda *_: None, lambda *_: None)
            viewer = BrowserSession(temporary / "viewer", lambda *_: None, lambda *_: None)
            server = None
            server_thread = None
            listener = None
            try:
                target.open(headless=True)
                self.assertIsNotNone(target.remote, "Target Chromium must support live CDP transport")
                manager = LocalFixtureManager(target)
                app = create_app(PROJECT, manager_factory=lambda _: manager)

                @app.get("/__fixture__")
                def fixture_page():
                    return HTMLResponse(FIXTURE_PAGE)

                @app.get("/__viewer_test__")
                def viewer_page():
                    return HTMLResponse('<!doctype html><html><meta charset="utf-8"><style>html,body{margin:0;height:100%;overflow:hidden;background:#111114}iframe{width:100%;height:100%;border:0;display:block}</style><iframe src="/browser" title="内嵌浏览器"></iframe></html>')

                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                self.assertNotEqual(port, 8765)
                server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
                server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
                server_thread.start()
                deadline = time.monotonic() + 15
                while not server.started and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(server.started, "Temporary loopback server did not start")
                manager.url = f"http://127.0.0.1:{port}/__fixture__"
                target.driver.get(manager.url)
                viewer.open(headless=True)
                # This browser is the ordinary frontend client, not another
                # controlled target. Avoid a second unconsumed capture stream.
                if viewer.remote:
                    viewer.remote.close()
                viewer.driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
                viewer.driver.set_window_size(1260, 850)
                viewer.driver.get(f"http://127.0.0.1:{port}/__viewer_test__")
                viewer.driver.switch_to.frame(viewer.driver.find_element(By.TAG_NAME, "iframe"))
                try:
                    WebDriverWait(viewer.driver, 15).until(lambda driver: driver.execute_script("return document.querySelector('#browser-canvas').classList.contains('interactive') && !document.querySelector('#browser-canvas').hidden"))
                except TimeoutException:
                    stream = {key: value for key, value in manager.remote_snapshot().items() if key != "data"}
                    page = viewer.driver.execute_script("return {text:document.body.innerText,canvasHidden:document.querySelector('#browser-canvas').hidden,canvasClass:document.querySelector('#browser-canvas').className}")
                    self.fail(f"Viewer startup diagnostic: stream={stream}; page={page}; console={viewer.driver.get_log('browser')}")

                def settled_size(scale=2):
                    displayed = viewer.driver.execute_script("const c=document.querySelector('#browser-canvas'),v=document.querySelector('#viewport'),s=getComputedStyle(c),ctx=c.getContext('2d');return {cw:c.clientWidth,ch:c.clientHeight,bw:c.width,bh:c.height,w:v.clientWidth,h:v.clientHeight,scroll:document.documentElement.scrollHeight,inner:innerHeight,filter:s.filter,rendering:s.imageRendering,contextFilter:ctx.filter,smoothing:ctx.imageSmoothingQuality}")
                    actual = target.driver.execute_script("return {w:innerWidth,h:innerHeight,dpr:devicePixelRatio}")
                    fitted = abs(displayed["cw"] - displayed["w"]) <= 2 and abs(displayed["ch"] - displayed["h"]) <= 2
                    css_size = actual["w"] == displayed["w"] and actual["h"] == displayed["h"]
                    backing = abs(displayed["bw"] - actual["w"] * scale) <= 2 and abs(displayed["bh"] - actual["h"] * scale) <= 2
                    return displayed if fitted and css_size and backing and actual["dpr"] == scale else False

                dimensions = WebDriverWait(viewer.driver, 12).until(lambda _: settled_size())
                self.assertLessEqual(dimensions["scroll"], dimensions["inner"], "Iframe content must not leave a scrollable blank region")
                self.assertEqual(Select(viewer.driver.find_element(By.ID, "stream-quality")).first_selected_option.get_attribute("value"), "high")
                self.assertEqual(dimensions["filter"], "none", "Do not replace actual resolution with a CSS sharpening filter")
                self.assertEqual(dimensions["contextFilter"], "none")
                self.assertEqual(dimensions["rendering"], "auto", "Text downsampling must not use nearest-neighbor pixelation")
                self.assertEqual(dimensions["smoothing"], "high")

                def canvas_offset(selector):
                    remote = target.driver.execute_script("const r=document.querySelector(arguments[0]).getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,w:innerWidth,h:innerHeight}", selector)
                    size = viewer.driver.execute_script("const c=document.querySelector('#browser-canvas');return {w:c.clientWidth,h:c.clientHeight}")
                    return int(remote["x"] / remote["w"] * size["w"] - size["w"] / 2), int(remote["y"] / remote["h"] * size["h"] - size["h"] / 2)

                canvas = viewer.driver.find_element(By.ID, "browser-canvas")
                x, y = canvas_offset("#target-input")
                viewer.driver.execute_script("document.addEventListener('pointerdown',e=>window.lastLocalPointer={x:e.clientX,y:e.clientY,target:e.target.id})")
                ActionChains(viewer.driver).move_to_element_with_offset(canvas, x, y).click().perform()
                try:
                    WebDriverWait(target.driver, 8).until(lambda driver: driver.execute_script("return document.activeElement.id") == "target-input")
                except TimeoutException:
                    target_diagnostic = target.driver.execute_script("return {pointer:window.lastPointer,active:document.activeElement.id,rect:document.querySelector('#target-input').getBoundingClientRect().toJSON(),width:innerWidth,height:innerHeight,dpr:devicePixelRatio}")
                    viewer_diagnostic = viewer.driver.execute_script("return {pointer:window.lastLocalPointer,canvas:document.querySelector('#browser-canvas').getBoundingClientRect().toJSON(),active:document.activeElement.id,toast:document.querySelector('#viewer-toast').textContent}")
                    self.fail(f"Click diagnostic: target={target_diagnostic}; viewer={viewer_diagnostic}; actions={manager.actions[-8:]}; requested-offset=({x},{y})")
                # Native CDP insertion into the focused viewer textarea fires trusted
                # beforeinput/input events. It is not a DOM dispatchEvent shortcut.
                viewer.driver.execute_cdp_cmd("Input.insertText", {"text": "中文输入验证"})
                WebDriverWait(target.driver, 8).until(lambda driver: driver.find_element(By.ID, "target-input").get_attribute("value") == "中文输入验证")

                x, y = canvas_offset("#handle")
                ActionChains(viewer.driver).move_to_element_with_offset(canvas, x, y).click_and_hold().move_by_offset(180, 0).pause(0.15).release().perform()
                WebDriverWait(target.driver, 8).until(lambda driver: driver.execute_script("return document.body.dataset.dragComplete") == "true")
                dragged = target.driver.execute_script("return parseFloat(document.querySelector('#handle').style.left)")
                self.assertGreater(dragged, 150, "Real pointer drag did not reach the controlled page")

                viewer.driver.switch_to.default_content()
                viewer.driver.set_window_size(1060, 700)
                viewer.driver.switch_to.frame(viewer.driver.find_element(By.TAG_NAME, "iframe"))
                resized = WebDriverWait(viewer.driver, 12).until(lambda _: settled_size())
                self.assertLess(resized["w"], dimensions["w"])
                self.assertLess(resized["h"], dimensions["h"])
                self.assertLessEqual(resized["scroll"], resized["inner"])

                Select(viewer.driver.find_element(By.ID, "stream-quality")).select_by_value("smooth")
                smooth = WebDriverWait(viewer.driver, 12).until(lambda _: settled_size(scale=1))
                self.assertEqual((smooth["w"], smooth["h"]), (resized["w"], resized["h"]), "Quality changes must not resize the CSS page")
                self.assertEqual(target.driver.find_element(By.ID, "target-input").get_attribute("value"), "中文输入验证")
                x, y = canvas_offset("#target-input")
                ActionChains(viewer.driver).move_to_element_with_offset(canvas, x, y).click().key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).perform()
                viewer.driver.execute_cdp_cmd("Input.insertText", {"text": "流畅模式仍可输入"})
                WebDriverWait(target.driver, 8).until(lambda driver: driver.find_element(By.ID, "target-input").get_attribute("value") == "流畅模式仍可输入")
                viewer.driver.switch_to.default_content()
                viewer.driver.refresh()
                viewer.driver.switch_to.frame(viewer.driver.find_element(By.TAG_NAME, "iframe"))
                WebDriverWait(viewer.driver, 12).until(lambda _: settled_size(scale=1))
                self.assertEqual(Select(viewer.driver.find_element(By.ID, "stream-quality")).first_selected_option.get_attribute("value"), "smooth", "Quality preference must persist across reloads")

                for mobile_width in (390, 320):
                    viewer.driver.switch_to.default_content()
                    viewer.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": mobile_width, "height": 700, "deviceScaleFactor": 1, "mobile": False})
                    viewer.driver.switch_to.frame(viewer.driver.find_element(By.TAG_NAME, "iframe"))
                    WebDriverWait(viewer.driver, 12).until(lambda _: settled_size(scale=1))
                    controls = viewer.driver.execute_script("document.querySelector('#page-title').textContent='这是一个很长的外站标题 Example page title';document.querySelector('#mode-badge').textContent='自动化运行中';return [...document.querySelectorAll('.browser-tab,.quality-control,#stream-quality,#mode-badge,#navigation button,#address,.connection')].filter(el=>el.getClientRects().length&&getComputedStyle(el).display!=='none').map(el=>{const r=el.getBoundingClientRect();return {name:el.id||el.className,left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width}})")
                    for control in controls:
                        self.assertGreater(control["width"], 0, control["name"])
                        self.assertGreaterEqual(control["left"], -1, f"{mobile_width}px: {control}")
                        self.assertLessEqual(control["right"], mobile_width + 1, f"{mobile_width}px: {control}")
                    top_controls = [control for control in controls if control["name"] in {"browser-tab", "quality-control", "mode-badge"}]
                    top_controls.sort(key=lambda control: control["left"])
                    for previous, current in zip(top_controls, top_controls[1:]):
                        self.assertLessEqual(previous["right"], current["left"] + 1, f"Controls overlap at {mobile_width}px: {previous}, {current}")
            finally:
                viewer.close()
                if server:
                    server.should_exit = True
                if server_thread:
                    server_thread.join(timeout=10)
                if listener:
                    listener.close()
                target.close()


if __name__ == "__main__":
    unittest.main()
