import io
import os
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from xhs_console.browser import (BrowserSession, NeedsInteraction, XHS_HOME, browser_candidates,
                                 browser_crash_summary,
                                 browser_connection_arguments, browser_display_arguments, browser_native_headless_arguments,
                                 browser_recovery_arguments,
                                 bundled_chrome_paths, bundled_chrome_version, bundled_driver_for_chrome,
                                 chrome_versions_share_build,
                                 configure_browser_binary, debugger_address_from_capabilities,
                                 deduplicate_note_urls, ensure_loopback_no_proxy, normalize_note, normalize_note_url,
                                 external_program_environment, installed_chrome_version, normalize_xhs_url, prepare_edge_driver,
                                 stop_service_safely, unsupported_windows_message,
                                 validate_navigation_url)


class BrowserNormalizationTests(unittest.TestCase):
    def test_debugger_address_prefers_the_selected_browser_namespace(self):
        capabilities = {
            "goog:chromeOptions": {"debuggerAddress": "127.0.0.1:9001"},
            "ms:edgeOptions": {"debuggerAddress": "127.0.0.1:9002"},
        }
        self.assertEqual(debugger_address_from_capabilities(capabilities, "chrome"), "127.0.0.1:9001")
        self.assertEqual(debugger_address_from_capabilities(capabilities, "edge"), "127.0.0.1:9002")
        with self.assertRaisesRegex(RuntimeError, "未提供实时画面"):
            debugger_address_from_capabilities({}, "edge")

    def test_release_bundle_is_first_and_contains_a_matched_driver(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root).resolve()
            browser = root_path / "browser" / "chrome-win64" / "chrome.exe"
            driver = root_path / "browser" / "chromedriver-win64" / "chromedriver.exe"
            browser.parent.mkdir(parents=True)
            driver.parent.mkdir(parents=True)
            browser.touch()
            driver.touch()
            self.assertEqual(bundled_chrome_paths(root_path), (str(browser), str(driver)))
            self.assertEqual(browser_candidates("auto", root_path)[0], ("chrome", str(browser)))
            self.assertIn(("chrome", str(browser)), browser_candidates("edge", root_path))

    def test_bundled_driver_can_follow_a_system_chrome_patch_update(self):
        self.assertTrue(chrome_versions_share_build("152.0.7977.82", "152.0.7977.83"))
        self.assertFalse(chrome_versions_share_build("152.0.7977.82", "152.0.7978.1"))
        self.assertFalse(chrome_versions_share_build("152.0.7977.82", "not-a-version"))
        self.assertFalse(chrome_versions_share_build(None, "152.0.7977.83"))

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root).resolve()
            bundled_browser = root_path / "bundle" / "chrome-win64" / "chrome.exe"
            bundled_driver = root_path / "bundle" / "chromedriver-win64" / "chromedriver.exe"
            system_browser = root_path / "system" / "Application" / "chrome.exe"
            bundled_browser.parent.mkdir(parents=True)
            bundled_driver.parent.mkdir(parents=True)
            system_browser.parent.mkdir(parents=True)
            bundled_browser.touch()
            bundled_driver.touch()
            system_browser.touch()
            (bundled_browser.parent / "152.0.7977.82.manifest").touch()
            self.assertEqual(bundled_chrome_version(bundled_browser), "152.0.7977.82")
            bundle = (str(bundled_browser), str(bundled_driver))
            with patch("xhs_console.browser.installed_chrome_version", return_value="152.0.7977.83"):
                self.assertEqual(bundled_driver_for_chrome(system_browser, bundle), str(bundled_driver))
            with patch("xhs_console.browser.installed_chrome_version", return_value="152.0.7978.1"):
                self.assertIsNone(bundled_driver_for_chrome(system_browser, bundle))
            with patch("xhs_console.browser.installed_chrome_version", return_value=None):
                self.assertIsNone(bundled_driver_for_chrome(system_browser, bundle))

    def test_compatible_system_chrome_precedes_the_online_edge_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root).resolve()
            resource_root = root_path / "resources"
            bundled_browser = resource_root / "browser" / "chrome-win64" / "chrome.exe"
            bundled_driver = resource_root / "browser" / "chromedriver-win64" / "chromedriver.exe"
            system_browser = root_path / "Google" / "Chrome" / "Application" / "chrome.exe"
            edge = root_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            for executable in (bundled_browser, bundled_driver, system_browser, edge):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()
            (bundled_browser.parent / "152.0.7977.82.manifest").touch()
            (system_browser.parent / "152.0.7977.83").mkdir()
            # Windows runners may expose the temporary directory through an 8.3
            # alias (for example RUNNER~1) while Path.resolve() expands it. Keep
            # discovery and the expected paths in the same canonical namespace.
            environment = {"PROGRAMFILES": str(root_path), "PROGRAMFILES(X86)": "", "LOCALAPPDATA": ""}
            with (patch.dict(os.environ, environment),
                  patch("xhs_console.browser.shutil.which", return_value=None),
                  patch("xhs_console.browser._windows_file_version", return_value=None)):
                candidates = browser_candidates("auto", resource_root)
        self.assertEqual(candidates[:3], [
            ("chrome", str(bundled_browser)),
            ("chrome", str(system_browser)),
            ("edge", str(edge)),
        ])

    def test_selected_chrome_file_version_wins_over_stale_install_folders(self):
        with tempfile.TemporaryDirectory() as root:
            browser = Path(root) / "Application" / "chrome.exe"
            browser.parent.mkdir(parents=True)
            browser.touch()
            (browser.parent / "999.0.9999.99").mkdir()
            with patch("xhs_console.browser._windows_file_version", return_value="152.0.7977.83"):
                self.assertEqual(installed_chrome_version(browser), "152.0.7977.83")

    def test_frozen_windows_temporarily_restores_the_system_dll_search_path(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            with (patch("xhs_console.browser.sys.platform", "win32"),
                  patch.object(__import__("sys"), "frozen", True, create=True),
                  patch.object(__import__("sys"), "_MEIPASS", root, create=True),
                  patch("xhs_console.browser._set_windows_dll_directory", side_effect=calls.append)):
                with external_program_environment():
                    calls.append("browser-started")
        self.assertEqual(calls, [None, "browser-started", str(Path(root).resolve())])

    def test_installed_edge_failure_retains_managed_chrome_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            edge = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            edge.parent.mkdir(parents=True)
            edge.touch()
            environment = {"PROGRAMFILES": root, "PROGRAMFILES(X86)": "", "LOCALAPPDATA": ""}
            with patch.dict(os.environ, environment), patch("xhs_console.browser.shutil.which", return_value=None):
                candidates = browser_candidates("auto", Path(root) / "without-bundle")
            self.assertEqual(candidates, [("edge", str(edge)), ("chrome", None)])

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

    def test_local_selenium_control_ports_never_use_global_proxy(self):
        with patch.dict(os.environ, {"NO_PROXY": "internal.example"}, clear=False):
            ensure_loopback_no_proxy()
            for key in ("NO_PROXY", "no_proxy"):
                values = os.environ[key].split(",")
                self.assertIn("127.0.0.1", values)
                self.assertIn("localhost", values)
                self.assertIn("::1", values)
            self.assertIn("internal.example", os.environ["NO_PROXY"])

    def test_embedded_display_uses_offscreen_window_not_headless_mode(self):
        arguments = browser_display_arguments(True)
        self.assertIn("--window-position=-10000,-10000", arguments)
        self.assertFalse(any(argument.startswith("--headless") for argument in arguments))
        self.assertEqual(browser_display_arguments(False), ())

    def test_recovery_mode_uses_software_rendering_without_disabling_sandbox(self):
        arguments = browser_recovery_arguments(True)
        self.assertIn("--disable-gpu", arguments)
        self.assertIn("--disable-crash-reporter", arguments)
        self.assertNotIn("--no-sandbox", arguments)
        self.assertEqual(browser_recovery_arguments(False), ())

    def test_server_fallback_uses_real_headless_without_disabling_sandbox(self):
        arguments = browser_native_headless_arguments(True)
        self.assertEqual(arguments, ("--headless=new",))
        self.assertNotIn("--no-sandbox", arguments)
        self.assertEqual(browser_native_headless_arguments(False), ())

    def test_browser_crash_log_is_reduced_to_actionable_lines(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "chrome-browser.log"
            log.write_text("ordinary line\n[ERROR:gpu] failed to initialize\n[FATAL:policy] access denied\n", encoding="utf-8")
            summary = browser_crash_summary(log)
        self.assertIn("failed to initialize", summary)
        self.assertIn("access denied", summary)
        self.assertNotIn("ordinary line", summary)

    def test_exact_microsoft_edge_driver_is_downloaded_and_cached(self):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("msedgedriver.exe", b"MZ" + b"x" * 1_000_000)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def geturl(self):
                return "https://msedgedriver.microsoft.com/152.0.4191.62/edgedriver_win64.zip"

            def read(self, _size):
                data, archive_bytes.position = archive_bytes.getvalue(), getattr(archive_bytes, "position", 0)
                if archive_bytes.position:
                    return b""
                archive_bytes.position = len(data)
                return data

        with tempfile.TemporaryDirectory() as root:
            with (patch("xhs_console.browser.installed_edge_version", return_value="152.0.4191.62"),
                  patch("xhs_console.browser.urlopen", return_value=Response()) as download):
                first = prepare_edge_driver(Path(root) / "msedge.exe", Path(root) / "cache")
                second = prepare_edge_driver(Path(root) / "msedge.exe", Path(root) / "cache")
            self.assertEqual(first, second)
            self.assertGreater(Path(first).stat().st_size, 1_000_000)
            self.assertEqual(download.call_count, 1)

    def test_old_windows_gets_an_explicit_browser_support_error(self):
        with patch("xhs_console.browser.os.name", "nt"), patch("xhs_console.browser.sys.getwindowsversion", return_value=SimpleNamespace(major=6, minor=3)):
            self.assertIn("Windows Server 2016", unsupported_windows_message())

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
    def test_bundled_chrome_crash_retries_with_isolated_software_profile(self):
        service_calls = []

        class FakeService:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                service_calls.append(kwargs)

            def stop(self):
                pass

        class FakeDriver:
            capabilities = {"goog:chromeOptions": {"debuggerAddress": "127.0.0.1:9001"}}
            current_window_handle = "CDwindow-chrome"
            window_handles = [current_window_handle]

            def set_page_load_timeout(self, _seconds):
                pass

            def set_script_timeout(self, _seconds):
                pass

            def get(self, _url):
                pass

        class FakeTransport:
            def __init__(self, *_args):
                pass

            def start(self):
                pass

            def wait_until_ready(self, timeout):
                return True

        driver = FakeDriver()
        events = []
        with tempfile.TemporaryDirectory() as root:
            chrome = Path(root) / "chrome.exe"
            executable = Path(root) / "chromedriver.exe"
            chrome.touch()
            executable.touch()
            session = BrowserSession(Path(root), lambda level, message: events.append((level, message)), lambda *_: None)
            with (patch("xhs_console.browser.browser_candidates", return_value=[("chrome", str(chrome))]),
                  patch("xhs_console.browser.bundled_chrome_paths", return_value=(str(chrome), str(executable))),
                  patch("selenium.webdriver.Chrome", side_effect=[RuntimeError("Chrome failed to start: crashed"),
                                                                   RuntimeError("Chrome failed to start: crashed"), driver]) as constructor,
                  patch("selenium.webdriver.chrome.service.Service", FakeService),
                  patch("xhs_console.remote_browser.RemoteBrowserTransport", FakeTransport)):
                session.open(headless=True, browser="auto")
        self.assertEqual(constructor.call_count, 3)
        self.assertEqual([call["executable_path"] for call in service_calls], [str(executable)] * 3)
        recovery_options = constructor.call_args_list[1].kwargs["options"]
        self.assertIn("--disable-gpu", recovery_options.arguments)
        self.assertTrue(any("chrome-compat" in argument for argument in recovery_options.arguments))
        server_options = constructor.call_args_list[2].kwargs["options"]
        self.assertIn("--headless=new", server_options.arguments)
        self.assertFalse(any(argument.startswith("--window-position") for argument in server_options.arguments))
        self.assertTrue(any("软件渲染" in message for _, message in events))
        self.assertTrue(any("无桌面模式" in message for _, message in events))

    def test_compatible_system_chrome_uses_the_bundled_driver_offline(self):
        service_calls = []

        class FakeService:
            def __init__(self, **kwargs):
                service_calls.append(kwargs)

            def stop(self):
                pass

        class FakeDriver:
            capabilities = {"goog:chromeOptions": {"debuggerAddress": "127.0.0.1:9001"}}
            current_window_handle = "CDwindow-chrome"
            window_handles = [current_window_handle]

            def set_page_load_timeout(self, _seconds):
                pass

            def set_script_timeout(self, _seconds):
                pass

            def get(self, _url):
                pass

        class FakeTransport:
            def __init__(self, *_args):
                pass

            def start(self):
                pass

            def wait_until_ready(self, timeout):
                return True

        events = []
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root).resolve()
            bundled_browser = root_path / "bundle" / "chrome.exe"
            bundled_driver = root_path / "bundle" / "chromedriver.exe"
            system_browser = root_path / "system" / "chrome.exe"
            for executable in (bundled_browser, bundled_driver, system_browser):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()
            session = BrowserSession(root_path, lambda level, message: events.append((level, message)), lambda *_: None)
            with (patch("xhs_console.browser.browser_candidates", return_value=[("chrome", str(system_browser))]),
                  patch("xhs_console.browser.bundled_chrome_paths",
                        return_value=(str(bundled_browser), str(bundled_driver))),
                  patch("xhs_console.browser.installed_chrome_version", return_value="152.0.7977.83"),
                  patch("xhs_console.browser.bundled_chrome_version", return_value="152.0.7977.82"),
                  patch("selenium.webdriver.Chrome", return_value=FakeDriver()) as constructor,
                  patch("selenium.webdriver.chrome.service.Service", FakeService),
                  patch("xhs_console.remote_browser.RemoteBrowserTransport", FakeTransport)):
                session.open(headless=True, browser="auto")
        self.assertEqual(constructor.call_count, 1)
        self.assertEqual(service_calls[0]["executable_path"], str(bundled_driver))
        self.assertEqual(constructor.call_args.kwargs["options"].binary_location, str(system_browser))
        self.assertTrue(any("内置匹配驱动启动本机 Chrome" in message for _, message in events))

    def test_edge_without_live_frame_is_closed_and_falls_back_to_bundled_chrome(self):
        class FakeService:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def stop(self):
                pass

        class FakeDriver:
            def __init__(self, name, port):
                self.name = name
                self.capabilities = {
                    ("ms:edgeOptions" if name == "edge" else "goog:chromeOptions"):
                    {"debuggerAddress": f"127.0.0.1:{port}"}
                }
                self.current_window_handle = f"CDwindow-{name}"
                self.window_handles = [self.current_window_handle]
                self.quit_called = False

            def set_page_load_timeout(self, _seconds):
                pass

            def set_script_timeout(self, _seconds):
                pass

            def get(self, _url):
                pass

            def quit(self):
                self.quit_called = True

        class FakeTransport:
            def __init__(self, address, *_args):
                self.ready = address.endswith(":9001")
                self.closed = False

            def start(self):
                pass

            def wait_until_ready(self, timeout):
                self.timeout = timeout
                return self.ready

            def snapshot(self):
                return {"error": None}

            def close(self):
                self.closed = True

        edge_driver = FakeDriver("edge", 9002)
        chrome_driver = FakeDriver("chrome", 9001)
        events = []
        with tempfile.TemporaryDirectory() as root:
            chrome = Path(root) / "chrome.exe"
            driver = Path(root) / "chromedriver.exe"
            chrome.touch()
            driver.touch()
            session = BrowserSession(Path(root), lambda level, message: events.append((level, message)), lambda *_: None)
            with (patch("xhs_console.browser.browser_candidates", return_value=[("edge", str(chrome)), ("chrome", str(chrome))]),
                  patch("xhs_console.browser.bundled_chrome_paths", return_value=(str(chrome), str(driver))),
                  patch("selenium.webdriver.Edge", return_value=edge_driver),
                  patch("selenium.webdriver.Chrome", return_value=chrome_driver),
                  patch("xhs_console.browser.prepare_edge_driver", return_value=str(driver)),
                  patch("selenium.webdriver.edge.service.Service", FakeService),
                  patch("selenium.webdriver.chrome.service.Service", FakeService),
                  patch("xhs_console.remote_browser.RemoteBrowserTransport", FakeTransport)):
                session.open(headless=True, browser="edge")
        self.assertTrue(edge_driver.quit_called)
        self.assertIs(session.driver, chrome_driver)
        self.assertEqual(session.browser, "chrome")
        self.assertTrue(any(level == "warning" and "备用启动方式" in message for level, message in events))

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
