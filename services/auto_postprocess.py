"""Durable, idempotent H3 -> MSR handoff owned by the server, not the browser."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import shutil
import uuid

import av

from services import studio_tasks as tasks, video_generation as video
from services.delivery_profiles import target_size
from services.post_processing import probe_video

LOG = logging.getLogger(__name__)
NAMESPACE = uuid.UUID("8d71bdc5-a1be-570c-9d6a-739c6358cfb8")


def child_task_id(parent_id: str) -> str:
    video.task_dir(parent_id)  # Validate the identifier before deriving any paths.
    return uuid.uuid5(NAMESPACE, "h3-auto-postprocess:" + parent_id).hex


def _parent_fields(parent_id, **fields):
    parent = video.load_metadata(parent_id)
    parent.update(fields)
    video._save_metadata(parent)


def _sync_child_status(parent_id, child_id):
    """Refresh the parent summary after a worker finishes; never change H3 status."""
    try:
        child = tasks.load(child_id)
        parent = video.load_metadata(parent_id)
        if child.get("_owner") != video._task_owner(parent) or child.get("auto_postprocess_parent_id") != parent_id:
            return
        error = child.get("error") if child.get("status") == "failed" else None
        _parent_fields(parent_id, auto_postprocess_task_id=child_id, auto_postprocess_error=error)
    except (OSError, ValueError, json.JSONDecodeError):
        LOG.exception("Could not refresh automatic finishing status for %s", parent_id)


def linked_error(parent):
    """Read current child status for history, including a later manual retry."""
    child_id = parent.get("auto_postprocess_task_id")
    if not child_id:
        return parent.get("auto_postprocess_error")
    try:
        child = tasks.load(child_id)
        if child.get("_owner") == video._task_owner(parent) and child.get("auto_postprocess_parent_id") == parent["id"]:
            if child.get("_ready"):
                return child.get("error") if child.get("status") == "failed" else None
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return parent.get("auto_postprocess_error")


def _prepare(parent_id):
    # Preparing includes disk copies. The RLock serializes duplicate callbacks
    # through the ready transition, so no process-local "in flight" flag is lost
    # on restart and no random orphan record can be created.
    with tasks.LOCK:
        parent = video.load_metadata(parent_id)
        if not parent.get("auto_postprocess") or parent.get("status") != "completed":
            return None, False
        child_id = child_task_id(parent_id)
        folder = tasks.directory(child_id)
        existing = None
        try:
            existing = tasks.load(child_id)
        except FileNotFoundError:
            pass
        if existing:
            if existing.get("_owner") != video._task_owner(parent) or existing.get("auto_postprocess_parent_id") != parent_id:
                raise ValueError("自动二采子任务归属不一致")
            if existing.get("_ready"):
                _sync_child_status(parent_id, child_id)
                return child_id, False
        # A reported preparation failure is not retried on every history event
        # or service start. A preparation interrupted without a recorded error
        # can be rebuilt in the same deterministic directory.
        if parent.get("auto_postprocess_error"):
            return child_id if existing else None, False
        target = parent.get("target_resolution", "1080p")
        target_size(target)
        seed = parent.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("自动二采需要父任务保存的精确整数种子")
        source = video.asset_path(parent_id, "video")
        source_info = probe_video(source)
        if abs(source_info["fps"] - 24) > .001:
            raise ValueError("自动LTX二采需要原生24fps视频")
        with av.open(str(source)) as container:
            frames = sum(1 for _ in container.decode(video=0))
        duration = frames / source_info["fps"]
        if not frames or duration > 16.25:
            raise ValueError("自动LTX二采仅支持实际16.25秒以内的视频")
        source_info.update(frames=frames, duration=duration)
        now = tasks.now()
        record = {"id": child_id, "kind": "postprocess", "status": "preparing", "progress": 0,
                  "current_stage": "准备自动二采", "error": None,
                  "created_at": existing.get("created_at", now) if existing else now, "updated_at": now,
                  "seed": seed, "_owner": video._task_owner(parent), "_assets": {}, "_inputs": [],
                  "_comfy_prompt_id": None, "_ready": False,
                  "auto_postprocess_parent_id": parent_id, "video_task_id": parent_id,
                  "preset": "redgraft_msr", "pipeline_contract": 2, "prompt": parent["prompt"],
                  "loop": False, "duration": duration, "requested_duration": duration,
                  "target_fps": 24, "interpolation": "none", "crossfade": .5, "cadence_stride": 1,
                  "target_resolution": target, "media_kind": "video", "output_kind": "video",
                  "source_info": source_info, "preview": False, "preview_frame": 0,
                  "filter_options": {"look": "neutral", "strength": .25}, "overlay_enabled": False,
                  "overlay_options": {}, "_overlay": None, "_reference": None}
        tasks.save(record)
        _parent_fields(parent_id, auto_postprocess_task_id=child_id, auto_postprocess_error=None)
        private_source = folder / ("source" + source.suffix.lower())
        shutil.copy2(source, private_source)
        reference = None
        names = ["first_frame", "reference_image_1", "last_frame"]
        names.extend(sorted(name for name in parent.get("_assets", {})
                            if name.startswith("reference_image_") and name not in names))
        for name in names:
            try:
                candidate = video.asset_path(parent_id, name)
            except (OSError, ValueError):
                continue
            reference = folder / ("reference" + candidate.suffix.lower())
            shutil.copy2(candidate, reference)
            break
        tasks.update(child_id, _source=str(private_source.resolve()),
                     _reference=str(reference.resolve()) if reference else None,
                     _ready=True, status="queued", current_stage="等待自动二采")
        return child_id, True


async def ensure_auto_postprocess(parent_id):
    """Call after the H3 completed record is saved. Errors belong to the chain."""
    child_id = None
    try:
        child_id, should_start = await asyncio.to_thread(_prepare, parent_id)
        if should_start:
            # start uses asyncio.create_task, so it must run on this event loop.
            tasks.start(child_id)
            job = tasks.JOBS.get(child_id)
            if job is not None:
                job.add_done_callback(lambda _: _sync_child_status(parent_id, child_id))
        return child_id
    except Exception as error:
        message = tasks.safe_error(error)
        LOG.error("Automatic postprocess for %s failed: %s", parent_id, message)
        try:
            _parent_fields(parent_id, auto_postprocess_error=message)
            candidate = child_id or child_task_id(parent_id)
            if (tasks.directory(candidate) / "metadata.json").exists():
                child, parent = tasks.load(candidate), video.load_metadata(parent_id)
                if child.get("_owner") == video._task_owner(parent) and child.get("auto_postprocess_parent_id") == parent_id:
                    tasks.update(candidate, status="failed", error=message, current_stage="自动二采准备失败")
        except Exception:
            LOG.exception("Could not persist automatic finishing failure for %s", parent_id)
        return None


async def resume_completed_tasks():
    """Run after studio_tasks.resume; ready children remain manual-retry history."""
    for parent in video.iter_task_records():
        if parent.get("status") == "completed" and parent.get("auto_postprocess"):
            await ensure_auto_postprocess(parent["id"])
