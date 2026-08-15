from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import queue
import re
import shutil
import sys
import threading
import traceback
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, filedialog, messagebox, ttk
import tkinter as tk


SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup", ".smi"}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def decode_text(value: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "big5", "shift_jis", "latin-1"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return value.decode("utf-8", errors="replace")


def bdecode(data: bytes, pos: int = 0):
    if pos >= len(data):
        raise ValueError("种子数据不完整")
    token = data[pos : pos + 1]
    if token == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1 : end]), end + 1
    if token == b"l":
        values, pos = [], pos + 1
        while data[pos : pos + 1] != b"e":
            value, pos = bdecode(data, pos)
            values.append(value)
        return values, pos + 1
    if token == b"d":
        values, pos = {}, pos + 1
        while data[pos : pos + 1] != b"e":
            key, pos = bdecode(data, pos)
            value, pos = bdecode(data, pos)
            values[key] = value
        return values, pos + 1
    if token.isdigit():
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        start, end = colon + 1, colon + 1 + length
        if end > len(data):
            raise ValueError("种子字符串越界")
        return data[start:end], end
    raise ValueError(f"未知的 Bencode 标记: {token!r}")


def torrent_title(path: Path) -> tuple[str, str]:
    """返回（建议名称，解析状态）。只读取种子，不改写。"""
    try:
        raw = path.read_bytes()
        root, end = bdecode(raw)
        if end != len(raw) or not isinstance(root, dict):
            raise ValueError("根节点格式不正确")
        info = root.get(b"info")
        if not isinstance(info, dict):
            raise ValueError("缺少 info 字段")
        name = info.get(b"name.utf-8") or info.get(b"name")
        if not isinstance(name, bytes):
            raise ValueError("缺少资源名称")
        return decode_text(name), "已读取种子内部名称"
    except Exception as exc:
        return path.stem, f"无法解析，保留原名: {exc}"


def safe_name(name: str, fallback: str = "未命名") -> str:
    name = INVALID_FILENAME.sub("_", name).strip().rstrip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:180].rstrip(" .") or fallback


def pretty_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


@dataclass
class Item:
    kind: str
    extension: str
    size: int
    source: str
    target_relative: str
    group: str
    note: str


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def unique_relative(folder: Path, filename: str, occupied: set[str], salt: str) -> Path:
    candidate = folder / filename
    key = str(candidate).casefold()
    if key not in occupied:
        occupied.add(key)
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    short = hashlib.sha1(salt.encode("utf-8", errors="replace")).hexdigest()[:8]
    candidate = folder / f"{stem} [{short}]{suffix}"
    counter = 2
    while str(candidate).casefold() in occupied:
        candidate = folder / f"{stem} [{short}-{counter}]{suffix}"
        counter += 1
    occupied.add(str(candidate).casefold())
    return candidate


def scan(source: Path, output: Path | None = None, progress=None, together: bool = True) -> list[Item]:
    source = source.resolve()
    output = output.resolve() if output else None
    occupied: set[str] = set()
    items: list[Item] = []
    count = 0

    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        dirs[:] = [
            d for d in dirs
            if not (output and is_relative_to((root_path / d).resolve(), output))
            and d != "__pycache__"
            and not ((root_path / d) / "统计数据.json").is_file()
            and not ((root_path / d) / ".media_asset_collector_output").is_file()
        ]
        for filename in files:
            # macOS AppleDouble 元数据可能带有 .torrent/.srt 后缀，但并非真实资源文件。
            if filename.startswith("._"):
                continue
            path = root_path / filename
            ext = path.suffix.lower()
            if ext != ".torrent" and ext not in SUBTITLE_EXTENSIONS:
                continue
            try:
                size = path.stat().st_size
                relative = path.relative_to(source)
            except (OSError, ValueError):
                continue
            group = relative.parts[0] if len(relative.parts) > 1 else "根目录"
            if ext == ".torrent":
                title, note = torrent_title(path)
                target_folder = Path("提取文件") if together else Path("种子")
                target = unique_relative(target_folder, safe_name(title) + ".torrent", occupied, str(relative))
                kind = "种子"
            else:
                if together:
                    # 同目录模式下，将原路径编入字幕名以避免多集重名。
                    subtitle_parts = [*relative.parts[:-1], path.stem]
                    flattened_name = safe_name(" - ".join(subtitle_parts), "字幕") + ext
                    target = unique_relative(Path("提取文件"), flattened_name, occupied, str(relative))
                    note = "原相对路径已合并到文件名，与种子保存在同一目录"
                else:
                    # 分类模式下保留项目内的层级。
                    if len(relative.parts) > 1:
                        target = Path("字幕") / safe_name(relative.parts[0]) / Path(*relative.parts[1:])
                    else:
                        target = Path("字幕") / "根目录" / filename
                    target = unique_relative(target.parent, safe_name(target.name), occupied, str(relative))
                    note = "按种子和字幕分类，保留项目内相对路径"
                kind = "字幕"
            items.append(Item(kind, ext, size, str(path), str(target), group, note))
            count += 1
            if progress and count % 50 == 0:
                progress(f"已找到 {count} 个目标文件…")
    items.sort(key=lambda x: (x.kind, x.group.casefold(), x.source.casefold()))
    return items


def write_reports(items: list[Item], output: Path, source: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / ".media_asset_collector_output").write_text(
        "This directory was generated by Media Asset Collector.\n", encoding="ascii"
    )
    csv_path = output / "文件清单.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["类型", "扩展名", "大小(字节)", "大小", "所属项目", "源文件", "整理后路径", "备注"])
        for item in items:
            writer.writerow([item.kind, item.extension, item.size, pretty_size(item.size), item.group,
                             item.source, item.target_relative, item.note])

    counts = Counter(item.kind for item in items)
    extensions = Counter(item.extension for item in items)
    groups = Counter(item.group for item in items)
    total_size = sum(item.size for item in items)
    data = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(source), "output": str(output), "total_files": len(items),
        "total_bytes": total_size, "by_type": dict(counts), "by_extension": dict(extensions),
        "by_project": dict(groups), "files": [asdict(item) for item in items],
    }
    (output / "统计数据.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    ext_rows = "".join(f"<tr><td>{html.escape(k or '(无扩展名)')}</td><td>{v}</td></tr>" for k, v in extensions.most_common())
    group_rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in groups.most_common())
    file_rows = "".join(
        f"<tr><td>{html.escape(i.kind)}</td><td>{html.escape(i.group)}</td>"
        f"<td>{html.escape(pretty_size(i.size))}</td><td>{html.escape(i.target_relative)}</td>"
        f"<td title='{html.escape(i.source, quote=True)}'>{html.escape(Path(i.source).name)}</td></tr>" for i in items
    )
    report = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>影视资源整理统计</title>
<style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:32px;color:#202124;background:#f6f7f9}}
.cards{{display:flex;gap:16px;flex-wrap:wrap}}.card{{background:white;padding:18px 24px;border-radius:12px;box-shadow:0 2px 10px #0001;min-width:150px}}
.n{{font-size:28px;font-weight:700;color:#3157d5}}table{{border-collapse:collapse;width:100%;background:white;margin:16px 0 28px}}
th,td{{padding:9px 12px;border-bottom:1px solid #e5e7eb;text-align:left}}th{{background:#eef2ff;position:sticky;top:0}}
.scroll{{max-height:560px;overflow:auto}}code{{word-break:break-all}}</style></head><body>
<h1>影视资源整理统计</h1><p>生成时间：{html.escape(data['generated_at'])}<br>源目录：<code>{html.escape(str(source))}</code></p>
<div class='cards'><div class='card'><div class='n'>{len(items)}</div>文件总数</div>
<div class='card'><div class='n'>{counts.get('种子', 0)}</div>种子</div><div class='card'><div class='n'>{counts.get('字幕', 0)}</div>字幕</div>
<div class='card'><div class='n'>{pretty_size(total_size)}</div>合计大小</div></div>
<h2>按扩展名</h2><table><tr><th>扩展名</th><th>数量</th></tr>{ext_rows}</table>
<h2>按所属项目</h2><table><tr><th>项目</th><th>数量</th></tr>{group_rows}</table>
<h2>文件列表</h2><div class='scroll'><table><tr><th>类型</th><th>项目</th><th>大小</th><th>整理后路径</th><th>原文件名</th></tr>{file_rows}</table></div>
</body></html>"""
    (output / "统计报告.html").write_text(report, encoding="utf-8")


def collect(items: list[Item], output: Path, source: Path, progress=None) -> tuple[int, list[str]]:
    output.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    copied = 0
    for index, item in enumerate(items, 1):
        src, dst = Path(item.source), output / item.target_relative
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                if src.stat().st_size == dst.stat().st_size and hashlib.sha256(src.read_bytes()).digest() == hashlib.sha256(dst.read_bytes()).digest():
                    copied += 1
                    continue
                raise FileExistsError("目标已存在且内容不同")
            shutil.copy2(src, dst)
            copied += 1
        except Exception as exc:
            errors.append(f"{src}: {exc}")
        if progress and (index % 25 == 0 or index == len(items)):
            progress(f"正在复制：{index}/{len(items)}")
    write_reports(items, output, source)
    if errors:
        (output / "错误日志.txt").write_text("\n".join(errors), encoding="utf-8")
    return copied, errors


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("影视种子与字幕整理工具")
        self.geometry("1040x680")
        self.minsize(820, 520)
        self.items: list[Item] = []
        self.events: queue.Queue = queue.Queue()
        # 启动脚本位于工具目录，默认扫描其上一级资源目录。
        default_source = Path(__file__).resolve().parent.parent
        self.source_var = tk.StringVar(value=str(default_source))
        default_output = default_source.parent / f"影视资源提取结果_{datetime.now():%Y%m%d_%H%M%S}"
        self.output_var = tk.StringVar(value=str(default_output))
        self.together_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="请先扫描预览，确认后再复制整理。")
        self._build()
        self.after(100, self._poll)

    def _build(self):
        pad = {"padx": 12, "pady": 6}
        paths = ttk.Frame(self)
        paths.pack(fill=X, pady=(10, 0))
        ttk.Label(paths, text="资源目录").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(paths, textvariable=self.source_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(paths, text="选择…", command=self._choose_source).grid(row=0, column=2, **pad)
        ttk.Label(paths, text="输出目录").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(paths, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(paths, text="选择…", command=self._choose_output).grid(row=1, column=2, **pad)
        paths.columnconfigure(1, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill=X, padx=12, pady=6)
        self.scan_button = ttk.Button(actions, text="1. 扫描预览", command=self._start_scan)
        self.scan_button.pack(side=LEFT, padx=(0, 8))
        self.collect_button = ttk.Button(actions, text="2. 复制整理并生成报告", command=self._start_collect, state="disabled")
        self.collect_button.pack(side=LEFT)
        ttk.Checkbutton(
            actions,
            text="种子和字幕放在同一文件夹",
            variable=self.together_var,
            command=self._layout_changed,
        ).pack(side=LEFT, padx=18)
        ttk.Label(actions, text="只读源目录：不移动、不改名、不删除", foreground="#287a3f").pack(side=RIGHT)

        ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken").pack(fill=X, side="bottom")
        columns = ("kind", "group", "size", "source", "target")
        self.tree = ttk.Treeview(self, columns=columns, show="headings")
        for key, title, width in (("kind", "类型", 60), ("group", "所属项目", 190), ("size", "大小", 85),
                                  ("source", "原文件", 300), ("target", "整理后路径", 340)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=50)
        ybar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True, padx=(12, 0), pady=(4, 12))
        ybar.pack(side=RIGHT, fill="y", padx=(0, 12), pady=(4, 12))

    def _choose_source(self):
        value = filedialog.askdirectory(title="选择影视资源目录", initialdir=self.source_var.get())
        if value:
            self.source_var.set(value)
            self.items = []
            self.collect_button.configure(state="disabled")

    def _choose_output(self):
        value = filedialog.askdirectory(title="选择输出目录", initialdir=str(Path(self.output_var.get()).parent), mustexist=False)
        if value:
            self.output_var.set(value)

    def _set_busy(self, busy: bool):
        self.scan_button.configure(state="disabled" if busy else "normal")
        self.collect_button.configure(state="disabled" if busy or not self.items else "normal")

    def _layout_changed(self):
        self.items = []
        self.tree.delete(*self.tree.get_children())
        self.collect_button.configure(state="disabled")
        mode = "同一文件夹" if self.together_var.get() else "种子/字幕分类"
        self.status_var.set(f"已切换为“{mode}”模式，请重新扫描预览。")

    def _start_scan(self):
        source, output = Path(self.source_var.get()), Path(self.output_var.get())
        if not source.is_dir():
            messagebox.showerror("目录无效", "请选择存在的资源目录。")
            return
        if source.resolve() == output.resolve():
            messagebox.showerror("目录无效", "输出目录不能与资源目录相同。")
            return
        self._set_busy(True)
        self.status_var.set("正在只读扫描…")
        threading.Thread(target=self._scan_worker, args=(source, output, self.together_var.get()), daemon=True).start()

    def _scan_worker(self, source, output, together):
        try:
            items = scan(source, output, lambda msg: self.events.put(("status", msg)), together=together)
            self.events.put(("scanned", items))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _start_collect(self):
        if not self.items:
            return
        output = Path(self.output_var.get())
        source = Path(self.source_var.get())
        answer = messagebox.askyesno("确认复制", f"将把 {len(self.items)} 个文件复制到：\n{output}\n\n源文件不会被改动。是否继续？")
        if not answer:
            return
        self._set_busy(True)
        self.status_var.set("正在复制整理…")
        threading.Thread(target=self._collect_worker, args=(source, output), daemon=True).start()

    def _collect_worker(self, source, output):
        try:
            copied, errors = collect(self.items, output, source, lambda msg: self.events.put(("status", msg)))
            self.events.put(("collected", (copied, errors, output)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "status":
                    self.status_var.set(payload)
                elif event == "scanned":
                    self.items = payload
                    self.tree.delete(*self.tree.get_children())
                    for item in self.items:
                        self.tree.insert("", END, values=(item.kind, item.group, pretty_size(item.size), item.source, item.target_relative))
                    counts = Counter(i.kind for i in self.items)
                    self.status_var.set(f"扫描完成：种子 {counts.get('种子', 0)} 个，字幕 {counts.get('字幕', 0)} 个，合计 {len(self.items)} 个。")
                    self._set_busy(False)
                elif event == "collected":
                    copied, errors, output = payload
                    self._set_busy(False)
                    self.status_var.set(f"已完成：{copied} 个文件，{len(errors)} 个错误。")
                    messagebox.showinfo("整理完成", f"已处理 {copied} 个文件。\n错误：{len(errors)}\n\n统计报告：\n{output / '统计报告.html'}")
                elif event == "error":
                    self._set_busy(False)
                    self.status_var.set("操作失败。")
                    messagebox.showerror("操作失败", payload)
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main() -> int:
    parser = argparse.ArgumentParser(description="只读扫描影视目录，复制整理种子与字幕并生成统计。")
    parser.add_argument("--source", type=Path, help="资源目录")
    parser.add_argument("--output", type=Path, help="输出目录")
    parser.add_argument("--preview", action="store_true", help="仅扫描预览，不写入任何文件")
    parser.add_argument("--separate", action="store_true", help="将种子和字幕分类保存（默认保存在同一文件夹）")
    args = parser.parse_args()
    if args.source:
        if not args.source.is_dir():
            parser.error("资源目录不存在")
        if not args.preview and not args.output:
            parser.error("非预览模式必须指定 --output")
        items = scan(args.source, args.output, together=not args.separate)
        counts = Counter(i.kind for i in items)
        print(f"种子: {counts.get('种子', 0)}")
        print(f"字幕: {counts.get('字幕', 0)}")
        print(f"合计: {len(items)} ({pretty_size(sum(i.size for i in items))})")
        if not args.preview:
            copied, errors = collect(items, args.output, args.source)
            print(f"已处理: {copied}，错误: {len(errors)}")
            return 1 if errors else 0
        return 0
    App().mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        log_path = Path(__file__).with_name("启动错误.log")
        try:
            log_path.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("启动失败", f"{exc}\n\n详细信息已写入：\n{log_path}")
            root.destroy()
        except Exception:
            pass
        raise
