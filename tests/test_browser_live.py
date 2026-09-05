"""Opt-in read-only site smoke: XHS_LIVE_BROWSER=1 python -m unittest discover.

Uses a fresh disposable browser profile. It never signs in, bypasses a gate, or
downloads media, and prints no signed note URLs or access tokens.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from xhs_console.browser import BrowserSession, NeedsInteraction, deduplicate_note_urls


@unittest.skipUnless(os.environ.get("XHS_LIVE_BROWSER") == "1", "Live XHS smoke requires XHS_LIVE_BROWSER=1")
class LiveBrowserSmoke(unittest.TestCase):
    def test_public_detail_or_gate_and_search_or_gate(self):
        with tempfile.TemporaryDirectory(prefix="xhs-live-test-") as directory:
            session = BrowserSession(Path(directory), lambda *_: None, time.sleep)
            try:
                session.open(headless=True)
                session.navigate("https://www.xiaohongshu.com/explore")
                deadline = time.monotonic() + 12
                links = []
                gate = None
                while time.monotonic() < deadline:
                    gate = session.access_status()
                    links = deduplicate_note_urls(session.driver.execute_script(
                        "return Array.from(document.querySelectorAll('.note-item a[href*=\"/explore/\"]')).map(a=>a.href)"
                    ))
                    if gate or links:
                        break
                    time.sleep(0.5)
                self.assertTrue(gate or links, "Homepage has neither a recognized gate nor current note links")
                self.assertTrue(session.screenshot().startswith(b"\xff\xd8"))
                self.assertGreater(session.info()["width"], 1000)
                if not gate:
                    try:
                        note = session.extract(links[0], SimpleNamespace(page_timeout=20))
                    except NeedsInteraction as exc:
                        self.assertTrue(str(exc))
                    else:
                        self.assertTrue(note["note_id"])
                        self.assertIn(note["source"], ("initial_state", "dom"))
                        self.assertTrue(note["title"] or note["content"] or note["images"])
                config = SimpleNamespace(keyword="Agent 面经", page_timeout=20, search_seconds=3, max_notes=3, interval_seconds=1.5)
                try:
                    results = session.search(config)
                except NeedsInteraction as exc:
                    self.assertTrue(str(exc))
                else:
                    self.assertGreater(len(results), 0)
                    self.assertLessEqual(len(results), 3)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
