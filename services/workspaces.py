"""Owned, versioned media workspaces. Client JSON never names filesystem paths."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import threading
import uuid
import zipfile

import av
from PIL import Image

from config import VIDEO_DATA_FOLDER

ROOT = Path(VIDEO_DATA_FOLDER) / "workspaces"
LOCK = threading.RLock()
MAX_FILE_BYTES = int(os.getenv("WORKSPACE_MAX_FILE_BYTES", 1024**3))
MAX_TOTAL_BYTES = int(os.getenv("WORKSPACE_MAX_TOTAL_BYTES", 10 * 1024**3))
MAX_ASSETS = int(os.getenv("WORKSPACE_MAX_ASSETS", 500))
MAX_REVISIONS = int(os.getenv("WORKSPACE_MAX_REVISIONS", 2000))
MAX_DRAFT_BYTES = 1024**2
MAX_MANIFEST_BYTES = 32 * 1024**2
ID_RE = re.compile(r"[0-9a-f]{32}\Z")
BLOB_RE = re.compile(r"[0-9a-f]{64}\.(?:png|jpg|webp|gif|bmp|tiff|mp4|mov|webm|mkv|avi|mp3|wav|m4a|aac|flac|ogg)\Z")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


class WorkspaceError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise WorkspaceError("工作区 JSON 无效") from error


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise WorkspaceError("工作区名称需为 1–160 个字符")
    return value.strip()


def _draft(value):
    if not isinstance(value, dict) or len(_json(value)) > MAX_DRAFT_BYTES:
        raise WorkspaceError("工作区草稿需为不超过 1 MiB 的 JSON 对象")
    return deepcopy(value)


def _folder(workspace_id):
    if not isinstance(workspace_id, str) or not ID_RE.fullmatch(workspace_id):
        raise FileNotFoundError("工作区不存在")
    root = ROOT.resolve()
    folder = ROOT / workspace_id
    if folder.is_symlink() or not folder.resolve().is_relative_to(root):
        raise FileNotFoundError("工作区不存在")
    return folder


def _inside(folder, relative):
    path = folder / relative
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise FileNotFoundError("素材不存在")
    return path


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(_json(data))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_owned(workspace_id: str, access: dict) -> dict:
    with LOCK:
        path = _inside(_folder(workspace_id), "workspace.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise FileNotFoundError("工作区不存在") from error
        if data.get("id") != workspace_id or not (access.get("role") == "admin" or data.get("_owner") == access.get("key_id")):
            raise FileNotFoundError("工作区不存在")
        return data


def _blob_path(workspace_id, blob):
    if not isinstance(blob, str) or not BLOB_RE.fullmatch(blob):
        raise FileNotFoundError("素材不存在")
    folder = _folder(workspace_id)
    blobs = _inside(folder, "blobs")
    path = _inside(blobs, blob)
    if not path.is_file():
        raise FileNotFoundError("素材不存在")
    return path


def asset_path(workspace_id: str, asset_id: str, access: dict) -> Path:
    with LOCK:
        data = load_owned(workspace_id, access)
        asset = next((item for item in data["assets"] if item["id"] == asset_id), None)
        if not asset:
            raise FileNotFoundError("素材不存在")
        return _blob_path(workspace_id, asset["_blob"]).resolve()


def public(data):
    result = {key: deepcopy(data[key]) for key in ("id", "name", "draft", "revision", "created_at", "updated_at")}
    result["assets"] = []
    for item in data["assets"]:
        asset = {key: deepcopy(value) for key, value in item.items() if not key.startswith("_")}
        asset["url"] = f"/api/studio/workspaces/{data['id']}/assets/{item['id']}"
        result["assets"].append(asset)
    return result


def list_owned(access):
    result = []
    with LOCK:
        for folder in ROOT.glob("*"):
            if not ID_RE.fullmatch(folder.name):
                continue
            try:
                data = load_owned(folder.name, access)
            except FileNotFoundError:
                continue
            item = {key: data[key] for key in ("id", "name", "revision", "created_at", "updated_at")}
            item["asset_count"] = len(data["assets"])
            result.append(item)
    return sorted(result, key=lambda item: (item["updated_at"], item["id"]), reverse=True)


def _checkpoint(data):
    if data["revision"] >= MAX_REVISIONS:
        raise WorkspaceError("工作区版本数量已达上限，请导出后新建工作区", 413)
    data["revision"] += 1
    data["updated_at"] = _now()
    folder = _folder(data["id"])
    snapshot = {key: deepcopy(data[key]) for key in ("name", "assets", "draft")}
    snapshot.update(id=data["revision"], created_at=data["updated_at"])
    _metadata_budget(data, extra_snapshot=snapshot)
    # Only revisions <= the committed head are visible if a write is interrupted.
    _write(_inside(folder, f"revisions/{data['revision']:08d}.json"), snapshot)
    _write(_inside(folder, "workspace.json"), data)
    return public(data)


def _metadata_budget(data, extra_snapshot=None):
    # Keep every locally accepted workspace exportable under the same import
    # limits. Serialized private fields and a small reserve overestimate export.
    count = data["revision"] - (1 if extra_snapshot is not None else 0)
    folder = _folder(data["id"])
    size = len(_json(data)) + 1024 + data["revision"] * 2
    for revision in range(1, count + 1):
        size += _inside(folder, f"revisions/{revision:08d}.json").stat().st_size
    if extra_snapshot is not None:
        size += len(_json(extra_snapshot))
    if size > MAX_MANIFEST_BYTES:
        raise WorkspaceError("工作区版本元数据已达上限，请导出后新建工作区", 413)


def create(name, access):
    with LOCK:
        data = {"id": uuid.uuid4().hex, "name": _name(name), "_owner": access["key_id"],
                "assets": [], "draft": {}, "revision": 0, "created_at": _now(), "updated_at": _now()}
        _folder(data["id"]).mkdir(parents=True)
        return _checkpoint(data)


def rename(workspace_id, name, access):
    with LOCK:
        data = load_owned(workspace_id, access)
        data["name"] = _name(name)
        return _checkpoint(data)


def set_draft(workspace_id, draft, access):
    with LOCK:
        data = load_owned(workspace_id, access)
        data["draft"] = _draft(draft)
        data["updated_at"] = _now()
        _metadata_budget(data)
        _write(_inside(_folder(workspace_id), "workspace.json"), data)
        return public(data)


def save(workspace_id, access, *, draft=None, name=None):
    with LOCK:
        data = load_owned(workspace_id, access)
        if draft is not None:
            data["draft"] = _draft(draft)
        if name is not None:
            data["name"] = _name(name)
        return _checkpoint(data)


def _snapshots(data):
    folder = _folder(data["id"])
    result = []
    for revision in range(1, data["revision"] + 1):
        try:
            snapshot = json.loads(_inside(folder, f"revisions/{revision:08d}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WorkspaceError("工作区版本文件缺失或损坏", 409) from error
        result.append(snapshot)
    return result


def history(workspace_id, access):
    with LOCK:
        data = load_owned(workspace_id, access)
        return [{"id": item["id"], "name": item["name"], "created_at": item["created_at"],
                 "asset_count": len(item["assets"])} for item in reversed(_snapshots(data))]


def restore(workspace_id, revision_id, access):
    with LOCK:
        data = load_owned(workspace_id, access)
        if not 1 <= revision_id <= data["revision"]:
            raise FileNotFoundError("工作区版本不存在")
        snapshot = _snapshots(data)[revision_id - 1]
        for asset in snapshot["assets"]:
            _blob_path(workspace_id, asset["_blob"])
        for key in ("name", "assets", "draft"):
            data[key] = deepcopy(snapshot[key])
        return _checkpoint(data)


def _filename(name):
    name = str(name or "素材").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(char for char in name if ord(char) >= 32 and char not in '<>:"|?*')[:240] or "素材"


def inspect_media(path, name):
    size = path.stat().st_size
    if size <= 0 or size > MAX_FILE_BYTES:
        raise WorkspaceError("素材为空或超过单文件大小限制", 413)
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXT:
        try:
            with Image.open(path) as image:
                if image.width * image.height > 100_000_000:
                    raise WorkspaceError("图片像素数量超过上限", 413)
                result = {"kind": "image", "width": image.width, "height": image.height}
                canonical = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp", "TIFF": ".tiff"}.get(image.format)
                image.verify()
                if not canonical:
                    raise ValueError("Unsupported image format")
        except WorkspaceError:
            raise
        except Exception as error:
            raise WorkspaceError("无法读取图片素材") from error
        return result, canonical
    if suffix not in VIDEO_EXT | AUDIO_EXT:
        raise WorkspaceError("不支持的素材格式")
    try:
        # A bytes/file handle prevents FFmpeg from opening secondary URLs or
        # local paths from disguised playlists. Restrict to actual media demuxers.
        demuxers = {".mp4": "mov", ".mov": "mov", ".m4a": "mov", ".webm": "matroska", ".mkv": "matroska",
                    ".avi": "avi", ".mp3": "mp3", ".wav": "wav", ".aac": "aac", ".flac": "flac", ".ogg": "ogg"}
        with path.open("rb") as source, av.open(source, format=demuxers[suffix], options={"protocol_whitelist": ""}) as container:
            streams = container.streams.video if suffix in VIDEO_EXT else container.streams.audio
            if not streams:
                raise ValueError("Required media stream absent")
            stream = streams[0]
            # Decode a frame instead of trusting an arbitrary file extension/header.
            frame = next(container.decode(stream))
            duration = float(stream.duration * stream.time_base) if stream.duration is not None else float(container.duration or 0) / av.time_base
            result = {"kind": "video" if suffix in VIDEO_EXT else "audio"}
            if math.isfinite(duration) and duration > 0:
                result["duration"] = duration
            if result["kind"] == "video":
                if frame.width * frame.height > 100_000_000:
                    raise ValueError("Video frame too large")
                result.update(width=frame.width, height=frame.height)
    except Exception as error:
        raise WorkspaceError("无法读取视频或音频素材") from error
    return result, suffix


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(1024**2):
            digest.update(block)
    return digest.hexdigest()


def add_files(workspace_id, files, access):
    """files: ordered (path, display_name, source_or_None) tuples; no client paths."""
    if not files or len(files) > MAX_ASSETS:
        raise WorkspaceError("请选择素材，且单次素材数量不能超过上限")
    with LOCK:
        data = load_owned(workspace_id, access)
        folder = _folder(workspace_id)
        blobs = _inside(folder, "blobs")
        blobs.mkdir(exist_ok=True)
        prepared = []
        by_content_name = {(asset["_blob"], asset["name"]): asset for asset in data["assets"]}
        selected = []
        for path, filename, source in files:
            name = _filename(filename)
            media, suffix = inspect_media(path, name)
            blob = _hash(path) + suffix
            existing = by_content_name.get((blob, name))
            if existing:
                # Every input still has a corresponding response ID, even when
                # multiple inputs refer to the same current material item.
                selected.append(existing["id"])
                continue
            asset = {"id": uuid.uuid4().hex, "name": name, "created_at": _now(),
                     "_blob": blob, "size": path.stat().st_size, **media,
                     **({"source": deepcopy(source)} if source else {})}
            prepared.append((path, asset))
            by_content_name[(blob, name)] = asset
            selected.append(asset["id"])
        if len(data["assets"]) + len(prepared) > MAX_ASSETS:
            raise WorkspaceError("工作区素材数量超过上限", 413)
        if not prepared:
            result = public(data)
            result["added_asset_ids"] = selected
            return result
        sizes = {path.name: path.stat().st_size for path in blobs.iterdir() if path.is_file()}
        for path, asset in prepared:
            sizes[asset["_blob"]] = asset["size"]
        if sum(sizes.values()) > MAX_TOTAL_BYTES:
            raise WorkspaceError("工作区全部素材（含历史版本）超过空间限制", 413)
        if len(sizes) > MAX_ASSETS * 10:
            raise WorkspaceError("工作区历史媒体数量已达上限，请导出后新建工作区", 413)
        added = []
        try:
            for path, asset in prepared:
                destination = _inside(blobs, asset["_blob"])
                if not destination.exists():
                    temporary = blobs / (uuid.uuid4().hex + ".tmp")
                    try:
                        shutil.copyfile(path, temporary)
                        if _hash(temporary) != asset["_blob"].split(".", 1)[0]:
                            raise WorkspaceError("复制期间素材已变化，请重试", 409)
                        temporary.replace(destination)
                        added.append(destination)
                    finally:
                        temporary.unlink(missing_ok=True)
            data["assets"].extend(asset for _, asset in prepared)
            result = _checkpoint(data)
        except Exception:
            for path in added:
                path.unlink(missing_ok=True)
            raise
        result["added_asset_ids"] = selected
        return result


def from_task(workspace_id, kind, task_id, asset_name, access):
    # Authorize the destination first, then use the existing task asset resolver.
    with LOCK:
        load_owned(workspace_id, access)
        if kind == "video":
            from services import video_generation as tasks
            data = tasks.load_metadata(task_id)
            if not (access.get("role") == "admin" or tasks._task_owner(data) == access.get("key_id")):
                raise FileNotFoundError("任务不存在")
            path = tasks.asset_path(task_id, asset_name)
        elif kind in {"image", "postprocess"}:
            from services import studio_tasks as tasks
            data = tasks.load(task_id)
            if data.get("kind") != kind or not tasks.authorized(data, access):
                raise FileNotFoundError("任务不存在")
            path = tasks.asset(data, asset_name)
        else:
            raise WorkspaceError("任务类型无效")
        return add_files(workspace_id, [(path, path.name, {"kind": kind, "task_id": task_id, "asset_name": asset_name})], access)


def from_workspace(workspace_id, source_workspace_id, asset_id, access):
    with LOCK:
        load_owned(workspace_id, access)
        source = load_owned(source_workspace_id, access)
        path = asset_path(source_workspace_id, asset_id, access)
        asset = next(item for item in source["assets"] if item["id"] == asset_id)
        return add_files(workspace_id, [(path, asset["name"], {"workspace_id": source_workspace_id, "asset_id": asset_id})], access)


def delete_asset(workspace_id, asset_id, access):
    with LOCK:
        data = load_owned(workspace_id, access)
        retained = [asset for asset in data["assets"] if asset["id"] != asset_id]
        if len(retained) == len(data["assets"]):
            raise FileNotFoundError("素材不存在")
        data["assets"] = retained
        # Blob stays available to every earlier saved revision.
        return _checkpoint(data)


def _remove_folder(folder):
    root = ROOT.resolve()
    resolved = folder.resolve()
    if folder.is_symlink() or resolved.parent != root or resolved == root:
        raise WorkspaceError("无效工作区路径")
    shutil.rmtree(folder)


def delete(workspace_id, access):
    with LOCK:
        load_owned(workspace_id, access)
        _remove_folder(_folder(workspace_id))


def export_zip(workspace_id, access):
    with LOCK:
        data = load_owned(workspace_id, access)
        revisions = _snapshots(data)
        current = {key: deepcopy(data[key]) for key in ("name", "assets", "draft", "created_at", "updated_at", "revision")}
        manifest = {"format": "frame-studio-workspace", "schema_version": 1, "current": current, "revisions": revisions}
        payload = _json(manifest)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise WorkspaceError("工作区版本元数据超过导出上限", 413)
        needed = {asset["_blob"] for snapshot in [current, *revisions] for asset in snapshot["assets"]}
        handle, name = tempfile.mkstemp(prefix="frame-studio-export-", suffix=".zip")
        os.close(handle)
        target = Path(name)
        try:
            # Media is already compressed. STORED avoids costly re-compression.
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                archive.writestr("workspace.json", payload)
                for blob in sorted(needed):
                    archive.write(_blob_path(workspace_id, blob), "blobs/" + blob)
            return target
        except Exception:
            target.unlink(missing_ok=True)
            raise


def _validate_snapshot(raw, *, current=False):
    if not isinstance(raw, dict):
        raise WorkspaceError("ZIP 工作区元数据无效")
    snapshot = {"name": _name(raw.get("name")), "draft": _draft(raw.get("draft", {})), "assets": []}
    assets = raw.get("assets")
    if not isinstance(assets, list) or len(assets) > MAX_ASSETS:
        raise WorkspaceError("ZIP 素材数量无效")
    ids = set()
    for value in assets:
        if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not ID_RE.fullmatch(value["id"]) or value["id"] in ids:
            raise WorkspaceError("ZIP 素材 ID 无效或重复")
        ids.add(value["id"])
        if not isinstance(value.get("_blob"), str) or not BLOB_RE.fullmatch(value["_blob"]):
            raise WorkspaceError("ZIP 素材路径无效")
        item = {"id": value["id"], "name": _filename(value.get("name")), "_blob": value["_blob"],
                "created_at": _valid_time(value.get("created_at"))}
        # Never trust client URLs, source task ownership or arbitrary metadata.
        # Retain a harmless provenance label for cross-install imports instead.
        if isinstance(value.get("source"), dict):
            item["source"] = {"imported": True}
        snapshot["assets"].append(item)
    if current:
        snapshot.update(created_at=_valid_time(raw.get("created_at")), updated_at=_valid_time(raw.get("updated_at")))
    else:
        if type(raw.get("id")) is not int or raw["id"] < 1:
            raise WorkspaceError("ZIP 版本 ID 无效")
        snapshot.update(id=raw["id"], created_at=_valid_time(raw.get("created_at")))
    return snapshot


def _valid_time(value):
    if not isinstance(value, str) or len(value) > 80:
        raise WorkspaceError("ZIP 时间格式无效")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise WorkspaceError("ZIP 时间格式无效") from error
    return value


def _zip_entries(archive):
    entries = {}
    seen = set()
    total = 0
    for info in archive.infolist():
        name = info.filename
        mode = info.external_attr >> 16
        if name.casefold() in seen or info.orig_filename != name:
            raise WorkspaceError("ZIP 存在重复路径")
        seen.add(name.casefold())
        if name != "workspace.json" and not (name.startswith("blobs/") and BLOB_RE.fullmatch(name[6:])):
            raise WorkspaceError("ZIP 包含不允许的路径")
        if info.is_dir() or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or info.flag_bits & 1:
            raise WorkspaceError("ZIP 不允许链接、特殊文件或加密条目")
        limit = MAX_MANIFEST_BYTES if name == "workspace.json" else MAX_FILE_BYTES
        if info.file_size <= 0 or info.file_size > limit or (info.file_size > 1024**2 and info.file_size > max(1, info.compress_size) * 200):
            raise WorkspaceError("ZIP 条目超过大小或压缩比例限制", 413)
        total += info.file_size
        if total > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES or len(entries) >= MAX_ASSETS * 10 + 1:
            raise WorkspaceError("ZIP 总大小或素材数量超过上限", 413)
        entries[name] = info
    if "workspace.json" not in entries:
        raise WorkspaceError("ZIP 缺少 workspace.json")
    if sum(info.file_size for name, info in entries.items() if name != "workspace.json") > MAX_TOTAL_BYTES:
        raise WorkspaceError("ZIP 素材总大小超过上限", 413)
    return entries


def _preflight_directory(path):
    """Bound central-directory parsing before ZipFile allocates all ZipInfo rows."""
    size = path.stat().st_size
    if size > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES + 10 * 1024**2:
        raise WorkspaceError("ZIP 文件超过大小限制", 413)
    with path.open("rb") as source:
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise WorkspaceError("ZIP 目录无效")
        record = struct.unpack("<4s4H2LH", tail[offset:offset + 22])
        _, disk, directory_disk, disk_count, count, length, position, comment = record
        if offset + 22 + comment != len(tail) or disk or directory_disk or disk_count != count:
            raise WorkspaceError("ZIP 不支持分卷或无效目录")
        end_position = max(0, size - 65557) + offset
        if end_position >= 20:
            source.seek(end_position - 20)
            locator = source.read(20)
            if locator.startswith(b"PK\x06\x07"):
                _, zip64_disk, zip64_position, disks = struct.unpack("<4sLQL", locator)
                if zip64_disk or disks != 1 or zip64_position > end_position - 76:
                    raise WorkspaceError("ZIP64 目录无效")
                source.seek(zip64_position)
                data = source.read(56)
                if len(data) != 56 or not data.startswith(b"PK\x06\x06"):
                    raise WorkspaceError("ZIP64 目录无效")
                values = struct.unpack("<4sQ2H2L4Q", data)
                if values[1] != 44 or values[4] or values[5] or values[6] != values[7]:
                    raise WorkspaceError("ZIP64 不支持分卷或扩展目录")
                count, length, position = values[7:10]
                end_position = zip64_position
        if not 1 <= count <= MAX_ASSETS * 10 + 1 or length > MAX_MANIFEST_BYTES:
            raise WorkspaceError("ZIP 目录条目或大小超过上限", 413)
        if position + length > end_position:
            raise WorkspaceError("ZIP 目录边界无效")
        source.seek(position)
        consumed, actual = 0, 0
        while consumed < length:
            header = source.read(46)
            if len(header) != 46 or not header.startswith(b"PK\x01\x02"):
                raise WorkspaceError("ZIP 目录无效")
            variable = sum(struct.unpack("<3H", header[28:34]))
            consumed += 46 + variable
            actual += 1
            if actual > count or consumed > length:
                raise WorkspaceError("ZIP 目录条目与声明不符")
            source.seek(variable, 1)
        if actual != count or consumed != length:
            raise WorkspaceError("ZIP 目录条目与声明不符")


def import_zip(path, access):
    """Extract only an explicit validated list into a new, privately staged folder."""
    with LOCK:
        ROOT.mkdir(parents=True, exist_ok=True)
        staging = ROOT / ("import-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            _preflight_directory(path)
            with zipfile.ZipFile(path) as archive:
                entries = _zip_entries(archive)
                raw = json.loads(archive.read("workspace.json"))
                if not isinstance(raw, dict) or raw.get("format") != "frame-studio-workspace" or raw.get("schema_version") != 1:
                    raise WorkspaceError("不是支持的 Frame Studio 工作区 ZIP")
                current = _validate_snapshot(raw.get("current"), current=True)
                raw_revisions = raw.get("revisions")
                if not isinstance(raw_revisions, list) or not 1 <= len(raw_revisions) <= MAX_REVISIONS:
                    raise WorkspaceError("ZIP 版本数量无效")
                revisions = [_validate_snapshot(item) for item in raw_revisions]
                if [item["id"] for item in revisions] != list(range(1, len(revisions) + 1)):
                    raise WorkspaceError("ZIP 版本序列无效")
                if type(raw["current"].get("revision")) is not int or raw["current"]["revision"] != len(revisions):
                    raise WorkspaceError("ZIP 当前版本与版本清单不一致")
                needed = {asset["_blob"] for item in [current, *revisions] for asset in item["assets"]}
                if set(entries) != {"workspace.json", *("blobs/" + blob for blob in needed)}:
                    raise WorkspaceError("ZIP 素材清单与文件不一致")
                (staging / "blobs").mkdir()
                media = {}
                for blob in sorted(needed):
                    target = staging / "blobs" / blob
                    expected = entries["blobs/" + blob].file_size
                    written = 0
                    digest = hashlib.sha256()
                    with archive.open("blobs/" + blob) as source, target.open("xb") as output:
                        while block := source.read(1024**2):
                            written += len(block)
                            if written > expected or written > MAX_FILE_BYTES:
                                raise WorkspaceError("ZIP 素材大小无效", 413)
                            digest.update(block)
                            output.write(block)
                    if written != expected or digest.hexdigest() != blob.split(".", 1)[0]:
                        raise WorkspaceError("ZIP 素材哈希不匹配")
                    details, suffix = inspect_media(target, blob)
                    if suffix != Path(blob).suffix:
                        raise WorkspaceError("ZIP 素材格式与清单不符")
                    media[blob] = {**details, "size": written}
                for snapshot in [current, *revisions]:
                    for asset in snapshot["assets"]:
                        asset.update(media[asset["_blob"]])
                workspace_id = uuid.uuid4().hex
                current.update(id=workspace_id, _owner=access["key_id"], revision=len(revisions), updated_at=_now())
                for snapshot in revisions:
                    _write(staging / "revisions" / f"{snapshot['id']:08d}.json", snapshot)
                _write(staging / "workspace.json", current)
                staging.replace(_folder(workspace_id))
                return public(current)
        except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, RuntimeError, RecursionError, NotImplementedError) as error:
            raise WorkspaceError("ZIP 文件损坏或格式不受支持") from error
        finally:
            if staging.exists():
                _remove_folder(staging)
