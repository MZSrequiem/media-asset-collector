import sys
import tempfile
import unittest
import hashlib
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

            together = collector.scan(source, together=True)
            separate = collector.scan(source, together=False)

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

            analysis = collector.scan_library(source, hash_videos=True)

            torrent_item = next(item for item in analysis.items if item.kind == "种子")
            self.assertEqual(torrent_item.infohash, hashlib.sha1(bencode(info)).hexdigest().upper())
            self.assertTrue(torrent_item.magnet.startswith("magnet:?xt=urn:btih:"))
            self.assertEqual(torrent_item.match_status, "已匹配本地视频")
            self.assertEqual(analysis.videos[0].match_status, "已找到对应种子")
            self.assertEqual(analysis.videos[0].sha256, hashlib.sha256(video_content).hexdigest().upper())

            collector.write_reports(analysis.items, output, source, analysis.videos)
            for filename in ("磁力链接.txt", "视频媒体信息.csv", "文件清单.csv", "统计报告.html", "统计数据.json"):
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

            items = collector.scan(source)

            self.assertEqual([Path(item.source).name for item in items], ["original.srt"])


if __name__ == "__main__":
    unittest.main()
