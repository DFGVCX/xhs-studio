from __future__ import annotations

import csv
import io
import json
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image
from pydantic import ValidationError

from xhs_console.config import (Settings, clean_filename, load_settings, normalize_urls, note_id,
                                resolve_output_root, save_settings)
from xhs_console.storage import ResultStore, _download_image, _public_media_url


ID = "66c74c69000000001d017ab1"
URL = f"https://www.xiaohongshu.com/explore/{ID}?xsec_token=signed%2Btoken&xsec_source=pc_search"
MEDIA = "https://sns-webpic-qc.xhscdn.com/example.webp"


def png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (3, 2), (80, 100, 120)).save(stream, format="PNG")
    return stream.getvalue()


def fake_session(body: bytes, status: int = 200, headers: dict | None = None):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = status
    response.headers = headers or {"Content-Length": str(len(body))}
    response.iter_content.return_value = [body]
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = response
    return session


class ConfigTests(unittest.TestCase):
    def test_validates_path_and_ranges(self):
        for keyword in ("../notes", "..\\notes", "C:\\notes", "NUL", "CON.txt", "a/b", "a|b", "a..b", "", "a\nname"):
            with self.subTest(keyword=keyword), self.assertRaises(ValidationError):
                Settings(keyword=keyword)
        for kwargs in ({"max_notes": 0}, {"search_seconds": 3601}, {"interval_seconds": float("nan")}, {"mode": "urls"}, {"browser": "firefox"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                Settings(**kwargs)
        self.assertEqual(clean_filename("CON.txt"), "_CON.txt")
        self.assertEqual(clean_filename("..\\test:*"), "_test__")

    def test_output_directory_defaults_local_and_accepts_absolute_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(resolve_output_root(root, Settings().output_dir), root / "Information")
            custom = root / "custom output"
            configured = Settings(output_dir=str(custom))
            self.assertEqual(resolve_output_root(root, configured.output_dir), custom)
            for invalid in ("../outside", "..\\outside", "\\\\server\\share", "//server/share", "~\\notes", "bad|name"):
                with self.subTest(output_dir=invalid), self.assertRaises(ValidationError):
                    Settings(output_dir=invalid)

    def test_signed_urls_identity_dedup_and_allowed_short_link(self):
        another = f"https://www.xiaohongshu.com/search_result/{ID}?xsec_token=another"
        self.assertEqual(normalize_urls([URL, another, URL]), [URL])
        self.assertEqual(normalize_urls([URL, another]), [another])
        self.assertEqual(normalize_urls([f"https://www.xiaohongshu.com/explore/{ID}", URL]), [URL])
        self.assertEqual(normalize_urls([URL, f"https://www.xiaohongshu.com/explore/{ID}"]), [URL])
        self.assertEqual(note_id(URL), ID)
        discovery = f"https://www.xiaohongshu.com/discovery/item/{ID}?xsec_token=mobile"
        self.assertEqual(note_id(discovery), ID)
        self.assertEqual(normalize_urls([URL, discovery]), [discovery])
        short = "http://xhslink.com/a/AbCd123"
        self.assertEqual(normalize_urls([short]), [short])
        self.assertEqual(note_id(short), "")

    def test_rejects_non_note_urls(self):
        for url in (
            "https://example.com/explore/" + ID,
            "https://www.xiaohongshu.com.evil.com/explore/" + ID,
            "https://user:password@www.xiaohongshu.com/explore/" + ID,
            "http://www.xiaohongshu.com/explore/" + ID,
            "https://www.xiaohongshu.com:443/explore/" + ID,
            "https://www.xiaohongshu.com/explore/../" + ID,
            URL + "#fragment",
            URL + "<script>",
            "javascript:alert(1)",
            "https://xhslink.com/../admin",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_urls([url])

    def test_save_roundtrip_and_malformed_config_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = Settings(keyword="测试采集", mode="urls", urls=[URL])
            save_settings(root, original)
            self.assertEqual(load_settings(root), original)
            path = root / "runtime" / "config.json"
            path.write_text('{"broken":', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(root)
            self.assertEqual(path.read_text(encoding="utf-8"), '{"broken":')

    def test_legacy_import_clamps_values_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = URL + "\n面经|999999|-3|0|2|1|1\n"
            (root / "parameter.txt").write_text(source, encoding="utf-8")
            settings = load_settings(root)
            self.assertEqual(settings.mode, "urls")
            self.assertEqual(settings.search_seconds, 3600)
            self.assertEqual(settings.interval_seconds, 1)
            self.assertEqual(settings.naming, "content")
            self.assertTrue(settings.headless)
            self.assertEqual(settings.max_notes, 20)
            self.assertEqual((root / "parameter.txt").read_text(encoding="utf-8"), source)
            self.assertFalse((root / "runtime" / "config.json").exists())


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ResultStore(self.root, "测试面经")
        self.config = Settings(keyword="测试面经", retries=0)
        self.events = []

    def emit(self, level, message):
        self.events.append((level, message))

    def test_custom_output_root_keeps_results_and_index_separate(self):
        custom = self.root / "chosen library"
        store = ResultStore(self.root, "自定义资料", str(custom))
        self.assertEqual(store.output_root, custom)
        self.assertEqual(store.directory, custom / "自定义资料" / "console")
        self.assertTrue(store._path.is_relative_to(self.root / "runtime" / "collections" / "by-output"))

    def note(self, **updates):
        result = {"note_id": ID, "url": URL, "title": "一篇面经", "author": "作者", "content": "正文第一行\n正文第二行", "images": []}
        result.update(updates)
        return result

    def test_upsert_resume_and_exports_do_not_duplicate_markdown(self):
        self.store.save(self.note(), self.config, self.emit)
        self.store.save(self.note(title="更新后的面经"), self.config, self.emit)
        self.assertEqual(len(self.store.records()), 1)
        restored = ResultStore(self.root, "测试面经")
        self.assertTrue(restored.has(ID))
        exports = restored.export()
        restored.export()
        markdown = (restored.directory / "测试面经.md").read_text(encoding="utf-8")
        self.assertEqual(markdown.count("## 更新后的面经"), 1)
        self.assertNotIn("## 一篇面经", markdown)
        self.assertTrue(all(item["url"].startswith("/api/files/") for item in exports))
        with zipfile.ZipFile(restored.directory / "测试面经.zip") as archive:
            self.assertIn("测试面经.csv", archive.namelist())

    def test_missing_metadata_is_kept_and_identified_as_partial(self):
        result = self.store.save({"url": URL}, self.config, self.emit)
        self.assertEqual(result["title"], "未命名笔记")
        self.assertEqual(result["author"], "未知作者")
        self.assertEqual(result["status"], "partial")
        self.assertFalse(self.store.has(ID))
        self.assertEqual(len(self.store.records()), 1)

    def test_discovery_share_destination_can_be_saved(self):
        discovery = f"https://www.xiaohongshu.com/discovery/item/{ID}?xsec_token=mobile"
        result = self.store.save(self.note(url=discovery), self.config, self.emit)
        self.assertEqual(result["note_id"], ID)
        self.assertEqual(result["url"], discovery)
        self.assertTrue(self.store.has(ID))

    def test_html_response_retains_text_as_partial(self):
        with patch("xhs_console.storage._public_media_url", return_value=MEDIA), patch("xhs_console.storage.requests.Session", return_value=fake_session(b"<html>login required</html>")):
            saved = self.store.save(self.note(images=[MEDIA]), self.config, self.emit)
        self.assertEqual(saved["status"], "partial")
        self.assertEqual(saved["image_count"], 0)
        self.assertEqual(saved["content"], "正文第一行\n正文第二行")
        self.assertTrue(saved["errors"])
        self.assertFalse(self.store.has(ID))
        self.assertEqual(list((self.store.directory / "Images").iterdir()), [])

    def test_valid_image_preserves_dimensions_and_export_links(self):
        with patch("xhs_console.storage._public_media_url", return_value=MEDIA), patch("xhs_console.storage.requests.Session", return_value=fake_session(png_bytes())):
            saved = self.store.save(self.note(title="面经 [一]", images=[MEDIA]), self.config, self.emit)
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["image_count"], 1)
        image_path = self.store.directory / saved["local_images"][0]
        with Image.open(image_path) as picture:
            self.assertEqual(picture.size, (3, 2))
            self.assertEqual(picture.getpixel((0, 0)), (80, 100, 120))
        self.store.export()
        markdown = (self.store.directory / "测试面经.md").read_text(encoding="utf-8")
        self.assertIn("%20%5B", markdown)
        with zipfile.ZipFile(self.store.directory / "测试面经.zip") as archive:
            self.assertIn(saved["local_images"][0], archive.namelist())

    def test_stop_is_not_swallowed_and_text_has_already_been_saved(self):
        class StopRequested(Exception):
            pass

        def stopped(seconds=0):
            raise StopRequested()

        with self.assertRaises(StopRequested):
            self.store.save(self.note(images=[MEDIA]), self.config, self.emit, stopped)
        restored = ResultStore(self.root, "测试面经")
        self.assertEqual(restored.records()[0]["content"], "正文第一行\n正文第二行")
        self.assertEqual(restored.records()[0]["status"], "partial")
        self.assertFalse(restored.has(ID))

    def test_csv_formula_injection_is_escaped(self):
        self.store.save(self.note(title='=HYPERLINK("https://example.com")', author="  @SUM(1,1)", content="-1+1"), self.config, self.emit)
        self.store.export()
        path = self.store.directory / "测试面经.csv"
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        with path.open(encoding="utf-8-sig", newline="") as stream:
            row = list(csv.DictReader(stream))[0]
        self.assertTrue(row["title"].startswith("'="))
        self.assertTrue(row["author"].startswith("'@"))
        self.assertTrue(row["content"].startswith("'-"))

    def test_image_download_can_be_disabled(self):
        config = self.config.model_copy(update={"download_images": False})
        with patch("xhs_console.storage._download_image") as download:
            saved = self.store.save(self.note(images=[MEDIA]), config, self.emit)
        download.assert_not_called()
        self.assertEqual(saved["images"], [MEDIA])
        self.assertEqual(saved["status"], "success")
        self.assertTrue(self.store.has(ID))
        self.assertFalse(self.store.has(ID, require_images=True))

    def test_non_cdn_and_private_addresses_are_rejected(self):
        with self.assertRaises(ValueError):
            _public_media_url("https://example.com/image.png")
        with patch("xhs_console.storage.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                _public_media_url(MEDIA)

    def test_redirect_to_private_host_is_not_requested(self):
        session = fake_session(b"", status=302, headers={"Location": "http://127.0.0.1/private"})
        destination = self.store.directory / "Images" / "example.png"
        with patch("xhs_console.storage.requests.Session", return_value=session), patch("xhs_console.storage.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]):
            with self.assertRaises(ValueError):
                _download_image(MEDIA, destination, self.config, lambda seconds=0: None)
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
