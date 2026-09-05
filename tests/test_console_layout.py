"""Opt-in local comic theme, font loading, and content-density checks.

Set XHS_THEME_TESTS=1. Uses an isolated browser and random loopback port;
never opens Xiaohongshu or starts a collection task.
"""

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from xhs_console.browser import BrowserSession
from xhs_console.manager import JobManager
from xhs_console.server import create_app


PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("XHS_THEME_TESTS") == "1", "Set XHS_THEME_TESTS=1 for local comic layout checks")
class ConsoleComicLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="xhs-comic-layout-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        app = create_app(PROJECT, manager_factory=lambda _: JobManager(root / "manager"))
        cls.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.listener.bind(("127.0.0.1", 0))
        cls.addClassCleanup(cls.listener.close)
        cls.url = f"http://127.0.0.1:{cls.listener.getsockname()[1]}/"
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        cls.server_thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.listener]}, daemon=True)
        cls.server_thread.start()
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 15
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not cls.server.started:
            raise RuntimeError("Local test server did not start")
        cls.browser = BrowserSession(root / "viewer", lambda *_: None, lambda *_: None)
        cls.addClassCleanup(cls.browser.close)
        cls.browser.open(headless=True)
        if cls.browser.remote:
            cls.browser.remote.close()  # This is the test's viewer, not a controlled page.
        cls.driver = cls.browser.driver
        cls.driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
        cls.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
        cls.driver.set_script_timeout(20)

    @classmethod
    def stop_server(cls):
        cls.server.should_exit = True
        cls.server_thread.join(timeout=10)

    def setUp(self):
        self.driver.switch_to.default_content()
        self.set_viewport(1440)
        self.driver.get(self.url)
        WebDriverWait(self.driver, 12).until(lambda d: d.find_element(By.ID, "keyword").is_enabled())
        self.driver.execute_async_script("const done=arguments[0];document.fonts.ready.then(()=>done(true))")

    def set_viewport(self, width):
        self.driver.set_window_size(max(500, width), 1000)
        # Headless Chromium has a minimum outer window width. Explicit CSS
        # metrics test the actual 390px/650px breakpoints rather than that minimum.
        self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False})

    def test_local_comic_fonts_and_light_theme_load_in_both_surfaces(self):
        outer = self.driver.execute_script("""return {
          body:getComputedStyle(document.body).fontFamily,
          heading:getComputedStyle(document.querySelector('h1')).fontFamily,
          scheme:getComputedStyle(document.documentElement).colorScheme,
          fonts:[...document.fonts].map(f=>({family:f.family,status:f.status}))}
        """)
        self.assertEqual(outer["scheme"], "light")
        self.assertIn("XHS Hand", outer["body"])
        self.assertIn("XHS Comic", outer["heading"])
        for family in ["XHS Comic", "XHS Hand"]:
            self.assertTrue(any(f["family"].strip('"') == family and f["status"] == "loaded" for f in outer["fonts"]), outer)
        self.driver.switch_to.frame(self.driver.find_element(By.ID, "browser-embed"))
        self.driver.execute_async_script("const done=arguments[0];document.fonts.ready.then(()=>done(true))")
        inner = self.driver.execute_script("return {scheme:getComputedStyle(document.documentElement).colorScheme,font:getComputedStyle(document.querySelector('h1')).fontFamily,overflow:document.documentElement.scrollHeight-innerHeight}")
        self.assertEqual(inner["scheme"], "light")
        self.assertIn("XHS Comic", inner["font"])
        self.assertLessEqual(inner["overflow"], 1)

    def test_all_tabs_and_modes_have_no_horizontal_overflow_or_fixed_blank_panels(self):
        for width in [1440, 1024, 650, 390]:
            self.set_viewport(width)
            for name in ["settings", "status", "results", "logs"]:
                with self.subTest(width=width, tab=name):
                    self.driver.find_element(By.ID, f"{name}-tab").click()
                    size = self.driver.execute_script("const p=document.querySelector('[role=tabpanel]:not([hidden])');return {width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,min:getComputedStyle(p).minHeight,height:p.getBoundingClientRect().height}")
                    self.assertLessEqual(size["scroll"], size["width"] + 1, size)
                    self.assertIn(size["min"], ["0px", "auto"], size)
                    self.assertGreater(size["height"], 40)
            self.driver.find_element(By.ID, "settings-tab").click()
            self.driver.find_element(By.CSS_SELECTOR, '[data-mode="urls"]').click()
            self.assertFalse(self.driver.find_element(By.ID, "search-time-group").is_displayed())
            bounds = self.driver.execute_script("const g=document.querySelector('.config-grid').getBoundingClientRect();const f=document.querySelector('.config-grid>.field:last-child').getBoundingClientRect();return {edge:g.right,last:f.right}")
            self.assertLessEqual(abs(bounds["edge"] - bounds["last"]), 2, bounds)
            self.driver.find_element(By.CSS_SELECTOR, '[data-mode="search"]').click()

    def test_taller_viewer_resizes_without_changing_width_and_remembers_height(self):
        def size():
            return self.driver.execute_script("const r=document.querySelector('#browser-embed-wrap').getBoundingClientRect();return {width:r.width,height:r.height}")

        before = size()
        self.assertGreaterEqual(before["height"], 660)
        handle = self.driver.find_element(By.ID, "browser-height-handle")
        handle.send_keys(Keys.ARROW_DOWN)
        after = size()
        self.assertAlmostEqual(after["width"], before["width"], delta=1)
        self.assertAlmostEqual(after["height"], before["height"] + 40, delta=1)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", handle)
        ActionChains(self.driver).move_to_element(handle).click_and_hold().move_by_offset(0, 65).release().perform()
        dragged = size()
        self.assertAlmostEqual(dragged["height"], after["height"] + 65, delta=2)
        self.driver.refresh()
        WebDriverWait(self.driver, 10).until(lambda _: size()["height"] == dragged["height"])
        self.assertAlmostEqual(size()["width"], before["width"], delta=1)
        self.driver.find_element(By.ID, "reset-browser-height").click()
        self.assertAlmostEqual(size()["height"], before["height"], delta=1)


if __name__ == "__main__":
    unittest.main()
