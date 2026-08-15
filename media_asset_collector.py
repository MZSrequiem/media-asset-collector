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
import time
import traceback
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, filedialog, messagebox, ttk
import tkinter as tk
from urllib.parse import quote


VERSION = "1.3.0"
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup", ".smi"}
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".iso",
}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PROJECT_CONTAINER_NAMES = {
    "完", "已完", "完结", "已完结", "未完", "未完结", "连载", "更新中",
    "待整理", "已整理", "归档", "下载", "downloads", "complete", "completed",
    "电影", "影片", "movies", "movie", "电视剧", "剧集", "tv", "series",
    "动漫", "动画", "anime", "纪录片", "documentary",
}


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


@dataclass
class TorrentDetails:
    title: str
    infohash: str
    magnet: str
    files: list[tuple[str, int]]


def parse_torrent(path: Path) -> TorrentDetails:
    """读取种子名称、原始 info 字节的 SHA-1、文件列表与 tracker。"""
    raw = path.read_bytes()
    if not raw.startswith(b"d"):
        raise ValueError("根节点不是字典")
    root: dict = {}
    info_raw = None
    pos = 1
    while raw[pos : pos + 1] != b"e":
        key, pos = bdecode(raw, pos)
        value_start = pos
        value, pos = bdecode(raw, pos)
        root[key] = value
        if key == b"info":
            info_raw = raw[value_start:pos]
    if pos + 1 != len(raw):
        raise ValueError("根节点格式不正确")
    info = root.get(b"info")
    if not isinstance(info, dict) or info_raw is None:
        raise ValueError("缺少 info 字段")
    name = info.get(b"name.utf-8") or info.get(b"name")
    if not isinstance(name, bytes):
        raise ValueError("缺少资源名称")
    title = decode_text(name)

    torrent_files: list[tuple[str, int]] = []
    raw_files = info.get(b"files")
    if isinstance(raw_files, list):
        for entry in raw_files:
            if not isinstance(entry, dict) or not isinstance(entry.get(b"length"), int):
                continue
            parts = entry.get(b"path.utf-8") or entry.get(b"path")
            if not isinstance(parts, list):
                continue
            decoded = [decode_text(part) for part in parts if isinstance(part, bytes)]
            torrent_files.append(("/".join(decoded), entry[b"length"]))
    elif isinstance(info.get(b"length"), int):
        torrent_files.append((title, info[b"length"]))

    trackers: list[str] = []
    announce = root.get(b"announce")
    if isinstance(announce, bytes):
        trackers.append(decode_text(announce))
    tiers = root.get(b"announce-list")
    if isinstance(tiers, list):
        for tier in tiers:
            values = tier if isinstance(tier, list) else [tier]
            for tracker in values:
                if isinstance(tracker, bytes):
                    decoded = decode_text(tracker)
                    if decoded not in trackers:
                        trackers.append(decoded)

    infohash = hashlib.sha1(info_raw).hexdigest().upper()
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn={quote(title)}"
    for tracker in trackers:
        magnet += f"&tr={quote(tracker, safe='')}"
    return TorrentDetails(title, infohash, magnet, torrent_files)


def torrent_title(path: Path) -> tuple[str, str]:
    """返回（建议名称，解析状态）。只读取种子，不改写。"""
    try:
        return parse_torrent(path).title, "已读取种子内部名称"
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
    infohash: str = ""
    magnet: str = ""
    match_status: str = ""
    matched_videos: str = ""
    match_details: str = ""
    content_sha256: str = ""
    duplicate_status: str = ""


@dataclass
class VideoRecord:
    source: str
    relative: str
    extension: str
    size: int
    modified_at: str
    resolution_hint: str
    codec_hint: str
    sha256: str = ""
    match_status: str = "无对应种子"
    matched_torrents: str = ""
    match_method: str = ""
    duplicate_status: str = ""


@dataclass
class ScanAnalysis:
    items: list[Item]
    videos: list[VideoRecord]
    cancelled: bool = False
    elapsed_seconds: float = 0.0
    cache_hits: int = 0
    scanned_files: int = 0
    scanned_bytes: int = 0


@dataclass
class ProgressEvent:
    phase: str
    message: str
    current: int = 0
    total: int = 0
    bytes_read: int = 0
    bytes_total: int = 0
    elapsed_seconds: float = 0.0


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


def default_cache_path(source: Path) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache")) / "MediaAssetCollector"
    key = hashlib.sha1(str(source.resolve()).casefold().encode("utf-8", errors="replace")).hexdigest()
    return base / f"scan-{key}.json"


def load_scan_cache(source: Path, cache_path: Path | None = None) -> tuple[Path, dict]:
    path = cache_path or default_cache_path(source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == 1 and data.get("source") == str(source.resolve()):
            return path, data.get("files", {})
    except (OSError, ValueError, TypeError):
        pass
    return path, {}


def save_scan_cache(source: Path, path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "source": str(source.resolve()), "files": entries}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def clear_scan_cache(source: Path, cache_path: Path | None = None) -> bool:
    path = cache_path or default_cache_path(source)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def cache_key(path: Path, source: Path) -> str:
    return str(path.relative_to(source)).replace("\\", "/")


def cached_hash(cache: dict, key: str, stat: os.stat_result, field: str) -> str:
    entry = cache.get(key, {})
    if entry.get("size") == stat.st_size and entry.get("mtime_ns") == stat.st_mtime_ns:
        return str(entry.get(field, ""))
    return ""


def update_cache_entry(cache: dict, key: str, stat: os.stat_result, **values) -> None:
    entry = cache.setdefault(key, {})
    entry.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, **values})


def normalize_part(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", value)


def normalized_path_parts(value: str) -> tuple[str, ...]:
    return tuple(normalize_part(part) for part in re.split(r"[\\/]", value) if normalize_part(part))


def path_suffix_matches(local_relative: str, torrent_relative: str) -> bool:
    local_parts = normalized_path_parts(local_relative)
    torrent_parts = normalized_path_parts(torrent_relative)
    return bool(torrent_parts) and len(local_parts) >= len(torrent_parts) and local_parts[-len(torrent_parts):] == torrent_parts


def episode_token(value: str) -> str:
    patterns = (
        r"(?i)S(\d{1,2})[ ._-]*E(\d{1,3})",
        r"(?i)(?:EP?|Episode)[ ._-]*(\d{1,3})",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return "-".join(part.zfill(3) for part in match.groups())
    return ""


def subtitle_media_key(value: str) -> str:
    stem = Path(value).stem
    stem = re.sub(r"(?i)[ ._-]*(chs|cht|chi|zho|eng|jpn|简体|繁体|双语|中字|字幕)$", "", stem)
    return normalize_part(stem)


def project_group(relative: Path, source: Path) -> str:
    """返回第一个有意义的项目目录；根目录文件使用扫描目录名。"""
    folders = relative.parts[:-1]
    if not folders:
        return source.name or str(source)
    normalized_containers = {normalize_part(name) for name in PROJECT_CONTAINER_NAMES}
    for index, folder in enumerate(folders):
        has_deeper_folder = index + 1 < len(folders)
        if has_deeper_folder and normalize_part(folder) in normalized_containers:
            continue
        return folder
    return folders[-1]


def media_hints(filename: str) -> tuple[str, str]:
    resolution = re.search(r"(?i)(4320p|2160p|1440p|1080p|720p|576p|480p|4k|8k)", filename)
    codec = re.search(r"(?i)(x26[45]|h[ .]?26[45]|hevc|av1|avc|vc-1|vp9)", filename)
    return (resolution.group(1).upper() if resolution else "", codec.group(1).upper() if codec else "")


def sha256_file(
    path: Path,
    chunk_size: int = 4 * 1024 * 1024,
    cancel_event: threading.Event | None = None,
    on_chunk=None,
) -> str:
    digest = hashlib.sha256()
    processed = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("用户取消")
            digest.update(chunk)
            processed += len(chunk)
            if on_chunk:
                on_chunk(processed)
    return digest.hexdigest().upper()


def scan_library(
    source: Path,
    output: Path | None = None,
    progress=None,
    together: bool = True,
    hash_videos: bool = False,
    use_cache: bool = True,
    cache_path: Path | None = None,
    cancel_event: threading.Event | None = None,
) -> ScanAnalysis:
    started = time.monotonic()
    source = source.resolve()
    output = output.resolve() if output else None
    occupied: set[str] = set()
    items: list[Item] = []
    videos: list[VideoRecord] = []
    target_records: list[tuple[Path, os.stat_result, Path, str]] = []
    scanned_files = 0
    scanned_bytes = 0
    cache_hits = 0
    cancelled = False
    resolved_cache_path, cache = load_scan_cache(source, cache_path) if use_cache else (
        cache_path or default_cache_path(source), {}
    )

    def emit(phase: str, message: str, current=0, total=0, bytes_read=0, bytes_total=0):
        if progress:
            progress(ProgressEvent(
                phase, message, current, total, bytes_read, bytes_total, time.monotonic() - started
            ))

    emit("scan", "正在只读枚举媒体文件…")

    for root, dirs, files in os.walk(source):
        if cancel_event and cancel_event.is_set():
            cancelled = True
            break
        root_path = Path(root)
        dirs[:] = [
            d for d in dirs
            if not (output and is_relative_to((root_path / d).resolve(), output))
            and d != "__pycache__"
            and not ((root_path / d) / "统计数据.json").is_file()
            and not ((root_path / d) / ".media_asset_collector_output").is_file()
        ]
        for filename in files:
            if cancel_event and cancel_event.is_set():
                cancelled = True
                break
            # macOS AppleDouble 元数据可能带有 .torrent/.srt 后缀，但并非真实资源文件。
            if filename.startswith("._"):
                continue
            path = root_path / filename
            ext = path.suffix.lower()
            if ext not in VIDEO_EXTENSIONS and ext != ".torrent" and ext not in SUBTITLE_EXTENSIONS:
                continue
            try:
                stat = path.stat()
                relative = path.relative_to(source)
            except (OSError, ValueError):
                continue
            scanned_files += 1
            scanned_bytes += stat.st_size
            if ext in VIDEO_EXTENSIONS:
                resolution, codec = media_hints(filename)
                video = VideoRecord(
                    source=str(path),
                    relative=str(relative),
                    extension=ext,
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                    resolution_hint=resolution,
                    codec_hint=codec,
                )
                if hash_videos and use_cache:
                    video.sha256 = cached_hash(cache, cache_key(path, source), stat, "sha256")
                    if video.sha256:
                        cache_hits += 1
                videos.append(video)
            else:
                target_records.append((path, stat, relative, ext))
            if scanned_files % 100 == 0:
                emit("scan", f"已枚举 {scanned_files} 个目标文件，{pretty_size(scanned_bytes)}")

    video_name_index: dict[tuple[str, int], list[int]] = {}
    video_size_index: dict[int, list[int]] = {}
    video_group_index: dict[str, list[int]] = {}
    for index, video in enumerate(videos):
        video_name_index.setdefault((normalize_part(Path(video.source).name), video.size), []).append(index)
        video_size_index.setdefault(video.size, []).append(index)
        group = project_group(Path(video.relative), source)
        video_group_index.setdefault(group.casefold(), []).append(index)
    video_matches: dict[int, list[str]] = {}
    video_match_methods: dict[int, str] = {}
    match_priority = {"路径+大小": 3, "名称+大小": 2, "剧集编号+大小": 1}
    torrent_groups: set[str] = set()
    torrent_records = [record for record in target_records if record[3] == ".torrent"]
    subtitle_records = [record for record in target_records if record[3] in SUBTITLE_EXTENSIONS]

    emit("match", f"正在分析 {len(torrent_records)} 个种子与本地视频…", 0, len(torrent_records))
    for record_index, (path, stat, relative, ext) in enumerate(torrent_records, 1):
        if cancel_event and cancel_event.is_set():
            cancelled = True
            break
        group = project_group(relative, source)
        torrent_groups.add(group.casefold())
        try:
            details = parse_torrent(path)
            title, note = details.title, "已读取种子内部名称"
            torrent_videos = [entry for entry in details.files if Path(entry[0]).suffix.lower() in VIDEO_EXTENSIONS]
            matched_indexes: set[int] = set()
            path_count = name_count = suspected_count = 0
            for torrent_path, torrent_size in torrent_videos:
                candidates = video_size_index.get(torrent_size, [])
                exact = [index for index in candidates if path_suffix_matches(videos[index].relative, torrent_path)]
                named = [
                    index for index in candidates
                    if normalize_part(Path(videos[index].source).name) == normalize_part(Path(torrent_path).name)
                ]
                token = episode_token(torrent_path)
                suspected = [
                    index for index in candidates
                    if token and episode_token(videos[index].relative) == token
                ]
                selected: list[int] = []
                method = ""
                if exact:
                    selected, method = exact, "路径+大小"
                    path_count += 1
                elif named:
                    selected, method = named, "名称+大小"
                    name_count += 1
                elif suspected:
                    selected, method = suspected, "剧集编号+大小"
                    suspected_count += 1
                for index in selected:
                    matched_indexes.add(index)
                    video_matches.setdefault(index, []).append(title)
                    if match_priority.get(method, 0) > match_priority.get(video_match_methods.get(index, ""), 0):
                        video_match_methods[index] = method
            matched_entry_count = path_count + name_count + suspected_count
            unmatched_count = max(0, len(torrent_videos) - matched_entry_count)
            matched_names = sorted({videos[index].relative for index in matched_indexes})
            if not torrent_videos:
                match_status = "种子不含视频"
            elif matched_entry_count == len(torrent_videos) and path_count == len(torrent_videos):
                match_status = "完全匹配"
            elif matched_entry_count == len(torrent_videos) and suspected_count == 0:
                match_status = "可靠匹配"
            elif matched_entry_count == len(torrent_videos):
                match_status = "疑似匹配"
            elif matched_entry_count:
                match_status = "部分匹配"
            else:
                match_status = "未匹配"
            match_details = (
                f"种子视频 {len(torrent_videos)}；路径+大小 {path_count}；名称+大小 {name_count}；"
                f"剧集编号+大小 {suspected_count}；未匹配 {unmatched_count}"
            )
            infohash, magnet = details.infohash, details.magnet
        except Exception as exc:
            title, note = path.stem, f"无法解析，保留原名: {exc}"
            match_status, match_details, matched_names, infohash, magnet = "种子解析失败", "", [], "", ""
        target_folder = Path("提取文件") if together else Path("种子")
        target = unique_relative(target_folder, safe_name(title) + ".torrent", occupied, str(relative))
        items.append(Item(
            kind="种子", extension=ext, size=stat.st_size, source=str(path), target_relative=str(target),
            group=group, note=note, infohash=infohash, magnet=magnet, match_status=match_status,
            matched_videos=" | ".join(matched_names), match_details=match_details,
        ))
        if record_index % 10 == 0 or record_index == len(torrent_records):
            emit("match", f"已分析种子 {record_index}/{len(torrent_records)}", record_index, len(torrent_records))

    for index, video in enumerate(videos):
        matches = sorted(set(video_matches.get(index, [])))
        if matches:
            method = video_match_methods.get(index, "")
            video.match_status = {
                "路径+大小": "已精确匹配",
                "名称+大小": "已名称匹配",
                "剧集编号+大小": "疑似匹配",
            }.get(method, "已匹配")
            video.matched_torrents = " | ".join(matches)
            video.match_method = method

    emit("subtitle", f"正在检查 {len(subtitle_records)} 个字幕的关联和内容…", 0, len(subtitle_records))
    for record_index, (path, stat, relative, ext) in enumerate(subtitle_records, 1):
        if cancel_event and cancel_event.is_set():
            cancelled = True
            break
        group = project_group(relative, source)
        key = cache_key(path, source)
        content_hash = cached_hash(cache, key, stat, "sha256") if use_cache else ""
        if content_hash:
            cache_hits += 1
        elif not (cancel_event and cancel_event.is_set()):
            try:
                content_hash = sha256_file(path, cancel_event=cancel_event)
                update_cache_entry(cache, key, stat, sha256=content_hash)
            except InterruptedError:
                cancelled = True
            except OSError:
                content_hash = ""

        group_video_indexes = video_group_index.get(group.casefold(), [])
        subtitle_key = subtitle_media_key(path.name)
        sub_episode = episode_token(path.name)
        direct = []
        for video_index in group_video_indexes:
            video = videos[video_index]
            video_key = subtitle_media_key(Path(video.source).name)
            if subtitle_key and (subtitle_key == video_key or subtitle_key.startswith(video_key) or video_key.startswith(subtitle_key)):
                direct.append(video.relative)
            elif sub_episode and sub_episode == episode_token(video.relative):
                direct.append(video.relative)
        if direct:
            match_status = "已关联本地视频"
            match_details = " | ".join(sorted(set(direct)))
        elif group_video_indexes:
            match_status = "项目内有视频（未精确关联）"
            match_details = ""
        elif group.casefold() in torrent_groups:
            match_status = "项目内有种子（未关联视频）"
            match_details = ""
        else:
            match_status = "未找到关联视频或种子"
            match_details = ""

        if together:
            subtitle_parts = [*relative.parts[:-1], path.stem]
            flattened_name = safe_name(" - ".join(subtitle_parts), "字幕") + ext
            target = unique_relative(Path("提取文件"), flattened_name, occupied, str(relative))
            note = "原相对路径已合并到文件名，与种子保存在同一目录"
        else:
            if len(relative.parts) > 1:
                target = Path("字幕") / safe_name(relative.parts[0]) / Path(*relative.parts[1:])
            else:
                target = Path("字幕") / "根目录" / path.name
            target = unique_relative(target.parent, safe_name(target.name), occupied, str(relative))
            note = "按种子和字幕分类，保留项目内相对路径"
        items.append(Item(
            kind="字幕", extension=ext, size=stat.st_size, source=str(path), target_relative=str(target),
            group=group, note=note, match_status=match_status, match_details=match_details,
            content_sha256=content_hash,
        ))
        if record_index % 50 == 0 or record_index == len(subtitle_records):
            emit("subtitle", f"已检查字幕 {record_index}/{len(subtitle_records)}", record_index, len(subtitle_records))

    if hash_videos:
        for index, video in enumerate(videos, 1):
            if cancel_event and cancel_event.is_set():
                cancelled = True
                break
            if video.sha256:
                continue
            emit("hash", f"正在计算视频 SHA-256：{index}/{len(videos)}  {video.relative}", index, len(videos), 0, video.size)
            try:
                last_reported = [0]

                def report_chunk(processed, video=video, index=index):
                    if processed - last_reported[0] >= 64 * 1024 * 1024 or processed == video.size:
                        last_reported[0] = processed
                        emit("hash", f"正在计算 SHA-256：{video.relative}", index, len(videos), processed, video.size)

                video.sha256 = sha256_file(Path(video.source), cancel_event=cancel_event, on_chunk=report_chunk)
                stat = Path(video.source).stat()
                update_cache_entry(cache, cache_key(Path(video.source), source), stat, sha256=video.sha256)
            except InterruptedError:
                cancelled = True
                break
            except OSError as exc:
                video.sha256 = f"计算失败: {exc}"

    torrent_hash_groups: dict[str, list[Item]] = {}
    subtitle_hash_groups: dict[str, list[Item]] = {}
    subtitle_name_groups: dict[str, list[Item]] = {}
    for item in items:
        if item.kind == "种子" and item.infohash:
            torrent_hash_groups.setdefault(item.infohash, []).append(item)
        elif item.kind == "字幕":
            if item.content_sha256:
                subtitle_hash_groups.setdefault(item.content_sha256, []).append(item)
            subtitle_name_groups.setdefault(Path(item.source).name.casefold(), []).append(item)
    for group_items in torrent_hash_groups.values():
        if len(group_items) > 1:
            for item in group_items:
                item.duplicate_status = "重复种子（Info Hash 相同）"
    for group_items in subtitle_hash_groups.values():
        if len(group_items) > 1:
            for item in group_items:
                item.duplicate_status = "重复字幕（内容相同）"
    for group_items in subtitle_name_groups.values():
        hashes = {item.content_sha256 for item in group_items if item.content_sha256}
        if len(group_items) > 1 and len(hashes) > 1:
            for item in group_items:
                if not item.duplicate_status:
                    item.duplicate_status = "同名字幕但内容不同"

    video_hash_groups: dict[tuple[int, str], list[VideoRecord]] = {}
    video_size_groups: dict[int, list[VideoRecord]] = {}
    for video in videos:
        if video.sha256 and not video.sha256.startswith("计算失败"):
            video_hash_groups.setdefault((video.size, video.sha256), []).append(video)
        video_size_groups.setdefault(video.size, []).append(video)
    for group_videos in video_hash_groups.values():
        if len(group_videos) > 1:
            for video in group_videos:
                video.duplicate_status = "重复视频（SHA-256 相同）"
    for group_videos in video_size_groups.values():
        if len(group_videos) > 1:
            for video in group_videos:
                if not video.duplicate_status:
                    video.duplicate_status = "疑似重复（大小相同，需 SHA-256 确认）"

    if use_cache:
        try:
            save_scan_cache(source, resolved_cache_path, cache)
        except OSError as exc:
            emit("cache", f"无法保存扫描缓存：{exc}")

    items.sort(key=lambda x: (x.kind, x.group.casefold(), x.source.casefold()))
    videos.sort(key=lambda x: x.relative.casefold())
    elapsed = time.monotonic() - started
    emit("done", "扫描已取消，保留已完成结果。" if cancelled else "扫描完成。")
    return ScanAnalysis(items, videos, cancelled, elapsed, cache_hits, scanned_files, scanned_bytes)


def scan(
    source: Path,
    output: Path | None = None,
    progress=None,
    together: bool = True,
    hash_videos: bool = False,
) -> list[Item]:
    """保留原有接口：返回待复制的种子和字幕列表。"""
    return scan_library(source, output, progress, together, hash_videos).items


def write_reports(
    items: list[Item],
    output: Path,
    source: Path,
    videos: list[VideoRecord] | None = None,
    copied_kinds: set[str] | None = None,
) -> None:
    videos = videos or []
    copied_kinds = {"种子", "字幕"} if copied_kinds is None else set(copied_kinds)
    output.mkdir(parents=True, exist_ok=True)
    (output / ".media_asset_collector_output").write_text(
        "This directory was generated by Media Asset Collector.\n", encoding="ascii"
    )
    csv_path = output / "文件清单.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["类型", "本次复制", "扩展名", "大小(字节)", "大小", "所属项目", "源文件", "整理后路径",
                         "匹配状态", "匹配详情", "匹配的本地视频", "Info Hash", "磁力链接",
                         "内容 SHA-256", "重复状态", "备注"])
        for item in items:
            writer.writerow([item.kind, "是" if item.kind in copied_kinds else "否", item.extension,
                             item.size, pretty_size(item.size), item.group,
                             item.source, item.target_relative, item.match_status, item.match_details,
                             item.matched_videos, item.infohash, item.magnet, item.content_sha256,
                             item.duplicate_status, item.note])

    with (output / "视频媒体信息.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["相对路径", "扩展名", "大小(字节)", "大小", "修改时间", "分辨率线索", "编码线索",
                         "SHA-256", "种子匹配状态", "匹配方式", "对应种子", "重复状态"])
        for video in videos:
            writer.writerow([video.relative, video.extension, video.size, pretty_size(video.size), video.modified_at,
                             video.resolution_hint, video.codec_hint, video.sha256, video.match_status,
                             video.match_method, video.matched_torrents, video.duplicate_status])

    duplicate_rows = [
        (item.kind, item.source, item.size, item.content_sha256 or item.infohash, item.duplicate_status)
        for item in items if item.duplicate_status
    ] + [
        ("视频", video.source, video.size, video.sha256, video.duplicate_status)
        for video in videos if video.duplicate_status
    ]
    with (output / "重复项报告.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["类型", "源文件", "大小(字节)", "哈希", "重复状态"])
        writer.writerows(duplicate_rows)

    magnet_lines = [
        f"# {Path(item.target_relative).stem}\n{item.magnet}"
        for item in items if item.kind == "种子" and item.magnet
    ]
    (output / "磁力链接.txt").write_text("\n\n".join(magnet_lines) + ("\n" if magnet_lines else ""), encoding="utf-8")

    counts = Counter(item.kind for item in items)
    extensions = Counter(item.extension for item in items)
    groups = Counter(item.group for item in items)
    total_size = sum(item.size for item in items)
    torrent_statuses = Counter(item.match_status for item in items if item.kind == "种子")
    matched_torrents = torrent_statuses["完全匹配"] + torrent_statuses["可靠匹配"]
    unmatched_torrents = [item for item in items if item.kind == "种子" and item.match_status in {"疑似匹配", "部分匹配", "未匹配", "种子解析失败"}]
    unmatched_videos = [video for video in videos if video.match_status == "无对应种子"]
    orphan_subtitles = [item for item in items if item.kind == "字幕" and item.match_status == "未找到关联视频或种子"]
    data = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(source), "output": str(output), "total_files": len(items),
        "output_selection": {
            "copy_torrents": "种子" in copied_kinds,
            "copy_subtitles": "字幕" in copied_kinds,
            "generate_reports": True,
        },
        "total_bytes": total_size, "by_type": dict(counts), "by_extension": dict(extensions),
        "by_project": dict(groups), "files": [asdict(item) for item in items],
        "videos": [asdict(video) for video in videos],
        "matching": {
            "matched_torrents": matched_torrents,
            "unmatched_or_partial_torrents": len(unmatched_torrents),
            "videos_without_torrent": len(unmatched_videos),
            "subtitles_without_video_or_torrent": len(orphan_subtitles),
            "torrent_statuses": dict(torrent_statuses),
            "duplicate_items": len(duplicate_rows),
        },
    }
    (output / "统计数据.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    ext_rows = "".join(f"<tr><td>{html.escape(k or '(无扩展名)')}</td><td>{v}</td></tr>" for k, v in extensions.most_common())
    group_rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in groups.most_common())
    file_rows = "".join(
        f"<tr><td>{html.escape(i.kind)}</td><td>{'是' if i.kind in copied_kinds else '否'}</td><td>{html.escape(i.group)}</td>"
        f"<td>{html.escape(pretty_size(i.size))}</td><td>{html.escape(i.target_relative)}</td>"
        f"<td>{html.escape(i.match_status)}</td>"
        f"<td title='{html.escape(i.source, quote=True)}'>{html.escape(Path(i.source).name)}</td></tr>" for i in items
    )
    unmatched_torrent_rows = "".join(
        f"<tr><td>{html.escape(Path(i.source).name)}</td><td>{html.escape(i.match_status)}</td>"
        f"<td>{html.escape(i.matched_videos or '-')}</td></tr>" for i in unmatched_torrents
    ) or "<tr><td colspan='3'>无</td></tr>"
    unmatched_video_rows = "".join(
        f"<tr><td>{html.escape(v.relative)}</td><td>{html.escape(pretty_size(v.size))}</td>"
        f"<td>{html.escape(v.sha256 or '未计算')}</td></tr>" for v in unmatched_videos
    ) or "<tr><td colspan='3'>无</td></tr>"
    duplicate_html_rows = "".join(
        f"<tr><td>{html.escape(kind)}</td><td>{html.escape(str(path))}</td>"
        f"<td>{html.escape(pretty_size(size))}</td><td>{html.escape(status)}</td></tr>"
        for kind, path, size, _digest, status in duplicate_rows
    ) or "<tr><td colspan='4'>无</td></tr>"
    status_rows = "".join(
        f"<tr><td>{html.escape(status or '未分类')}</td><td>{count}</td></tr>"
        for status, count in torrent_statuses.most_common()
    )
    copy_summary = "、".join(kind for kind in ("种子", "字幕") if kind in copied_kinds) or "不复制种子或字幕（仅生成报告）"
    report = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>影视资源整理统计</title>
<style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:32px;color:#202124;background:#f6f7f9}}
.cards{{display:flex;gap:16px;flex-wrap:wrap}}.card{{background:white;padding:18px 24px;border-radius:12px;box-shadow:0 2px 10px #0001;min-width:150px}}
.n{{font-size:28px;font-weight:700;color:#3157d5}}table{{border-collapse:collapse;width:100%;background:white;margin:16px 0 28px}}
th,td{{padding:9px 12px;border-bottom:1px solid #e5e7eb;text-align:left}}th{{background:#eef2ff;position:sticky;top:0}}
.scroll{{max-height:560px;overflow:auto}}code{{word-break:break-all}}.warning{{background:#fff4df;border-left:5px solid #d97706;padding:14px 18px;margin:18px 0}}</style></head><body>
<h1>影视资源整理统计</h1><p>生成时间：{html.escape(data['generated_at'])}<br>源目录：<code>{html.escape(str(source))}</code><br>本次复制：{html.escape(copy_summary)}</p>
<div class='warning'><strong>重要：</strong>种子和磁力链接只是下载线索，不是视频备份。未来可能无法下载或速度很慢。</div>
<div class='cards'><div class='card'><div class='n'>{len(items)}</div>文件总数</div>
<div class='card'><div class='n'>{counts.get('种子', 0)}</div>种子</div><div class='card'><div class='n'>{counts.get('字幕', 0)}</div>字幕</div>
<div class='card'><div class='n'>{len(videos)}</div>本地视频</div><div class='card'><div class='n'>{len(unmatched_videos)}</div>无种子视频</div>
<div class='card'><div class='n'>{len(orphan_subtitles)}</div>未关联字幕</div><div class='card'><div class='n'>{len(duplicate_rows)}</div>重复/疑似项</div>
<div class='card'><div class='n'>{pretty_size(total_size)}</div>提取文件大小</div></div>
<h2>种子匹配概览</h2><table><tr><th>状态</th><th>数量</th></tr>{status_rows}</table>
<h2>未完全匹配的种子</h2><table><tr><th>种子</th><th>状态</th><th>已匹配视频</th></tr>{unmatched_torrent_rows}</table>
<h2>没有对应种子的本地视频</h2><table><tr><th>视频</th><th>大小</th><th>SHA-256</th></tr>{unmatched_video_rows}</table>
<h2>重复与疑似重复项</h2><table><tr><th>类型</th><th>源文件</th><th>大小</th><th>状态</th></tr>{duplicate_html_rows}</table>
<h2>按扩展名</h2><table><tr><th>扩展名</th><th>数量</th></tr>{ext_rows}</table>
<h2>按所属项目</h2><table><tr><th>项目</th><th>数量</th></tr>{group_rows}</table>
<h2>文件列表</h2><div class='scroll'><table><tr><th>类型</th><th>本次复制</th><th>项目</th><th>大小</th><th>整理后路径</th><th>匹配状态</th><th>原文件名</th></tr>{file_rows}</table></div>
</body></html>"""
    (output / "统计报告.html").write_text(report, encoding="utf-8")


def collect(
    items: list[Item],
    output: Path,
    source: Path,
    progress=None,
    videos: list[VideoRecord] | None = None,
    copy_torrents: bool = True,
    copy_subtitles: bool = True,
    generate_reports: bool = True,
) -> tuple[int, list[str]]:
    if not (copy_torrents or copy_subtitles or generate_reports):
        raise ValueError("请至少选择一种输出内容")
    output.mkdir(parents=True, exist_ok=True)
    (output / ".media_asset_collector_output").write_text(
        "This directory was generated by Media Asset Collector.\n", encoding="ascii"
    )
    errors: list[str] = []
    copied = 0
    copied_kinds = set()
    if copy_torrents:
        copied_kinds.add("种子")
    if copy_subtitles:
        copied_kinds.add("字幕")
    selected_items = [item for item in items if item.kind in copied_kinds]
    for index, item in enumerate(selected_items, 1):
        src, dst = Path(item.source), output / item.target_relative
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                if src.stat().st_size == dst.stat().st_size and sha256_file(src) == sha256_file(dst):
                    copied += 1
                    continue
                raise FileExistsError("目标已存在且内容不同")
            shutil.copy2(src, dst)
            copied += 1
        except Exception as exc:
            errors.append(f"{src}: {exc}")
        if progress and (index % 25 == 0 or index == len(selected_items)):
            progress(f"正在复制：{index}/{len(selected_items)}")
    if generate_reports:
        if progress:
            progress("正在生成统计报告…")
        write_reports(items, output, source, videos, copied_kinds)
    if errors:
        (output / "错误日志.txt").write_text("\n".join(errors), encoding="utf-8")
    return copied, errors


class Tooltip:
    """轻量级悬停说明，不占用主界面空间。"""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.after_id = None
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self.after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self.after_id is not None:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def _show(self):
        self.after_id = None
        if self.window or not self.widget.winfo_exists():
            return
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.attributes("-topmost", True)
        label = tk.Label(
            window,
            text=self.text,
            justify="left",
            anchor="w",
            wraplength=390,
            background="#fffbea",
            foreground="#202124",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=8,
            font=("Microsoft YaHei UI", 9),
        )
        label.pack()
        window.update_idletasks()
        x = self.widget.winfo_pointerx() + 14
        y = self.widget.winfo_pointery() + 18
        x = min(x, self.widget.winfo_screenwidth() - window.winfo_reqwidth() - 8)
        y = min(y, self.widget.winfo_screenheight() - window.winfo_reqheight() - 8)
        window.wm_geometry(f"+{max(0, x)}+{max(0, y)}")
        self.window = window

    def _hide(self, _event=None):
        self._cancel()
        if self.window is not None:
            self.window.destroy()
            self.window = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"影视种子与字幕整理工具 v{VERSION}")
        self.geometry("1120x760")
        self.minsize(820, 520)
        self.items: list[Item] = []
        self.videos: list[VideoRecord] = []
        self.events: queue.Queue = queue.Queue()
        self.tooltips: list[Tooltip] = []
        self.cancel_event = threading.Event()
        self.scan_running = False
        self.scan_cancelled = False
        # 启动脚本位于工具目录，默认扫描其上一级资源目录。
        if getattr(sys, "frozen", False):
            default_source = Path(sys.executable).resolve().parent
        else:
            default_source = Path(__file__).resolve().parent.parent
        self.source_var = tk.StringVar(value=str(default_source))
        default_output = default_source.parent / f"影视资源提取结果_{datetime.now():%Y%m%d_%H%M%S}"
        self.output_var = tk.StringVar(value=str(default_output))
        self.together_var = tk.BooleanVar(value=True)
        self.hash_videos_var = tk.BooleanVar(value=False)
        self.use_cache_var = tk.BooleanVar(value=True)
        self.copy_torrents_var = tk.BooleanVar(value=True)
        self.copy_subtitles_var = tk.BooleanVar(value=True)
        self.generate_reports_var = tk.BooleanVar(value=True)
        self.filter_var = tk.StringVar(value="全部")
        self.status_var = tk.StringVar(value="请先扫描预览，确认后再复制整理。")
        self._build()
        self.after(100, self._poll)

    def _build(self):
        pad = {"padx": 12, "pady": 6}
        paths = ttk.Frame(self)
        paths.pack(fill=X, pady=(10, 0))
        ttk.Label(paths, text="资源目录").grid(row=0, column=0, sticky="w", **pad)
        self.source_entry = ttk.Entry(paths, textvariable=self.source_var)
        self.source_entry.grid(row=0, column=1, sticky="ew", **pad)
        self.source_button = ttk.Button(paths, text="选择…", command=self._choose_source)
        self.source_button.grid(row=0, column=2, **pad)
        ttk.Label(paths, text="输出目录").grid(row=1, column=0, sticky="w", **pad)
        self.output_entry = ttk.Entry(paths, textvariable=self.output_var)
        self.output_entry.grid(row=1, column=1, sticky="ew", **pad)
        self.output_button = ttk.Button(paths, text="选择…", command=self._choose_output)
        self.output_button.grid(row=1, column=2, **pad)
        paths.columnconfigure(1, weight=1)

        tk.Label(
            self,
            text="重要：种子和磁力链接只是下载线索，不是备份。做种者可能离线，未来不一定能下载回视频。本工具绝不删除原视频。",
            foreground="#a64b00",
            background="#fff4df",
            anchor="w",
            justify="left",
            wraplength=1060,
            padx=12,
            pady=8,
        ).pack(fill=X, padx=12, pady=(3, 5))

        options = ttk.Frame(self)
        options.pack(fill=X, padx=12, pady=(0, 3))
        self.together_check = ttk.Checkbutton(
            options,
            text="种子和字幕放在同一文件夹",
            variable=self.together_var,
            command=self._scan_options_changed,
        )
        self.together_check.pack(side=LEFT)
        self.hash_check = ttk.Checkbutton(
            options,
            text="生成视频 SHA-256",
            variable=self.hash_videos_var,
            command=self._scan_options_changed,
        )
        self.hash_check.pack(side=LEFT, padx=22)
        self.cache_check = ttk.Checkbutton(
            options,
            text="使用扫描缓存",
            variable=self.use_cache_var,
            command=self._scan_options_changed,
        )
        self.cache_check.pack(side=LEFT)
        self.clear_cache_button = ttk.Button(options, text="清除当前目录缓存", command=self._clear_cache)
        self.clear_cache_button.pack(side=RIGHT)

        output_options = ttk.Frame(self)
        output_options.pack(fill=X, padx=12, pady=(1, 2))
        ttk.Label(output_options, text="输出内容：").pack(side=LEFT)
        self.copy_torrents_check = ttk.Checkbutton(
            output_options, text="复制种子", variable=self.copy_torrents_var, command=self._output_options_changed
        )
        self.copy_torrents_check.pack(side=LEFT, padx=(0, 18))
        self.copy_subtitles_check = ttk.Checkbutton(
            output_options, text="复制字幕", variable=self.copy_subtitles_var, command=self._output_options_changed
        )
        self.copy_subtitles_check.pack(side=LEFT, padx=(0, 18))
        self.generate_reports_check = ttk.Checkbutton(
            output_options, text="生成报告", variable=self.generate_reports_var, command=self._output_options_changed
        )
        self.generate_reports_check.pack(side=LEFT)

        actions = ttk.Frame(self)
        actions.pack(fill=X, padx=12, pady=6)
        self.scan_button = ttk.Button(actions, text="1. 扫描预览", command=self._start_scan)
        self.scan_button.pack(side=LEFT, padx=(0, 8))
        self.collect_button = ttk.Button(actions, text="2. 执行所选输出", command=self._start_collect, state="disabled")
        self.collect_button.pack(side=LEFT)
        self.cancel_button = ttk.Button(actions, text="停止扫描", command=self._cancel_scan, state="disabled")
        self.cancel_button.pack(side=LEFT, padx=8)
        ttk.Label(actions, text="只读源目录：不移动、不改名、不删除", foreground="#287a3f").pack(side=RIGHT)

        ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken").pack(fill=X, side="bottom")
        progress_frame = ttk.Frame(self)
        progress_frame.pack(fill=X, padx=12, pady=(0, 4))
        self.progress_bar = ttk.Progressbar(progress_frame, maximum=100)
        self.progress_bar.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(progress_frame, text="查看：").pack(side=LEFT, padx=(12, 4))
        filters = ("全部", "仅异常", "种子", "字幕", "未匹配/疑似", "无对应种子的视频", "重复项")
        self.filter_box = ttk.Combobox(
            progress_frame, textvariable=self.filter_var, values=filters, state="readonly", width=19
        )
        self.filter_box.pack(side=LEFT)
        self.filter_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_trees())

        notebook = ttk.Notebook(self)
        notebook.pack(fill=BOTH, expand=True, padx=12, pady=(0, 10))
        files_page = ttk.Frame(notebook)
        videos_page = ttk.Frame(notebook)
        notebook.add(files_page, text="种子与字幕")
        notebook.add(videos_page, text="本地视频健康检查")

        columns = ("kind", "group", "size", "match", "duplicate", "source", "target")
        self.tree = ttk.Treeview(files_page, columns=columns, show="headings")
        for key, title, width in (("kind", "类型", 55), ("group", "所属项目", 150), ("size", "大小", 75),
                                  ("match", "匹配状态", 165), ("duplicate", "重复检查", 170),
                                  ("source", "原文件", 245), ("target", "整理后路径", 245)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=45)
        file_ybar = ttk.Scrollbar(files_page, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=file_ybar.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        file_ybar.pack(side=RIGHT, fill="y")

        video_columns = ("match", "method", "duplicate", "size", "source", "sha256")
        self.video_tree = ttk.Treeview(videos_page, columns=video_columns, show="headings")
        for key, title, width in (("match", "种子匹配", 130), ("method", "匹配依据", 125),
                                  ("duplicate", "重复检查", 195), ("size", "大小", 90),
                                  ("source", "视频文件", 390), ("sha256", "SHA-256", 210)):
            self.video_tree.heading(key, text=title)
            self.video_tree.column(key, width=width, minwidth=55)
        video_ybar = ttk.Scrollbar(videos_page, orient="vertical", command=self.video_tree.yview)
        self.video_tree.configure(yscrollcommand=video_ybar.set)
        self.video_tree.pack(side=LEFT, fill=BOTH, expand=True)
        video_ybar.pack(side=RIGHT, fill="y")
        self._add_tooltips()

    def _tip(self, widget: tk.Widget, text: str):
        self.tooltips.append(Tooltip(widget, text))

    def _add_tooltips(self):
        source_tip = (
            "要检查的影视资源总目录。程序会递归读取其中的种子、外挂字幕和视频信息，"
            "但不会移动、改名、修改或删除任何源文件。目录较大时首次扫描会稍久。"
        )
        output_tip = (
            "种子、字幕和统计报告将复制到这里。本地视频不会被复制。建议选择资源目录之外的独立目录；"
            "若目标中已有同名但内容不同的文件，程序会报告错误而不会覆盖。"
        )
        self._tip(self.source_entry, source_tip)
        self._tip(self.source_button, source_tip)
        self._tip(self.output_entry, output_tip)
        self._tip(self.output_button, output_tip)
        self._tip(
            self.together_check,
            "只影响输出目录的排布，不影响扫描和匹配。勾选后种子与字幕统一放进“提取文件”目录，"
            "适合希望得到一个便于备份、移动的小型资源包的用户；取消后会分成“种子”和“字幕”两类目录。",
        )
        self._tip(
            self.hash_check,
            "SHA-256 会逐字节读取每一个完整视频，因此速度取决于视频总容量和硬盘速度，机械硬盘或大型媒体库可能需要数小时。"
            "通常不建议日常扫描勾选。只有需要确认两个视频内容是否完全相同、排查重复文件，或制作长期完整性清单时才建议开启。"
            "它不会修改视频，但会产生持续磁盘读取。",
        )
        self._tip(
            self.cache_check,
            "缓存会记录文件路径、大小、修改时间和已经算出的哈希。下次扫描时，未变化的字幕和视频可以跳过重复计算，"
            "通常建议保持开启，媒体库越大收益越明显。缓存不包含影视内容，保存在当前 Windows 用户目录中；"
            "如果怀疑文件被修改但修改时间未变化，可清除缓存后重新扫描。",
        )
        self._tip(
            self.clear_cache_button,
            "删除当前资源目录对应的扫描缓存。不会删除种子、字幕、视频或已整理结果。清除后下一次扫描会重新计算相关哈希。",
        )
        self._tip(
            self.copy_torrents_check,
            "将扫描到的种子文件复制到输出目录。关闭后仍会分析种子并用于匹配检查，只是不复制种子文件。",
        )
        self._tip(
            self.copy_subtitles_check,
            "将扫描到的外挂字幕复制到输出目录。关闭后仍会检查字幕关联和重复情况，只是不复制字幕文件。",
        )
        self._tip(
            self.generate_reports_check,
            "生成文件清单、视频信息、重复项、磁力链接、HTML 和 JSON 报告。可以只勾选这一项，从而只输出报告而不复制种子或字幕。",
        )
        self._tip(
            self.scan_button,
            "只读检查资源目录并生成预览，不会复制任何文件。建议先查看异常、疑似匹配和重复提示，确认无误后再执行整理。",
        )
        self._tip(
            self.collect_button,
            "按照“输出内容”中的勾选执行复制或报告生成。不会复制或删除视频，也不会改动源目录。",
        )
        self._tip(
            self.cancel_button,
            "请求停止正在进行的目录扫描或视频哈希。已完成的部分会保留供查看，但为了避免清单不完整，停止后不能直接执行整理。",
        )
        self._tip(
            self.filter_box,
            "只改变当前表格显示的内容，不会重新扫描或删除结果。“仅异常”适合集中检查未匹配、疑似匹配、孤立字幕和重复提示。",
        )

    def _choose_source(self):
        value = filedialog.askdirectory(title="选择影视资源目录", initialdir=self.source_var.get())
        if value:
            self.source_var.set(value)
            self.items = []
            self.videos = []
            self._refresh_trees()
            self.collect_button.configure(state="disabled")

    def _choose_output(self):
        value = filedialog.askdirectory(title="选择输出目录", initialdir=str(Path(self.output_var.get()).parent), mustexist=False)
        if value:
            self.output_var.set(value)

    def _set_busy(self, busy: bool, scanning: bool = False):
        self.scan_running = busy and scanning
        for control in (
            self.source_entry, self.source_button, self.output_entry, self.output_button,
            self.together_check, self.hash_check, self.cache_check, self.clear_cache_button,
            self.copy_torrents_check, self.copy_subtitles_check, self.generate_reports_check,
        ):
            control.configure(state="disabled" if busy else "normal")
        self.scan_button.configure(state="disabled" if busy else "normal")
        can_collect = bool(self.items) and not self.scan_cancelled and self._has_output_selection()
        self.collect_button.configure(state="disabled" if busy or not can_collect else "normal")
        self.cancel_button.configure(state="normal" if self.scan_running else "disabled")

    def _scan_options_changed(self):
        self.items = []
        self.videos = []
        self.scan_cancelled = False
        self._refresh_trees()
        self.collect_button.configure(state="disabled")
        mode = "同一文件夹" if self.together_var.get() else "种子/字幕分类"
        hash_mode = "计算视频 SHA-256" if self.hash_videos_var.get() else "不计算视频 SHA-256"
        cache_mode = "使用缓存" if self.use_cache_var.get() else "不使用缓存"
        self.status_var.set(f"已切换为“{mode}、{hash_mode}、{cache_mode}”，请重新扫描预览。")

    def _has_output_selection(self) -> bool:
        return self.copy_torrents_var.get() or self.copy_subtitles_var.get() or self.generate_reports_var.get()

    def _output_options_changed(self):
        selected = []
        if self.copy_torrents_var.get():
            selected.append("种子")
        if self.copy_subtitles_var.get():
            selected.append("字幕")
        if self.generate_reports_var.get():
            selected.append("报告")
        self.collect_button.configure(
            state="normal" if self.items and not self.scan_cancelled and selected else "disabled"
        )
        self.status_var.set(
            f"本次输出：{'、'.join(selected)}。" if selected else "请至少选择一种输出内容。"
        )

    def _clear_cache(self):
        source = Path(self.source_var.get())
        if not source.is_dir():
            messagebox.showerror("目录无效", "请先选择存在的资源目录。")
            return
        removed = clear_scan_cache(source)
        messagebox.showinfo("扫描缓存", "已清除当前目录缓存。" if removed else "当前目录没有扫描缓存。")

    def _cancel_scan(self):
        if self.scan_running:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("正在停止扫描，请稍候…")

    @staticmethod
    def _item_is_abnormal(item: Item) -> bool:
        return bool(item.duplicate_status) or item.match_status in {
            "疑似匹配", "部分匹配", "未匹配", "种子解析失败",
            "项目内有种子（未关联视频）", "未找到关联视频或种子",
        }

    def _show_item(self, item: Item) -> bool:
        selected = self.filter_var.get()
        if selected == "全部":
            return True
        if selected == "仅异常":
            return self._item_is_abnormal(item)
        if selected in {"种子", "字幕"}:
            return item.kind == selected
        if selected == "未匹配/疑似":
            return item.match_status in {"疑似匹配", "部分匹配", "未匹配", "种子解析失败"}
        if selected == "重复项":
            return bool(item.duplicate_status)
        return False

    def _show_video(self, video: VideoRecord) -> bool:
        selected = self.filter_var.get()
        if selected == "全部":
            return True
        if selected == "仅异常":
            return video.match_status in {"疑似匹配", "无对应种子"} or bool(video.duplicate_status)
        if selected == "未匹配/疑似":
            return video.match_status in {"疑似匹配", "无对应种子"}
        if selected == "无对应种子的视频":
            return video.match_status == "无对应种子"
        if selected == "重复项":
            return bool(video.duplicate_status)
        return False

    def _refresh_trees(self):
        if not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        self.video_tree.delete(*self.video_tree.get_children())
        for item in self.items:
            if self._show_item(item):
                self.tree.insert("", END, values=(
                    item.kind, item.group, pretty_size(item.size), item.match_status,
                    item.duplicate_status, item.source, item.target_relative,
                ))
        for video in self.videos:
            if self._show_video(video):
                self.video_tree.insert("", END, values=(
                    video.match_status, video.match_method, video.duplicate_status,
                    pretty_size(video.size), video.source, video.sha256,
                ))

    def _start_scan(self):
        source, output = Path(self.source_var.get()), Path(self.output_var.get())
        if not source.is_dir():
            messagebox.showerror("目录无效", "请选择存在的资源目录。")
            return
        if source.resolve() == output.resolve():
            messagebox.showerror("目录无效", "输出目录不能与资源目录相同。")
            return
        self.cancel_event.clear()
        self.scan_cancelled = False
        self.progress_bar.configure(mode="indeterminate", value=0)
        self.progress_bar.start(12)
        self._set_busy(True, scanning=True)
        self.status_var.set("正在只读扫描…")
        threading.Thread(
            target=self._scan_worker,
            args=(source, output, self.together_var.get(), self.hash_videos_var.get(), self.use_cache_var.get()),
            daemon=True,
        ).start()

    def _scan_worker(self, source, output, together, hash_videos, use_cache):
        try:
            analysis = scan_library(
                source,
                output,
                progress=lambda event: self.events.put(("progress", event)),
                together=together,
                hash_videos=hash_videos,
                use_cache=use_cache,
                cancel_event=self.cancel_event,
            )
            self.events.put(("scanned", analysis))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _start_collect(self):
        if not self.items or not self._has_output_selection():
            if not self._has_output_selection():
                messagebox.showwarning("未选择输出", "请至少选择复制种子、复制字幕或生成报告中的一项。")
            return
        output = Path(self.output_var.get())
        source = Path(self.source_var.get())
        torrent_count = sum(item.kind == "种子" for item in self.items) if self.copy_torrents_var.get() else 0
        subtitle_count = sum(item.kind == "字幕" for item in self.items) if self.copy_subtitles_var.get() else 0
        output_lines = []
        if self.copy_torrents_var.get():
            output_lines.append(f"复制种子：{torrent_count} 个")
        if self.copy_subtitles_var.get():
            output_lines.append(f"复制字幕：{subtitle_count} 个")
        if self.generate_reports_var.get():
            output_lines.append("生成完整扫描报告")
        answer = messagebox.askyesno(
            "确认输出",
            "本次将执行：\n" + "\n".join(output_lines) + f"\n\n输出目录：\n{output}\n\n"
            f"扫描报告包含 {len(self.videos)} 个本地视频的信息，但不会复制视频。\n\n"
            "警告：种子不是备份，未来可能无法下载。\n"
            "源文件不会被改动。是否继续？",
        )
        if not answer:
            return
        self.progress_bar.configure(mode="indeterminate", value=0)
        self.progress_bar.start(12)
        self._set_busy(True)
        self.status_var.set("正在执行所选输出…")
        threading.Thread(
            target=self._collect_worker,
            args=(source, output, self.copy_torrents_var.get(), self.copy_subtitles_var.get(), self.generate_reports_var.get()),
            daemon=True,
        ).start()

    def _collect_worker(self, source, output, copy_torrents, copy_subtitles, generate_reports):
        try:
            copied, errors = collect(
                self.items,
                output,
                source,
                lambda msg: self.events.put(("status", msg)),
                self.videos,
                copy_torrents=copy_torrents,
                copy_subtitles=copy_subtitles,
                generate_reports=generate_reports,
            )
            self.events.put(("collected", (copied, errors, output, generate_reports)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "status":
                    self.status_var.set(payload)
                elif event == "progress":
                    progress: ProgressEvent = payload
                    if progress.total > 0:
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate")
                        self.progress_bar["value"] = min(100, progress.current * 100 / progress.total)
                    elif progress.bytes_total > 0:
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate")
                        self.progress_bar["value"] = min(100, progress.bytes_read * 100 / progress.bytes_total)
                    self.status_var.set(f"{progress.message}（{progress.elapsed_seconds:.1f} 秒）")
                elif event == "scanned":
                    self.items = payload.items
                    self.videos = payload.videos
                    self.scan_cancelled = payload.cancelled
                    self._refresh_trees()
                    counts = Counter(i.kind for i in self.items)
                    missing = sum(video.match_status == "无对应种子" for video in self.videos)
                    abnormal = sum(self._item_is_abnormal(item) for item in self.items)
                    duplicate = sum(bool(item.duplicate_status) for item in self.items) + sum(
                        bool(video.duplicate_status) for video in self.videos
                    )
                    prefix = "扫描已停止，保留部分预览" if payload.cancelled else "扫描完成"
                    self.status_var.set(
                        f"{prefix}：种子 {counts.get('种子', 0)}，字幕 {counts.get('字幕', 0)}，视频 {len(self.videos)}；"
                        f"异常 {abnormal}，无种子视频 {missing}，重复提示 {duplicate}；"
                        f"缓存命中 {payload.cache_hits}，耗时 {payload.elapsed_seconds:.1f} 秒。"
                    )
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate", value=0 if payload.cancelled else 100)
                    self._set_busy(False)
                elif event == "collected":
                    copied, errors, output, generated_reports = payload
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate", value=100)
                    self._set_busy(False)
                    self.status_var.set(f"已完成：{copied} 个文件，{len(errors)} 个错误。")
                    report_line = f"\n\n统计报告：\n{output / '统计报告.html'}" if generated_reports else ""
                    messagebox.showinfo("输出完成", f"已复制 {copied} 个文件。\n错误：{len(errors)}{report_line}")
                elif event == "error":
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate", value=0)
                    self._set_busy(False)
                    self.status_var.set("操作失败。")
                    messagebox.showerror("操作失败", payload)
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main() -> int:
    parser = argparse.ArgumentParser(description=f"影视种子与字幕整理工具 v{VERSION}")
    parser.add_argument("--source", type=Path, help="资源目录")
    parser.add_argument("--output", type=Path, help="输出目录")
    parser.add_argument("--preview", action="store_true", help="仅扫描预览，不写入任何文件")
    parser.add_argument("--separate", action="store_true", help="将种子和字幕分类保存（默认保存在同一文件夹）")
    parser.add_argument("--hash-videos", action="store_true", help="计算每个本地视频的 SHA-256（可能非常耗时）")
    parser.add_argument("--no-cache", action="store_true", help="本次扫描不读取或更新扫描缓存")
    parser.add_argument("--clear-cache", action="store_true", help="扫描前清除当前资源目录的缓存")
    parser.add_argument("--no-torrents", action="store_true", help="不复制种子文件")
    parser.add_argument("--no-subtitles", action="store_true", help="不复制字幕文件")
    parser.add_argument("--no-reports", action="store_true", help="不生成统计报告")
    parser.add_argument("--self-test-gui", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test_gui:
        app = App()
        app.withdraw()
        app.update_idletasks()
        app.destroy()
        return 0
    if args.source:
        if not args.source.is_dir():
            parser.error("资源目录不存在")
        if not args.preview and not args.output:
            parser.error("非预览模式必须指定 --output")
        if not args.preview and args.no_torrents and args.no_subtitles and args.no_reports:
            parser.error("请至少保留种子、字幕或报告中的一种输出")
        if args.clear_cache:
            clear_scan_cache(args.source)
        analysis = scan_library(
            args.source,
            args.output,
            together=not args.separate,
            hash_videos=args.hash_videos,
            use_cache=not args.no_cache,
        )
        items = analysis.items
        counts = Counter(i.kind for i in items)
        print(f"种子: {counts.get('种子', 0)}")
        print(f"字幕: {counts.get('字幕', 0)}")
        print(f"本地视频: {len(analysis.videos)}")
        print(f"无对应种子的视频: {sum(v.match_status == '无对应种子' for v in analysis.videos)}")
        print(f"重复提示: {sum(bool(i.duplicate_status) for i in items) + sum(bool(v.duplicate_status) for v in analysis.videos)}")
        print(f"缓存命中: {analysis.cache_hits}")
        print(f"耗时: {analysis.elapsed_seconds:.1f} 秒")
        print(f"合计: {len(items)} ({pretty_size(sum(i.size for i in items))})")
        if not args.preview:
            copied, errors = collect(
                items,
                args.output,
                args.source,
                videos=analysis.videos,
                copy_torrents=not args.no_torrents,
                copy_subtitles=not args.no_subtitles,
                generate_reports=not args.no_reports,
            )
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
        app_path = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
        log_path = app_path.with_name("启动错误.log")
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
