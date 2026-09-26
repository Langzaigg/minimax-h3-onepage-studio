"""Persistent reference/finishing jobs; ComfyUI serializes GPU execution."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import uuid

from config import COMFYUI_BASE_URL, LLM_API_KEY, VIDEO_DATA_FOLDER
from services import image_generation, post_processing

ROOT = Path(VIDEO_DATA_FOLDER) / "studio"
LOCK = threading.RLock()
JOBS: dict[str, asyncio.Task] = {}
LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def directory(task_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise FileNotFoundError("任务不存在")
    return ROOT / task_id


def load(task_id):
    with LOCK:
        return json.loads((directory(task_id) / "metadata.json").read_text(encoding="utf-8"))


def save(data):
    with LOCK:
        folder = directory(data["id"])
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / "metadata.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, folder / "metadata.json")


def update(task_id, **fields):
    with LOCK:
        data = load(task_id)
        data.update(fields, updated_at=now())
        save(data)
        return data


def create(kind, owner, **options):
    if kind not in {"image", "postprocess"}:
        raise ValueError("无效任务类型")
    seed = options.pop("seed", -1)
    data = {"id": uuid.uuid4().hex, "kind": kind, "status": "preparing", "progress": 0,
            "current_stage": "准备任务", "error": None, "created_at": now(), "updated_at": now(),
            "seed": seed if seed >= 0 else secrets.randbelow(2**63), "_owner": owner,
            "_assets": {}, "_inputs": [], "_comfy_prompt_id": None, "_ready": False, **options}
    save(data)
    return data


def authorized(data, access):
    return access["role"] == "admin" or data.get("_owner") == access.get("key_id")


def public(data):
    result = {k: v for k, v in data.items() if not k.startswith("_")}
    result["seed_text"] = str(data["seed"]) if data.get("seed") is not None else ""
    result["assets"] = {}
    result["input_images"] = []
    names = _asset_values(data)
    filenames = data.get("_input_filenames", [])
    for name in names:
        try:
            path = asset(data, name)
        except (OSError, ValueError):
            continue
        url = f"/api/studio/tasks/{data['id']}/assets/{name}"
        result["assets"][name] = url
        if name.startswith("input_image_"):
            index = int(name.removeprefix("input_image_"))
            filename = filenames[index] if index < len(filenames) else path.name
            result["input_images"].append({"url": url, "filename": Path(filename).name})
    if data["kind"] == "image":
        result["expected_input_count"] = len(data.get("_inputs", []))
        result["missing_input_count"] = max(0, result["expected_input_count"] - len(result["input_images"]))
        result["model_label"] = "Qwen Image 2.1"
    else:
        result.setdefault("requested_duration", data.get("duration", 15))
        result.setdefault("target_resolution", "1080p")
        result.setdefault("cadence_stride", 2 if data.get("preset") in {"rtx_loop", "redgraft_loop"} else 1)
        result["model_label"] = post_processing.PRESETS.get(data.get("preset"), {}).get("label", data.get("preset", ""))
        result.setdefault("media_kind", "video")
        result.setdefault("output_kind", "image" if "image" in data.get("_assets", {}) else "video")
    return result


def _asset_values(data):
    values = dict(data.get("_assets", {}))
    values.update({f"input_image_{index}": path for index, path in enumerate(data.get("_inputs", []))})
    source_name = "source_image" if data.get("media_kind") == "image" else "source_video"
    for name, field in ((source_name, "_source"), ("title_image", "_title_image"),
                        ("reference_image", "_reference"), ("overlay_media", "_overlay")):
        if data.get(field):
            values[name] = data[field]
    return values


def asset(data, name):
    value = _asset_values(data).get(name)
    if not value:
        raise FileNotFoundError("资源不存在")
    path = Path(value).resolve()
    folder = directory(data["id"]).resolve()
    if (not folder.is_relative_to(ROOT.resolve()) or folder == ROOT.resolve()
            or not path.is_relative_to(folder) or not path.is_file()):
        raise FileNotFoundError("资源不存在")
    return path


def records():
    for path in ROOT.glob("*/metadata.json"):
        try:
            yield json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue


def list_tasks(access, kind=None, limit=30, offset=0):
    rows = sorted((d for d in records() if authorized(d, access) and (not kind or d["kind"] == kind)),
                  key=lambda d: d["created_at"], reverse=True)
    return {"tasks": [public(d) for d in rows[offset:offset+limit]], "total": len(rows)}


def library(access, kind="all", query="", limit=20, offset=0):
    """Filter the full owned history before sorting and paginating across stages."""
    from services import video_generation

    rows = []
    if kind in {"all", "image", "postprocess"}:
        rows.extend((data, False) for data in records()
                    if authorized(data, access) and (kind == "all" or data["kind"] == kind))
    if kind in {"all", "video"}:
        rows.extend((data, True) for data in video_generation.iter_task_records()
                    if access["role"] == "admin" or video_generation._task_owner(data) == access.get("key_id"))
    needle = query.strip().casefold()
    def matches(row):
        data, is_video = row
        model_label = data.get("hybrid_model", "") if is_video else (
            "Qwen Image 2.1" if data["kind"] == "image" else
            post_processing.PRESETS.get(data.get("preset"), {}).get("label", ""))
        values = [str(data.get(key, "")) for key in (
            "id", "prompt", "original_prompt", "polished_prompt", "model", "model_label",
            "preset", "hybrid_model", "hybrid_preset", "mode")]
        return not needle or needle in "\n".join([*values, model_label]).casefold()
    rows = [row for row in rows if matches(row)]
    def created_order(row):
        try:
            created = datetime.fromisoformat(str(row[0].get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            timestamp = created.timestamp()
        except (ValueError, OSError, OverflowError):
            timestamp = float("-inf")
        return timestamp, row[0].get("id", "")
    rows.sort(key=created_order, reverse=True)
    page = []
    for data, is_video in rows[offset:offset + limit]:
        if is_video:
            # A history read must not rename the original task's legacy outputs.
            entry = video_generation.public_task(data, migrate_assets=False)
            entry["kind"] = "video"
        else:
            entry = public(data)
        page.append(entry)
    return {"tasks": page, "total": len(rows), "limit": limit, "offset": offset}


def safe_error(error):
    message = str(error)
    if LLM_API_KEY:
        message = message.replace(LLM_API_KEY, "[redacted]")
    return message[:2500]


def claim_retry(task_id, *, resume_prompt_id=None):
    """Atomically move a failed, fully prepared task back into the queue."""
    with LOCK:
        data = load(task_id)
        if data["status"] != "failed":
            raise ValueError("仅失败任务可以重试")
        if not data.get("_ready", True):
            raise ValueError("原素材未准备完成，请重新上传并创建任务")
        current = JOBS.get(task_id)
        if current is not None and not current.done():
            raise ValueError("上一次任务正在结束，请稍后重试")
        return update(task_id, status="queued", progress=0, error=None,
                      current_stage="等待重试", _comfy_prompt_id=resume_prompt_id)


def _run(task_id):
    data = load(task_id)
    folder = directory(task_id)
    def progress(percent, stage):
        update(task_id, status="running", progress=max(0, min(99, float(percent))), current_stage=stage)
    def submitted(prompt_id):
        update(task_id, _comfy_prompt_id=prompt_id)
    try:
        if data["kind"] == "image":
            result = image_generation.run_image_generation(
                data["prompt"], folder, images=[Path(p) for p in data["_inputs"]],
                preset=data["preset"], seed=data["seed"], polish_prompt_first=data["polish_prompt_first"],
                progress_callback=progress, on_submitted=submitted, resume_prompt_id=data.get("_comfy_prompt_id"))
            update(task_id, width=result["width"], height=result["height"], prompt=result["prompt"],
                   polished_prompt=result.get("polished_prompt"), _assets={"image": result["image_path"]})
        else:
            output_kind = data.get("output_kind", "video")
            finished = folder / ("finished.png" if output_kind == "image" else "finished.mp4")
            result = post_processing.run_postprocess(
                data["_source"], finished, preset=data["preset"], prompt=data["prompt"],
                loop_seconds=data.get("requested_duration", data["duration"]) if data["loop"] else None, fps=data["target_fps"],
                interpolation=data.get("interpolation", "none"),
                media_kind=data.get("media_kind", "video"), preview=data.get("preview", False),
                preview_frame=data.get("preview_frame", 0), preview_time=data.get("preview_time"),
                filter_options=data.get("filter_options"),
                progress_callback=progress, on_submitted=submitted, comfy_url=COMFYUI_BASE_URL,
                reference_image=data.get("_reference"), seed=data["seed"], crossfade=data["crossfade"],
                cadence_stride=data.get("cadence_stride", 2 if data.get("preset") in {"rtx_loop", "redgraft_loop"} else 1),
                target_resolution=data.get("target_resolution", "1080p"))
            if data.get("_overlay"):
                from services import media_overlay
                preview_time = (result.get("preview_timestamp", data.get("preview_timestamp"))
                                if data.get("preview") and data.get("media_kind") == "video" else None)
                composed = folder / ("composited" + finished.suffix)
                overlay_result = media_overlay.apply_overlay(finished, Path(data["_overlay"]), composed,
                    **data.get("overlay_options", {}), preview_time=preview_time, media_kind=output_kind)
                finished = composed
                result.update(path=str(finished), overlay=overlay_result)
            elif data.get("_title_image") and output_kind == "video":
                from services import title_overlay
                title_result = title_overlay.apply_title(
                    finished, Path(data["_title_image"]), fade=data.get("title_fade", False))
                update(task_id, _title_result=title_result)
            assets = {output_kind: str(finished.resolve())}
            if result.get("preview_source"):
                assets["preview_source"] = result["preview_source"]
            update(task_id, width=result["width"], height=result["height"], duration=result["duration"],
                   resampling=result["resampling"], segments=result["segments"],
                   fps=result.get("fps"), frames=result.get("frames"), output_kind=output_kind,
                   _assets=assets, _result=result)
        update(task_id, status="completed", progress=100, error=None, current_stage="已完成", completed_at=now())
    except Exception as error:
        LOG.error("Studio task %s failed: %s", task_id, safe_error(error))
        update(task_id, status="failed", error=safe_error(error), current_stage="任务失败，可重试")


def start(task_id):
    with LOCK:
        if task_id in JOBS and not JOBS[task_id].done():
            return
        job = asyncio.create_task(asyncio.to_thread(_run, task_id))
        JOBS[task_id] = job
    def forget(finished):
        with LOCK:
            # A finished attempt's callback can run after a retry is started.
            # It must not remove that newer worker from duplicate protection.
            if JOBS.get(task_id) is finished:
                JOBS.pop(task_id, None)
    job.add_done_callback(forget)


async def resume():
    for data in records():
        if data["status"] not in {"preparing", "queued", "running"}:
            continue
        if data["kind"] == "image" and data.get("_comfy_prompt_id"):
            start(data["id"])
        else:
            update(data["id"], status="failed", error="服务曾中断，请重试；已完成的高清分段会复用。", current_stage="任务中断")


async def shutdown():
    # Threads must finish their receipt writes before a graceful process shutdown.
    if JOBS:
        await asyncio.gather(*list(JOBS.values()), return_exceptions=True)
