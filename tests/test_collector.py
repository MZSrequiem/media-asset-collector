import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media_asset_collector as collector


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
