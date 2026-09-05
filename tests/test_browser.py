import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from xhs_console.browser import (BrowserSession, NeedsInteraction, XHS_HOME, browser_connection_arguments,
                                 browser_display_arguments, configure_browser_binary,
                                 deduplicate_note_urls, normalize_note, normalize_note_url,
                                 normalize_xhs_url, stop_service_safely, validate_navigation_url)


class BrowserNormalizationTests(unittest.TestCase):
    def test_missing_browser_uses_managed_stable_version(self):
        options = SimpleNamespace(binary_location=None, browser_version=None)
        configure_browser_binary(options, None)
        self.assertEqual(options.browser_version, "stable")
        configure_browser_binary(options, "C:/Browser/browser.exe")
        self.assertEqual(options.binary_location, "C:/Browser/browser.exe")

    def test_incomplete_service_cleanup_never_masks_startup_error(self):
        class IncompleteService:
            def stop(self):
                raise AttributeError("'Service' object has no attribute 'process'")

        stop_service_safely(IncompleteService())

    def test_direct_connection_explicitly_bypasses_browser_proxy(self):
        self.assertEqual(browser_connection_arguments(True), ("--no-proxy-server", "--proxy-bypass-list=*"))
        self.assertEqual(browser_connection_arguments(False), ())

    def test_embedded_display_uses_offscreen_window_not_headless_mode(self):
        arguments = browser_display_arguments(True)
        self.assertIn("--window-position=-32000,-32000", arguments)
        self.assertFalse(any(argument.startswith("--headless") for argument in arguments))
        self.assertEqual(browser_display_arguments(False), ())

    def test_signed_href_replaces_unsigned_but_keeps_discovery_order(self):
        first = "674000000000000001001001"
        second = "674000000000000001001002"
        result = deduplicate_note_urls([
            f"/explore/{first}", f"/explore/{second}?xsec_token=second",
            f"/search_result/{first}?xsec_token=a%2Bb%3D&xsec_source=pc_search",
        ])
        self.assertEqual(len(result), 2)
        self.assertIn("xsec_token=a%2Bb%3D&xsec_source=pc_search", result[0])
        self.assertIn(second, result[1])

    def test_navigation_preserves_arbitrary_web_hosts_scheme_ports_and_fragments(self):
        urls = (
            "https://example.org/query?q=a%2Fb&next=%23section#fragment",
            "http://127.0.0.1:8765/local/path?value=1#section",
            "http://localhost:9000/fixture",
            "http://[::1]:8765/local/path?value=1#section",
            "https://[2001:db8::1]/path#fragment",
            "http://xhslink.com/abc",
            "https://www.xiaohongshu.com:9000/explore",
            "https://example.org/path%20with%20spaces?q=%E4%B8%AD%E6%96%87",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(validate_navigation_url(url), url)
        self.assertEqual(validate_navigation_url("  https://example.org  "), "https://example.org/")

    def test_navigation_rejects_non_web_schemes_credentials_and_malformed_urls(self):
        urls = (
            "file:///etc/passwd", "javascript:alert(1)", "data:text/html,test", "chrome://settings", "edge://settings",
            "ftp://example.org/file", "about:blank", "example.org/path", "//example.org/path", "https:///missing",
            "https://user:pw@example.org/", "https://@example.org/", "https://user@example.org/",
            "https://example.org:not-a-port/", "https://example.org:65536/", "https://example.org:0/",
            "https://example.org:/", "https://[::1]extra:443/", "https://[::1/", "https://example.org:-1/",
            "https://exa mple.org/", "https://example.org/path with spaces", "https://example.org\\@other.org/",
            "\nhttps://example.org/", "https://exam\tple.org/", "https://example.org/path\r\nheader",
            "https://example.org/path\x00", "https://example.org/path\x7f", "https://<example.org>/",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_navigation_url(url)

    def test_collection_retains_xhs_allowlist_and_https_canonicalization(self):
        identity = "674000000000000001001001"
        signed = f"http://WWW.XIAOHONGSHU.COM:80/explore/{identity}?xsec_token=a%2Bb%3D&amp;source=test#comments"
        self.assertEqual(normalize_note_url(signed), f"https://www.xiaohongshu.com/explore/{identity}?xsec_token=a%2Bb%3D&source=test")
        self.assertEqual(normalize_xhs_url("http://xhslink.com/abc#part"), "https://xhslink.com/abc")
        for host in ("example.org", "xiaohongshu.com.evil.test", "127.0.0.1:9000", "[::1]:9000", "www.xiaohongshu.com:9000"):
            with self.subTest(host=host):
                self.assertIsNone(normalize_note_url(f"http://{host}/explore/{identity}?xsec_token=test"))
        self.assertEqual(deduplicate_note_urls([f"https://example.org/explore/{identity}", f"/explore/{identity}"]),
                         [f"https://www.xiaohongshu.com/explore/{identity}"])

    def test_profile_links_are_not_collected_as_notes(self):
        self.assertIsNone(normalize_note_url("/user/profile/674000000000000001001001"))
        self.assertIsNone(normalize_note_url("https://example.com/explore/674000000000000001001001"))

    def test_missing_date_is_not_fabricated_and_last_image_is_retained(self):
        result = normalize_note({"images": ["//example.com/1.jpg", "//example.com/2.jpg", "//example.com/1.jpg", "data:image/png;base64,bad"], "title": " demo "}, "https://www.xiaohongshu.com/explore/674000000000000001001001")
        self.assertEqual(result["images"], ["https://example.com/1.jpg", "https://example.com/2.jpg"])
        self.assertEqual(result["published_at"], "")
        self.assertEqual(result["author"], "")
        self.assertEqual(result["title"], "demo")

    def test_epoch_date_is_explicit_china_timezone(self):
        result = normalize_note({"published_at": 1704067200000}, "https://www.xiaohongshu.com/explore/674000000000000001001001")
        self.assertEqual(result["published_at"], "2024-01-01T08:00:00+08:00")


class BrowserPauseTests(unittest.TestCase):
    def test_collection_opens_home_and_waits_without_login_cookie(self):
        visited = []

        class Driver:
            def set_page_load_timeout(self, seconds):
                self.timeout = seconds

            def get_cookie(self, name):
                self.cookie_name = name
                return None

        session = BrowserSession(Path("."), lambda *_: None, lambda *_: None)
        session.driver = Driver()
        session.navigate = visited.append
        session._check_access = lambda: None
        with self.assertRaisesRegex(NeedsInteraction, "尚未检测到登录会话"):
            session.prepare_collection(SimpleNamespace(page_timeout=12))
        self.assertEqual(visited, [XHS_HOME])
        self.assertEqual(session.driver.cookie_name, "web_session")

    def test_collection_continues_only_with_login_cookie(self):
        class Driver:
            def set_page_load_timeout(self, seconds):
                pass

            def get_cookie(self, name):
                return {"name": name, "value": "local-session"}

        events = []
        session = BrowserSession(Path("."), lambda level, message: events.append((level, message)), lambda *_: None)
        session.driver = Driver()
        session.navigate = lambda url: events.append(("navigate", url))
        session._check_access = lambda: None
        session.prepare_collection(SimpleNamespace(page_timeout=12))
        self.assertIn(("navigate", XHS_HOME), events)
        self.assertTrue(any(level == "success" and "登录会话" in message for level, message in events))

    def test_long_manual_pause_does_not_exhaust_detail_timeout(self):
        clock = [0.0]
        calls = [0]

        def checkpoint(seconds=0):
            clock[0] += seconds + 60

        class Driver:
            current_url = "https://www.xiaohongshu.com/explore/674000000000000001001001"

            def set_page_load_timeout(self, seconds):
                pass

            def execute_script(self, script, *args):
                if "const expectedId" not in script:
                    return None
                calls[0] += 1
                return None if calls[0] == 1 else {"ready": True, "title": "已恢复", "content": "正文"}

        session = BrowserSession(Path("."), lambda *_: None, checkpoint)
        session.driver = Driver()
        session.navigate = lambda url: None
        with patch("xhs_console.browser.time.monotonic", side_effect=lambda: clock[0]):
            note = session.extract(Driver.current_url, SimpleNamespace(page_timeout=5))
        self.assertEqual(note["title"], "已恢复")
        self.assertEqual(calls[0], 2)

    def test_long_manual_pause_does_not_exhaust_search_budget(self):
        clock = [0.0]
        checkpoints = [0]
        searches = [0]

        def checkpoint(seconds=0):
            checkpoints[0] += 1
            clock[0] += seconds + (60 if checkpoints[0] == 2 else 0)

        class Driver:
            def set_page_load_timeout(self, seconds):
                pass

            def execute_script(self, script, *args):
                if ".map(a=>a.href)" not in script:
                    return None
                searches[0] += 1
                return [f"https://www.xiaohongshu.com/explore/67400000000000000100100{index}?xsec_token=test" for index in range(1, searches[0] + 1)]

        session = BrowserSession(Path("."), lambda *_: None, checkpoint)
        session.driver = Driver()
        session.navigate = lambda url: None
        config = SimpleNamespace(keyword="测试", page_timeout=5, search_seconds=5, max_notes=2, interval_seconds=1.5)
        with patch("xhs_console.browser.time.monotonic", side_effect=lambda: clock[0]):
            urls = session.search(config)
        self.assertEqual(len(urls), 2)


@unittest.skipUnless(os.environ.get("XHS_RUN_BROWSER_TESTS") == "1", "Set XHS_RUN_BROWSER_TESTS=1 for local browser fixture tests")
class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = tempfile.TemporaryDirectory(prefix="xhs-console-fixtures-")
        cls.session = BrowserSession(Path(cls.profile.name), lambda *_: None, time.sleep)
        cls.session.open(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.profile.cleanup()

    def load(self, html):
        self.session.driver.get("data:text/html;charset=utf-8," + quote(html))

    def extract_fixture(self):
        return self.session.driver.execute_script(self.session._extract_script, "674000000000000001001001")

    def test_manual_navigation_reaches_local_http_fixture_without_https_upgrade(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<!doctype html><title>Local navigation fixture</title><p id='section'>Ready</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/fixture?q=a%2Fb#section"
            self.session.navigate(url)
            self.assertEqual(self.session.driver.current_url, url)
            self.assertEqual(self.session.driver.title, "Local navigation fixture")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_state_extracts_full_image_order_and_original_fields(self):
        self.load("<html><body><div id='noteContainer'></div></body></html>")
        self.session.driver.execute_script("""
          window.__INITIAL_STATE__ = {note:{noteDetailMap:{'674000000000000001001001':{note:{
            noteId:'674000000000000001001001', title:'标题',desc:'正文',time:1704067200000,
            ipLocation:'上海',user:{nickname:'作者'},type:'normal',imageList:[
              {urlDefault:'https://example.com/first.jpg',urlPre:'https://example.com/thumb.jpg'},
              {infoList:[{imageScene:'WB_DFT',url:'https://example.com/last.jpg'}]}
            ]
          }}}}};
        """)
        result = self.extract_fixture()
        self.assertEqual(result["source"], "initial_state")
        self.assertEqual(result["title"], "标题")
        self.assertEqual(result["author"], "作者")
        self.assertEqual(result["images"], ["https://example.com/first.jpg", "https://example.com/last.jpg"])
        self.assertEqual(result["published_at"], 1704067200000)

    def test_dom_fallback_only_reads_note_and_retains_final_image(self):
        self.load("""<html><body><img src='https://example.com/recommended.jpg'>
          <div id='noteContainer'><div class='author-container'><span class='username'>作者</span></div>
          <div id='detail-title'>页面标题</div><div id='detail-desc'>正文提到安全验证，但不是验证页面</div>
          <div class='bottom-container'><span class='date'>编辑于 昨天 11:20 上海</span></div>
          <div class='note-slider'><img src='https://example.com/a.jpg'><img src='https://example.com/b.jpg'></div>
          </div></body></html>""")
        result = self.extract_fixture()
        self.assertEqual(result["source"], "dom")
        self.assertEqual(result["images"], ["https://example.com/a.jpg", "https://example.com/b.jpg"])
        self.assertEqual(result["published_at"], "昨天 11:20")
        self.assertEqual(result["location"], "上海")
        self.assertIsNone(self.session.access_status())

    def test_login_and_unrelated_state_are_not_saved_as_note(self):
        self.load("""<html><head><meta property='og:title' content='小红书登录'><meta property='og:image' content='https://example.com/logo.jpg'></head>
          <body><div class='login-container'>扫码登录</div><div class='note-item'>推荐内容</div></body></html>""")
        self.session.driver.execute_script("window.__INITIAL_STATE__={note:{noteDetailMap:{'674000000000000001001002':{note:{title:'错误的推荐笔记',noteId:'674000000000000001001002'}}}}};")
        self.assertIsNone(self.extract_fixture())
        self.assertIn("登录", self.session.access_status())

    def test_empty_mounted_container_and_global_meta_are_not_a_note(self):
        self.load("<html><head><meta property='og:image' content='https://example.com/logo.jpg'></head><body><div id='noteContainer'></div></body></html>")
        self.assertIsNone(self.extract_fixture())

    def test_network_restriction_is_not_presented_as_login_only(self):
        self.load("<html><body><main class='error-page'>安全限制 IP存在风险 错误码 300012</main><div class='login-container'>扫码登录</div></body></html>")
        reason = self.session.access_status()
        self.assertIn("当前网络访问受限", reason)
        self.assertIn("IP存在风险", reason)
        self.assertIn("300012", reason)
        self.assertIn("稍后再试", reason)
        self.assertNotIn("扫码", reason)

    def test_preview_jpeg_dimensions_and_keyboard_adapter(self):
        self.load("<html><body><input autofocus value='old'></body></html>")
        self.session.interact({"type": "key", "key": "Control+a"})
        self.session.interact({"type": "text", "text": "新内容"})
        self.assertEqual(self.session.driver.find_element("css selector", "input").get_attribute("value"), "新内容")
        self.assertTrue(self.session.screenshot().startswith(b"\xff\xd8"))
        info = self.session.info()
        self.assertGreater(info["width"], 1000)
        self.assertGreater(info["height"], 500)


if __name__ == "__main__":
    unittest.main()
