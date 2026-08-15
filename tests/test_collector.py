import sys
import tempfile
import unittest
import hashlib
import threading
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media_asset_collector as collector


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"
    raise TypeError(type(value))


class CollectorTests(unittest.TestCase):
    def test_graphical_app_can_be_created(self):
        app = collector.App()
        try:
            app.withdraw()
            app.update_idletasks()
            self.assertIn("v1.2.1", app.title())
            self.assertEqual(app.hash_check.cget("text"), "生成视频 SHA-256")
            self.assertGreaterEqual(len(app.tooltips), 11)
            tooltip_text = "\n".join(tip.text for tip in app.tooltips)
            self.assertIn("数小时", tooltip_text)
            self.assertIn("通常建议保持开启", tooltip_text)
            app.tooltips[5]._show()
            app.update_idletasks()
            self.assertIsNotNone(app.tooltips[5].window)
            app.tooltips[5]._hide()
        finally:
            app.destroy()

    def test_together_and_separate_layouts(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "library"
            first = source / "Show A" / "Subs"
            second = source / "Show B" / "Subs"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "episode.srt").write_text("subtitle A", encoding="utf-8")
            (second / "episode.srt").write_text("subtitle B", encoding="utf-8")
            (source / "show.torrent").write_bytes(b"d4:infod4:name10:Demo Showsee")
            (source / "._ignored.srt").write_bytes(b"not a subtitle")

            together = collector.scan_library(source, together=True, use_cache=False).items
            separate = collector.scan_library(source, together=False, use_cache=False).items

            self.assertEqual(len(together), 3)
            self.assertEqual(len(separate), 3)
            self.assertEqual({Path(item.target_relative).parts[0] for item in together}, {"提取文件"})
            self.assertEqual({Path(item.target_relative).parts[0] for item in separate}, {"种子", "字幕"})
            self.assertEqual(len({item.target_relative.casefold() for item in together}), 3)

    def test_magnet_video_matching_hash_and_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "library"
            output = Path(temp) / "output"
            source.mkdir()
            video_content = b"small test video"
            (source / "Movie.mkv").write_bytes(video_content)
            info = {b"length": len(video_content), b"name": b"Movie.mkv"}
            torrent = {b"announce": b"https://tracker.example/announce", b"info": info}
            (source / "movie.torrent").write_bytes(bencode(torrent))

            analysis = collector.scan_library(source, hash_videos=True, use_cache=False)

            torrent_item = next(item for item in analysis.items if item.kind == "种子")
            self.assertEqual(torrent_item.infohash, hashlib.sha1(bencode(info)).hexdigest().upper())
            self.assertTrue(torrent_item.magnet.startswith("magnet:?xt=urn:btih:"))
            self.assertEqual(torrent_item.match_status, "完全匹配")
            self.assertEqual(analysis.videos[0].match_status, "已精确匹配")
            self.assertEqual(analysis.videos[0].sha256, hashlib.sha256(video_content).hexdigest().upper())

            collector.write_reports(analysis.items, output, source, analysis.videos)
            for filename in ("磁力链接.txt", "视频媒体信息.csv", "文件清单.csv", "重复项报告.csv",
                             "统计报告.html", "统计数据.json"):
                self.assertTrue((output / filename).is_file(), filename)
            self.assertIn(torrent_item.magnet, (output / "磁力链接.txt").read_text(encoding="utf-8"))

    def test_generated_output_is_not_scanned_again(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            output = source / "old-output"
            output.mkdir()
            (output / "统计数据.json").write_text("{}", encoding="utf-8")
            (output / "copied.srt").write_text("copied", encoding="utf-8")
            (source / "original.srt").write_text("original", encoding="utf-8")

            items = collector.scan_library(source, use_cache=False).items

            self.assertEqual([Path(item.source).name for item in items], ["original.srt"])

    def test_matching_levels(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            (source / "Actual").mkdir()
            exact_data = b"exact-video"
            named_data = b"named-video-content"
            suspected_data = b"suspected-video-content-longer"
            (source / "Exact.mkv").write_bytes(exact_data)
            (source / "Actual" / "Show.S01E01.mkv").write_bytes(named_data)
            (source / "Actual" / "Other.S01E02.mp4").write_bytes(suspected_data)

            torrents = {
                "exact.torrent": {b"length": len(exact_data), b"name": b"Exact.mkv"},
                "named.torrent": {
                    b"files": [{b"length": len(named_data), b"path": [b"Original", b"Show.S01E01.mkv"]}],
                    b"name": b"Named",
                },
                "suspected.torrent": {
                    b"files": [{b"length": len(suspected_data), b"path": [b"Original", b"Rip.S01E02.mkv"]}],
                    b"name": b"Suspected",
                },
            }
            for filename, info in torrents.items():
                (source / filename).write_bytes(bencode({b"info": info}))

            analysis = collector.scan_library(source, use_cache=False)
            statuses = {Path(item.source).name: item.match_status for item in analysis.items if item.kind == "种子"}
            self.assertEqual(statuses["exact.torrent"], "完全匹配")
            self.assertEqual(statuses["named.torrent"], "可靠匹配")
            self.assertEqual(statuses["suspected.torrent"], "疑似匹配")

    def test_project_group_skips_archive_container_and_names_scan_root(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "资源"
            project = source / "完" / "Adolescence" / "Subs"
            project.mkdir(parents=True)
            video_content = b"episode video"
            (project / "Adolescence.S01E01.mkv").write_bytes(video_content)
            (project / "Adolescence.S01E01.srt").write_text("subtitle", encoding="utf-8")
            info = {b"length": len(video_content), b"name": b"Adolescence.S01E01.mkv"}
            (project / "Adolescence.torrent").write_bytes(bencode({b"info": info}))
            (source / "root.srt").write_text("root subtitle", encoding="utf-8")

            analysis = collector.scan_library(source, use_cache=False)
            nested_items = [item for item in analysis.items if "Adolescence" in Path(item.source).parts]
            root_item = next(item for item in analysis.items if Path(item.source).name == "root.srt")

            self.assertTrue(nested_items)
            self.assertEqual({item.group for item in nested_items}, {"Adolescence"})
            self.assertEqual(root_item.group, "资源")
            self.assertEqual(analysis.videos[0].match_status, "已精确匹配")

    def test_cache_reuse_and_invalidation(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "library"
            cache = Path(temp) / "scan-cache.json"
            source.mkdir()
            video = source / "Movie.mkv"
            subtitle = source / "Movie.srt"
            video.write_bytes(b"video content")
            subtitle.write_text("subtitle content", encoding="utf-8")

            first = collector.scan_library(source, hash_videos=True, cache_path=cache)
            second = collector.scan_library(source, hash_videos=True, cache_path=cache)
            self.assertEqual(first.cache_hits, 0)
            self.assertGreaterEqual(second.cache_hits, 2)

            subtitle.write_text("changed subtitle content", encoding="utf-8")
            third = collector.scan_library(source, hash_videos=True, cache_path=cache)
            self.assertEqual(third.cache_hits, 1)

    def test_cancel_returns_safe_partial_result(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            (source / "Movie.srt").write_text("subtitle", encoding="utf-8")
            cancel = threading.Event()
            cancel.set()
            analysis = collector.scan_library(source, use_cache=False, cancel_event=cancel)
            self.assertTrue(analysis.cancelled)
            self.assertEqual(analysis.items, [])

    def test_duplicate_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            for folder in ("A", "B", "C"):
                (source / folder).mkdir()
            (source / "A" / "same.srt").write_text("identical", encoding="utf-8")
            (source / "B" / "copy.srt").write_text("identical", encoding="utf-8")
            (source / "C" / "same.srt").write_text("different", encoding="utf-8")
            (source / "A" / "one.mkv").write_bytes(b"same video")
            (source / "B" / "two.mkv").write_bytes(b"same video")

            analysis = collector.scan_library(source, hash_videos=True, use_cache=False)
            subtitles = [item for item in analysis.items if item.kind == "字幕"]
            self.assertEqual(sum("重复字幕" in item.duplicate_status for item in subtitles), 2)
            self.assertTrue(any("同名字幕" in item.duplicate_status for item in subtitles))
            self.assertTrue(all("重复视频" in video.duplicate_status for video in analysis.videos))


if __name__ == "__main__":
    unittest.main()
