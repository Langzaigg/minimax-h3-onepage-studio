import asyncio
import base64
import json
import mimetypes
import os
import random
import re
import shutil
import subprocess
import threading
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
import websockets

from config import (
    COMFYUI_AUDIO_VAE,
    COMFYUI_BASE_URL,
    COMFYUI_FL2VA_MODEL,
    COMFYUI_REF2VA_MODEL,
    COMFYUI_SPECTRUM_ENABLED,
    COMFYUI_TEXT_ENCODER,
    COMFYUI_TIMEOUT,
    COMFYUI_VIDEO_VAE,
    FFMPEG_PATH,
    VIDEO_DATA_FOLDER,
    VIDEO_MODEL,
    VIDEO_WORKFLOW_FOLDER,
)
from services.llm import CompletionOptions, LLMClient


TASK_ID_RE = re.compile(r"^[a-f0-9]{32}$")
ASSET_NAMES = {"first_frame", "last_frame", "video"}
REFERENCE_ASSET_RE = re.compile(r"^reference_(image|video|audio)_([1-9]\d*)$")
NODE_STAGES = {
    "104": "正在解析提示词与参考帧",
    "136": "正在解析多模态参考素材",
    "150": "正在应用采样加速",
    "14": "正在采样生成",
    "10": "正在解码视频",
    "23": "正在解码音频",
    "91": "正在封装音视频",
    "92": "正在保存生成结果",
}
_write_lock = threading.Lock()
_background_tasks: set[asyncio.Task] = set()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def task_dir(task_id: str) -> Path:
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("无效的视频任务 ID")
    return Path(VIDEO_DATA_FOLDER) / task_id


def metadata_path(task_id: str) -> Path:
    return task_dir(task_id) / "metadata.json"


def _save_metadata(data: dict[str, Any]) -> None:
    path = metadata_path(data["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = now_iso()
    temp = path.with_suffix(".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with _write_lock:
        # Windows 杀软可能瞬时锁定新写入的 .tmp 文件，导致 os.replace 抛 WinError 5，
        # 短退避重试可避免偶发写入失败。
        for attempt in range(5):
            try:
                temp.write_text(payload, encoding="utf-8")
                os.replace(temp, path)
                return
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))


def load_metadata(task_id: str) -> dict[str, Any]:
    return json.loads(metadata_path(task_id).read_text(encoding="utf-8"))


def public_task(data: dict[str, Any]) -> dict[str, Any]:
    ensure_named_video_asset(data)
    result = {key: value for key, value in data.items() if not key.startswith("_")}
    assets: dict[str, str] = {}
    for name in data.get("_assets", {}):
        if name in ASSET_NAMES or REFERENCE_ASSET_RE.fullmatch(name):
            assets[name] = f"/api/video/tasks/{data['id']}/assets/{name}"
    result["assets"] = assets
    video_path = data.get("_assets", {}).get("video")
    result["video_filename"] = Path(video_path).name if video_path else None
    audio_offset = (
        int(data.get("reference_counts", {}).get("videos", 0))
        if data.get("use_video_audio")
        else 0
    )
    references = []
    for item in data.get("_reference_assets", []):
        label_index = item["index"] + audio_offset if item["kind"] == "audio" else item["index"]
        reference = {
            "kind": item["kind"],
            "index": item["index"],
            "label_index": label_index,
            "filename": item["filename"],
            "asset_name": item["asset_name"],
            "url": assets.get(item["asset_name"]),
        }
        if item["kind"] == "video" and data.get("use_video_audio"):
            reference["audio_label_index"] = item["index"]
        references.append(reference)
    result["references"] = references
    return result


def ensure_named_video_asset(data: dict[str, Any]) -> None:
    """Migrate legacy result.mp4 files when an old history item is viewed."""
    video_path = data.get("_assets", {}).get("video")
    if not video_path:
        return
    source = Path(video_path)
    if not source.is_file() or not re.fullmatch(r"result\.(mp4|webm|mov|mkv)", source.name, re.IGNORECASE):
        return
    destination = source.with_name(video_output_filename(data["id"], data, source.suffix))
    if destination.exists():
        data["_assets"]["video"] = str(destination.resolve())
        _save_metadata(data)
        return
    try:
        source.replace(destination)
    except OSError:
        return
    data["_assets"]["video"] = str(destination.resolve())
    _save_metadata(data)


def iter_task_records() -> list[dict[str, Any]]:
    root = Path(VIDEO_DATA_FOLDER)
    rows = []
    if not root.exists():
        return rows
    for path in root.glob("*/metadata.json"):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return rows


def list_tasks(task_ids: list[str], limit: int = 100, include_all: bool = False) -> list[dict[str, Any]]:
    if include_all:
        rows = iter_task_records()[:limit]
        return [public_task(item) for item in rows]
    allowed = {task_id for task_id in task_ids if TASK_ID_RE.fullmatch(task_id)}
    rows = [item for item in iter_task_records() if item.get("id") in allowed]
    return [public_task(item) for item in rows[:limit]]


def create_task_record(
    *, mode: str, prompt: str, duration: float, width: int, height: int,
    resolution_megapixels: float, aspect_ratio: str, seed: int | None, warnings: list[str],
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = uuid.uuid4().hex
    created = now_iso()
    data = {
        "id": task_id,
        "mode": mode,
        "status": "preparing",
        "prompt": prompt,
        "duration": duration,
        "width": width,
        "height": height,
        "resolution_megapixels": resolution_megapixels,
        "aspect_ratio": aspect_ratio,
        "seed": seed if seed is not None and seed >= 0 else random.randint(0, 2**63 - 1),
        "progress": 0,
        "queue_position": None,
        "current_stage": "正在准备工作流",
        "error": None,
        "created_at": created,
        "updated_at": created,
        "completed_at": None,
        "warnings": warnings,
        "_assets": {},
        "_comfy_prompt_id": None,
    }
    data.update(extras or {})
    _save_metadata(data)
    return data


def save_input_asset(task_id: str, name: str, filename: str, content: bytes) -> Path:
    suffix = Path(filename).suffix.lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
        raise ValueError("参考帧仅支持常见图片格式")
    path = task_dir(task_id) / "inputs" / f"{name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    data = load_metadata(task_id)
    data["_assets"][name] = str(path.resolve())
    _save_metadata(data)
    return path


def asset_path(task_id: str, name: str) -> Path:
    if name not in ASSET_NAMES and not REFERENCE_ASSET_RE.fullmatch(name):
        raise ValueError("无效的资源类型")
    value = load_metadata(task_id).get("_assets", {}).get(name)
    if not value:
        raise FileNotFoundError(name)
    path = Path(value).resolve()
    root = task_dir(task_id).resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError(name)
    return path


REFERENCE_EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"},
    "video": {".mp4", ".mov", ".webm", ".mkv", ".avi"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
}


def save_reference_asset(
    task_id: str, kind: str, index: int, filename: str, content: bytes
) -> Path:
    suffix = Path(filename).suffix.lower()
    if kind not in REFERENCE_EXTENSIONS or suffix not in REFERENCE_EXTENSIONS[kind]:
        raise ValueError(f"不支持的参考{kind}格式")
    asset_name = f"reference_{kind}_{index}"
    path = task_dir(task_id) / "references" / f"{asset_name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    data = load_metadata(task_id)
    data["_assets"][asset_name] = str(path.resolve())
    data.setdefault("_reference_assets", []).append(
        {
            "kind": kind,
            "index": index,
            "filename": Path(filename).name,
            "asset_name": asset_name,
            "path": str(path.resolve()),
        }
    )
    _save_metadata(data)
    return path


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _clean_polished_output(raw: str) -> str:
    """Remove model-internal reasoning blocks and code fences from polished prompts."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"^```(?:text)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def polish_prompt(prompt: str, mode: str, duration: float, images: list[Path]) -> str:
    guide_path = Path(VIDEO_WORKFLOW_FOLDER) / "VIDEO_PROMPT_WRITING_GUIDE_base_en.md"
    guide = guide_path.read_text(encoding="utf-8")
    task_name = {
        "text_to_video": "T2VA",
        "first_frame": "I2VA",
        "last_frame": "L2VA",
        "first_last_frame": "FL2VA",
    }[mode]
    system = (
        "You are a professional MiniMax H3 audiovisual prompt editor. Rewrite the user's idea in English "
        f"as a production-ready {task_name} prompt. Follow the supplied guide exactly. Preserve user-provided "
        "dialogue and visible text verbatim. Do not invent sexual, violent, discriminatory, or unsafe content. "
        "Return only the final prompt, with no code fence or commentary.\n\nPROMPT GUIDE:\n" + guide
    )
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Mode: {task_name}\nEffective duration: {duration:.2f} seconds\n"
                f"Reference images: {len(images)} (in first-frame, last-frame order)\n"
                f"User idea:\n{prompt.strip()}"
            ),
        }
    ]
    for path in images:
        content.append({"type": "image_url", "image_url": {"url": _data_url(path)}})
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]
    options = CompletionOptions(model=VIDEO_MODEL, temperature=0.2, max_tokens=5000)
    result = _clean_polished_output(LLMClient().complete_text(messages, options))
    if not result:
        raise RuntimeError("模型未返回润色后的提示词")
    return result


def _probe_media(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        return {"filename": path.name}
    completed = subprocess.run(
        [
            executable,
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if completed.returncode:
        return {"filename": path.name}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"filename": path.name}


def _media_summary(path: Path) -> str:
    info = _probe_media(path)
    duration = info.get("format", {}).get("duration")
    streams = info.get("streams", [])
    parts = [f"filename={path.name}"]
    if duration:
        parts.append(f"duration={float(duration):.2f}s")
    for stream in streams:
        kind = stream.get("codec_type", "stream")
        detail = stream.get("codec_name", "unknown")
        if kind == "video" and stream.get("width"):
            detail += f" {stream['width']}x{stream['height']}"
        if kind == "audio" and stream.get("sample_rate"):
            detail += f" {stream['sample_rate']}Hz {stream.get('channels', '?')}ch"
        parts.append(f"{kind}={detail}")
    return ", ".join(parts)


def _extract_video_frames(video: Path, folder: Path) -> list[Path]:
    executable = shutil.which("ffmpeg")
    if not executable:
        return []
    info = _probe_media(video)
    try:
        duration = float(info.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    times = [0.1, max(0.1, duration / 2), max(0.1, duration - 0.2)] if duration else [0.1]
    frames = []
    for index, timestamp in enumerate(times, start=1):
        output = folder / f"{video.stem}_frame_{index}.jpg"
        completed = subprocess.run(
            [
                executable, "-y", "-ss", f"{timestamp:.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", "scale='min(960,iw)':-2", "-q:v", "3", str(output),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode == 0 and output.is_file():
            frames.append(output)
    return frames


def _audio_spectrogram(audio: Path, folder: Path) -> Path | None:
    executable = shutil.which("ffmpeg")
    if not executable:
        return None
    output = folder / f"{audio.stem}_spectrogram.png"
    completed = subprocess.run(
        [
            executable, "-y", "-i", str(audio), "-lavfi",
            "showspectrumpic=s=960x360:legend=disabled:color=rainbow", str(output),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return output if completed.returncode == 0 and output.is_file() else None


def polish_reference_prompt(
    prompt: str,
    duration: float,
    images: list[Path],
    videos: list[Path],
    audios: list[Path],
    use_video_audio: bool,
) -> str:
    base_guide = (Path(VIDEO_WORKFLOW_FOLDER) / "VIDEO_PROMPT_WRITING_GUIDE_base_en.md").read_text(
        encoding="utf-8"
    )
    ref_guide = (Path(VIDEO_WORKFLOW_FOLDER) / "VIDEO_PROMPT_WRITING_GUIDE_ref_en.md").read_text(
        encoding="utf-8"
    )
    system = (
        "You are a professional MiniMax H3 full-reference audiovisual prompt editor. Rewrite the user's idea "
        "as a production-ready prompt with exactly these six English sections in order: subject_definitions, "
        "summary, retention_analysis, detailed_description, overall_soundscape, non_diegetic_music. Use the "
        "reference labels exactly in the supplied asset order. Representative video frames and audio spectrograms "
        "are analysis aids, not extra numbered reference assets. Never invent dialogue or lyrics that cannot be "
        "verified; write [unclear] when necessary. Return only the final prompt without a code fence.\n\n"
        "BASE VIDEO GUIDE:\n" + base_guide + "\n\nFULL-REFERENCE GUIDE:\n" + ref_guide
    )
    inventory = [
        f"Target duration: {duration:.2f} seconds",
        f"User intent:\n{prompt.strip()}",
        "Reference inventory (labels are fixed):",
    ]
    for index, path in enumerate(images, start=1):
        inventory.append(f"<Picture {index}>: {_media_summary(path)}")
    audio_index = 0
    for index, path in enumerate(videos, start=1):
        inventory.append(f"<Video {index}>: {_media_summary(path)}")
        if use_video_audio:
            audio_index += 1
            inventory.append(
                f"<Audio {audio_index}>: synchronized audio track from <Video {index}>; "
                "decide copy/reference role from the user intent without inventing words."
            )
    for path in audios:
        audio_index += 1
        inventory.append(f"<Audio {audio_index}>: standalone reference audio; {_media_summary(path)}")

    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(inventory)}]
    for index, path in enumerate(images, start=1):
        content.extend(
            [
                {"type": "text", "text": f"Visual content of <Picture {index}>:"},
                {"type": "image_url", "image_url": {"url": _data_url(path)}},
            ]
        )
    with tempfile.TemporaryDirectory(prefix="video_ref_polish_") as temp_name:
        folder = Path(temp_name)
        for index, path in enumerate(videos, start=1):
            for frame_index, frame in enumerate(_extract_video_frames(path, folder), start=1):
                content.extend(
                    [
                        {
                            "type": "text",
                            "text": f"Representative frame {frame_index} from <Video {index}> (not a Picture label):",
                        },
                        {"type": "image_url", "image_url": {"url": _data_url(frame)}},
                    ]
                )
        audio_sources = [*videos, *audios] if use_video_audio else audios
        for index, path in enumerate(audio_sources, start=1):
            spectrum = _audio_spectrogram(path, folder)
            if spectrum:
                content.extend(
                    [
                        {
                            "type": "text",
                            "text": f"Spectrogram of <Audio {index}> (use only as rhythm/energy evidence):",
                        },
                        {"type": "image_url", "image_url": {"url": _data_url(spectrum)}},
                    ]
                )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        result = _clean_polished_output(
            LLMClient().complete_text(
                messages, CompletionOptions(model=VIDEO_MODEL, temperature=0.2, max_tokens=9000)
            )
        )
    if not result:
        raise RuntimeError("模型未返回润色后的全参考提示词")
    return result


def _apply_spectrum(workflow: dict, loader_id: str, consumer_ids: list[str], enabled: bool) -> None:
    """采样加速（Spectrum Apply MiniMax H3）开关。

    启用时保持节点 150 串联在模型加载器与调度器/引导器之间；
    关闭时直接把消费者模型输入回连到加载器，并移除节点，
    使工作流完全不引用 Spectrum 节点，ComfyUI 侧无需安装该依赖。
    """
    if enabled:
        workflow["150"]["inputs"]["enabled"] = True
        return
    workflow.pop("150", None)
    for node_id in consumer_ids:
        workflow[node_id]["inputs"]["model"] = [loader_id, 0]


def _workflow(mode: str, prompt: str, width: int, height: int, duration: float, seed: int) -> dict:
    if mode == "reference_to_video":
        workflow = json.loads(
            (Path(VIDEO_WORKFLOW_FOLDER) / "reference_video_api.json").read_text(encoding="utf-8")
        )
        workflow["119"]["inputs"]["vae_name"] = COMFYUI_VIDEO_VAE
        workflow["120"]["inputs"]["vae_name"] = COMFYUI_AUDIO_VAE
        workflow["127"]["inputs"]["unet_name"] = COMFYUI_REF2VA_MODEL
        workflow["128"]["inputs"]["clip_name"] = COMFYUI_TEXT_ENCODER
        workflow["136"]["inputs"].update(
            {"prompt": prompt, "width": width, "height": height, "length": _frame_length(duration)}
        )
        workflow["129"]["inputs"]["noise_seed"] = seed
        workflow["92"]["inputs"]["filename_prefix"] = f"video/minimax_h3_studio_ref_{uuid.uuid4().hex}"
        _apply_spectrum(workflow, "127", ["124", "126"], COMFYUI_SPECTRUM_ENABLED)
        return workflow
    # All modes share the same graph; unused optional image branches are removed.
    workflow = json.loads((Path(VIDEO_WORKFLOW_FOLDER) / "video_api.json").read_text(encoding="utf-8"))
    workflow["6"]["inputs"]["unet_name"] = COMFYUI_FL2VA_MODEL
    _apply_spectrum(workflow, "6", ["9", "16"], COMFYUI_SPECTRUM_ENABLED)
    workflow["11"]["inputs"]["vae_name"] = COMFYUI_VIDEO_VAE
    workflow["13"]["inputs"]["clip_name"] = COMFYUI_TEXT_ENCODER
    workflow["24"]["inputs"]["vae_name"] = COMFYUI_AUDIO_VAE
    workflow["104"]["inputs"].update(
        {"prompt": prompt, "width": width, "height": height, "length": _frame_length(duration)}
    )
    workflow["15"]["inputs"]["noise_seed"] = seed
    workflow["92"]["inputs"]["filename_prefix"] = f"video/minimax_h3_studio_{uuid.uuid4().hex}"
    for node_id in ("3", "4"):
        workflow[node_id]["inputs"].update(width=width, height=height)
    if mode not in {"first_frame", "first_last_frame"}:
        workflow.pop("1")
        workflow.pop("3")
        workflow["104"]["inputs"].pop("first_frame")
    if mode not in {"last_frame", "first_last_frame"}:
        workflow.pop("2")
        workflow.pop("4")
        workflow["104"]["inputs"].pop("last_frame")
    return workflow


def _frame_length(duration: float) -> int:
    raw = max(5, round(duration * 24))
    return raw + (5 - raw % 17) % 17


class ComfyClient:
    def __init__(self):
        self.base_url = COMFYUI_BASE_URL.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False

    def upload_file(self, path: Path) -> str:
        remote_name = f"minimax_h3_studio_{uuid.uuid4().hex}{path.suffix.lower() or '.png'}"
        with path.open("rb") as stream:
            response = self.session.post(
                f"{self.base_url}/upload/image",
                files={"image": (remote_name, stream)},
                data={"type": "input", "overwrite": "true"},
                timeout=COMFYUI_TIMEOUT,
            )
        response.raise_for_status()
        body = response.json()
        return "/".join(part for part in [body.get("subfolder"), body["name"]] if part)

    def upload_image(self, path: Path) -> str:
        return self.upload_file(path)

    def submit(self, workflow: dict, client_id: str) -> dict:
        response = self.session.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=COMFYUI_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("node_errors"):
            raise RuntimeError("视频工作流校验失败，请联系管理员")
        return body

    def queue(self) -> dict:
        response = self.session.get(f"{self.base_url}/queue", timeout=COMFYUI_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def history(self, prompt_id: str) -> dict:
        response = self.session.get(f"{self.base_url}/history/{prompt_id}", timeout=COMFYUI_TIMEOUT)
        response.raise_for_status()
        return response.json()

    def download(self, descriptor: dict, destination: Path) -> None:
        query = urlencode({key: descriptor.get(key, "") for key in ("filename", "subfolder", "type")})
        with self.session.get(f"{self.base_url}/view?{query}", stream=True, timeout=COMFYUI_TIMEOUT) as response:
            response.raise_for_status()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as target:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        target.write(chunk)


def start_generation(task_id: str) -> None:
    job = asyncio.create_task(_run_generation(task_id))
    _background_tasks.add(job)
    job.add_done_callback(_background_tasks.discard)


def start_polish_and_generation(task_id: str) -> None:
    data = load_metadata(task_id)
    data.update(status="polishing", progress=0, current_stage="正在使用 AI 润色提示词")
    _save_metadata(data)
    job = asyncio.create_task(_polish_then_generate(task_id))
    _background_tasks.add(job)
    job.add_done_callback(_background_tasks.discard)


async def _polish_then_generate(task_id: str) -> None:
    try:
        data = load_metadata(task_id)
        original_prompt = data.get("original_prompt") or data["prompt"]
        if data["mode"] == "reference_to_video":
            grouped = {"image": [], "video": [], "audio": []}
            for item in data.get("_reference_assets", []):
                grouped[item["kind"]].append((int(item["index"]), Path(item["path"])))
            polished = await asyncio.to_thread(
                polish_reference_prompt,
                original_prompt,
                data["duration"],
                [path for _, path in sorted(grouped["image"])],
                [path for _, path in sorted(grouped["video"])],
                [path for _, path in sorted(grouped["audio"])],
                data.get("use_video_audio", True),
            )
        else:
            images = [
                Path(data["_assets"][name])
                for name in ("first_frame", "last_frame")
                if name in data.get("_assets", {})
            ]
            polished = await asyncio.to_thread(
                polish_prompt, original_prompt, data["mode"], data["duration"], images
            )
        data = load_metadata(task_id)
        data.update(
            prompt=polished,
            polished_prompt=polished,
            original_prompt=original_prompt,
            status="preparing",
            current_stage="提示词润色完成，正在准备工作流",
        )
        _save_metadata(data)
        await _run_generation(task_id)
    except Exception as error:
        data = load_metadata(task_id)
        data.update(status="failed", current_stage="提示词润色失败", error=_public_error(error))
        _save_metadata(data)


async def _run_generation(task_id: str) -> None:
    client_id = uuid.uuid4().hex
    client = ComfyClient()
    try:
        data = load_metadata(task_id)
        workflow = _workflow(
            data["mode"], data["prompt"], data["width"], data["height"], data["duration"], data["seed"]
        )
        if data["mode"] == "reference_to_video":
            workflow["136"]["inputs"]["ref_image_size"] = data.get("ref_image_size", "match")
            for item in data.get("_reference_assets", []):
                remote_name = await asyncio.to_thread(client.upload_file, Path(item["path"]))
                index = int(item["index"]) - 1
                kind = item["kind"]
                if kind == "image":
                    node_id = f"ref_image_load_{index}"
                    workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": remote_name}}
                    workflow["136"]["inputs"][f"ref_images.ref_image_{index}"] = [node_id, 0]
                elif kind == "video":
                    load_id = f"ref_video_load_{index}"
                    split_id = f"ref_video_components_{index}"
                    workflow[load_id] = {"class_type": "LoadVideo", "inputs": {"file": remote_name}}
                    workflow[split_id] = {
                        "class_type": "GetVideoComponents",
                        "inputs": {"video": [load_id, 0]},
                    }
                    workflow["136"]["inputs"][f"ref_videos.ref_video_{index}"] = [split_id, 0]
                    if data.get("use_video_audio", True):
                        workflow["136"]["inputs"][f"ref_video_audios.ref_video_audio_{index}"] = [split_id, 1]
                elif kind == "audio":
                    node_id = f"ref_audio_load_{index}"
                    workflow[node_id] = {"class_type": "LoadAudio", "inputs": {"audio": remote_name}}
                    workflow["136"]["inputs"][f"ref_audios.ref_audio_{index}"] = [node_id, 0]
        elif "first_frame" in data["_assets"]:
            first_name = await asyncio.to_thread(client.upload_image, Path(data["_assets"]["first_frame"]))
            workflow["1"]["inputs"]["image"] = first_name
        if data["mode"] != "reference_to_video" and "last_frame" in data["_assets"]:
            last_name = await asyncio.to_thread(client.upload_image, Path(data["_assets"]["last_frame"]))
            workflow["2"]["inputs"]["image"] = last_name

        ws_url = client.base_url.replace("http://", "ws://").replace("https://", "wss://")
        async with websockets.connect(f"{ws_url}/ws?clientId={client_id}", proxy=None, max_size=2**20) as websocket:
            submitted = await asyncio.to_thread(client.submit, workflow, client_id)
            data = load_metadata(task_id)
            data.update(
                status="queued",
                progress=0,
                current_stage="已进入生成队列",
                _comfy_prompt_id=submitted["prompt_id"],
            )
            _save_metadata(data)
            await _watch_websocket(task_id, submitted["prompt_id"], websocket, client)
    except Exception as error:
        data = load_metadata(task_id)
        data.update(status="failed", current_stage="生成失败", error=_public_error(error), queue_position=None)
        _save_metadata(data)


async def _watch_websocket(task_id: str, prompt_id: str, websocket, client: ComfyClient) -> None:
    while True:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=3)
            if not isinstance(raw, str):
                continue
            event = json.loads(raw)
            payload = event.get("data", {})
            if payload.get("prompt_id") not in {None, prompt_id}:
                continue
            data = load_metadata(task_id)
            kind = event.get("type")
            if kind == "execution_start":
                data.update(status="running", progress=max(data["progress"], 2), queue_position=0, current_stage="开始生成")
            elif kind == "executing" and payload.get("node"):
                data.update(status="running", queue_position=0, current_stage=NODE_STAGES.get(str(payload["node"]), "正在准备生成资源"))
            elif kind == "progress" and payload.get("max"):
                value = float(payload.get("value", 0)) / float(payload["max"])
                data.update(status="running", progress=min(98, max(data["progress"], round(value * 95, 1))), current_stage="正在采样生成")
            elif kind == "execution_error":
                raise RuntimeError("视频生成任务执行失败")
            _save_metadata(data)
        except asyncio.TimeoutError:
            await _update_queue_position(task_id, prompt_id, client)

        history = await asyncio.to_thread(client.history, prompt_id)
        if prompt_id in history:
            await _complete_from_history(task_id, history[prompt_id], client)
            return


async def _update_queue_position(task_id: str, prompt_id: str, client: ComfyClient) -> None:
    queue = await asyncio.to_thread(client.queue)
    pending_ids = [_queue_prompt_id(item) for item in queue.get("queue_pending", [])]
    running_ids = [_queue_prompt_id(item) for item in queue.get("queue_running", [])]
    data = load_metadata(task_id)
    if prompt_id in running_ids:
        data.update(status="running", queue_position=0, progress=max(data["progress"], 2), current_stage="正在生成")
    elif prompt_id in pending_ids:
        data.update(status="queued", queue_position=pending_ids.index(prompt_id) + 1, current_stage="等待生成资源")
    _save_metadata(data)


def _queue_prompt_id(item: Any) -> str | None:
    if isinstance(item, (list, tuple)) and len(item) > 1:
        return str(item[1])
    if isinstance(item, dict):
        return item.get("prompt_id")
    return None


def _find_video_descriptor(history_item: dict) -> dict | None:
    extensions = {".mp4", ".webm", ".mov", ".mkv"}
    for node in history_item.get("outputs", {}).values():
        if not isinstance(node, dict):
            continue
        for value in node.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and Path(str(item.get("filename", ""))).suffix.lower() in extensions:
                    return item
    return None


def ensure_video_gif(task_id: str) -> Path:
    """把已完成任务的视频转成 GIF（palette 方式，输出缓存在任务 outputs 目录）。"""
    if not FFMPEG_PATH:
        raise RuntimeError("服务端未配置 ffmpeg，无法转换 GIF")
    data = load_metadata(task_id)
    video = data.get("_assets", {}).get("video")
    if not video or not Path(video).is_file():
        raise FileNotFoundError("video")
    source = Path(video)
    target = source.with_suffix(".gif")
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    completed = subprocess.run(
        [
            FFMPEG_PATH, "-y", "-i", str(source),
            "-vf",
            "fps=12,scale='min(480,iw)':-2:flags=lanczos,split[s0][s1];"
            "[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5",
            "-loop", "0",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if completed.returncode != 0 or not target.is_file():
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise RuntimeError(f"GIF 转换失败：{detail}")
    return target


def video_output_filename(task_id: str, data: dict[str, Any], suffix: str = ".mp4") -> str:
    """Return a stable, filesystem-safe download name for a generated video."""
    created_at = str(data.get("created_at", ""))
    date_label = re.sub(r"[^0-9]", "", created_at)[:8] or datetime.now().strftime("%Y%m%d")
    mode_label = {
        "text_to_video": "text_to_video",
        "first_frame": "first_frame",
        "last_frame": "last_frame",
        "first_last_frame": "first_last_frame",
        "reference_to_video": "reference_to_video",
    }.get(data.get("mode"), "video")
    safe_suffix = suffix.lower() if suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"} else ".mp4"
    return f"AI_Video_{mode_label}_{date_label}_{task_id[:8]}{safe_suffix}"


async def _complete_from_history(task_id: str, history_item: dict, client: ComfyClient) -> None:
    status = history_item.get("status", {})
    if status.get("status_str") == "error" or status.get("completed") is False:
        raise RuntimeError("视频生成任务执行失败")
    descriptor = _find_video_descriptor(history_item)
    if not descriptor:
        raise RuntimeError("工作流已结束，但没有找到视频输出")
    suffix = Path(descriptor["filename"]).suffix.lower() or ".mp4"
    task_data = load_metadata(task_id)
    destination = task_dir(task_id) / "outputs" / video_output_filename(task_id, task_data, suffix)
    await asyncio.to_thread(client.download, descriptor, destination)
    data = load_metadata(task_id)
    data["_assets"]["video"] = str(destination.resolve())
    data.update(
        status="completed",
        progress=100,
        queue_position=None,
        current_stage="生成完成",
        completed_at=now_iso(),
        error=None,
    )
    _save_metadata(data)


async def resume_incomplete_tasks() -> None:
    for data in iter_task_records():
        if data["status"] not in {"polishing", "preparing", "queued", "running"}:
            continue
        prompt_id = data.get("_comfy_prompt_id")
        if not prompt_id:
            data.update(status="failed", current_stage="服务重启前尚未提交", error="任务提交被服务重启中断")
            _save_metadata(data)
            continue
        job = asyncio.create_task(_resume_monitor(data["id"], prompt_id))
        _background_tasks.add(job)
        job.add_done_callback(_background_tasks.discard)


async def _resume_monitor(task_id: str, prompt_id: str) -> None:
    client = ComfyClient()
    try:
        while True:
            history = await asyncio.to_thread(client.history, prompt_id)
            if prompt_id in history:
                await _complete_from_history(task_id, history[prompt_id], client)
                return
            await _update_queue_position(task_id, prompt_id, client)
            await asyncio.sleep(3)
    except Exception as error:
        data = load_metadata(task_id)
        data.update(status="failed", current_stage="恢复监控失败", error=_public_error(error), queue_position=None)
        _save_metadata(data)


def _public_error(error: Exception) -> str:
    if isinstance(error, requests.RequestException):
        return "暂时无法连接视频生成服务，请稍后重试"
    message = str(error).replace(COMFYUI_BASE_URL, "视频生成服务")
    message = re.sub(r"https?://[^\s/]+(?::\d+)?", "远程服务", message)
    return message.replace("ComfyUI", "视频生成服务")[:500]


async def shutdown_background_tasks() -> None:
    for job in tuple(_background_tasks):
        job.cancel()
    if _background_tasks:
        await asyncio.gather(*tuple(_background_tasks), return_exceptions=True)


def sanitized_queue(task_ids: list[str], include_all: bool = False) -> dict[str, Any]:
    queue = ComfyClient().queue()
    task_lookup = {}
    if include_all:
        for data in iter_task_records():
            if data.get("_comfy_prompt_id"):
                task_lookup[data["_comfy_prompt_id"]] = data["id"]
    else:
        allowed = {task_id for task_id in task_ids if TASK_ID_RE.fullmatch(task_id)}
        for data in iter_task_records():
            if data.get("id") not in allowed:
                continue
            if data.get("_comfy_prompt_id"):
                task_lookup[data["_comfy_prompt_id"]] = data["id"]
    rows = []
    for state, key in (("running", "queue_running"), ("pending", "queue_pending")):
        for index, item in enumerate(queue.get(key, []), start=1):
            local_id = task_lookup.get(_queue_prompt_id(item))
            if local_id:
                rows.append({"task_id": local_id, "state": state, "position": 0 if state == "running" else index})
    return {
        "running": sum(1 for row in rows if row["state"] == "running"),
        "pending": sum(1 for row in rows if row["state"] == "pending"),
        "tasks": rows,
    }


def delete_task_record(task_id: str) -> None:
    data = load_metadata(task_id)
    if data.get("status") not in {"completed", "failed"}:
        raise RuntimeError("运行中或排队中的任务不能删除")
    target = task_dir(task_id).resolve()
    root = Path(VIDEO_DATA_FOLDER).resolve()
    if target.parent != root or not target.is_dir():
        raise FileNotFoundError(task_id)
    shutil.rmtree(target)


def cleanup_expired_video_tasks(retention_days: int = 90) -> int:
    cutoff = datetime.now().astimezone() - timedelta(days=retention_days)
    removed = 0
    for data in iter_task_records():
        if data.get("status") not in {"completed", "failed"}:
            continue
        timestamp = data.get("completed_at") or data.get("updated_at") or data.get("created_at")
        try:
            expired = datetime.fromisoformat(timestamp) < cutoff
        except (TypeError, ValueError):
            expired = False
        if expired:
            try:
                delete_task_record(data["id"])
                removed += 1
            except (OSError, ValueError, FileNotFoundError):
                continue
    return removed
