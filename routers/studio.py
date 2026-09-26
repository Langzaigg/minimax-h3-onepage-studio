import asyncio
from pathlib import Path
import shutil
import tempfile
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
import requests

from config import COMFYUI_BASE_URL, VIDEO_MODEL
from routers.access import require_video_access
from services import image_generation, post_processing, studio_tasks as tasks, video_generation
from services.delivery_profiles import target_size, resolution_profiles

router = APIRouter(prefix="/api/studio", tags=["创作流程"])
_capability_cache = (0, None)
ARCHIVED_POSTPROCESS_PRESETS = frozenset(post_processing.PRESETS) - post_processing.ACTIVE_PRESETS
ARCHIVED_POSTPROCESS_MESSAGE = "旧方案已归档，请选择LTX原分辨率2×二采或DLSS5原尺寸滤镜"


def _capabilities():
    global _capability_cache
    if time.monotonic() - _capability_cache[0] < 20 and _capability_cache[1]:
        return _capability_cache[1]
    info = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(COMFYUI_BASE_URL.rstrip("/") + "/object_info", timeout=10)
            response.raise_for_status()
            info = response.json()
    except requests.RequestException:
        pass
    image_graph = image_generation.build_image_workflow("probe", 1536, 864, 0, ["probe.png"])
    image_nodes = {node["class_type"] for node in image_graph.values()} | {"EmptyLatentImage"}
    image_missing_nodes = sorted(image_nodes - set(info or {}))
    image_missing_models = []
    for loader, field, name in (
        ("UNETLoader", "unet_name", image_graph["3"]["inputs"]["unet_name"]),
        ("CLIPLoader", "clip_name", image_graph["4"]["inputs"]["clip_name"]),
        ("VAELoader", "vae_name", image_graph["5"]["inputs"]["vae_name"]),
    ):
        schema = (info or {}).get(loader, {}).get("input", {}).get("required", {}).get(field, [])
        choices = schema[0] if schema and isinstance(schema[0], (list, tuple)) else []
        if name not in choices:
            image_missing_models.append(name)
    image_available = bool(info and not image_missing_nodes and not image_missing_models)
    image_description = "Qwen Image 2.1 · 生成及多图编辑"
    if not image_available:
        image_description = "Qwen 环境未就绪：" + "；".join(filter(None, [
            "缺少节点 " + ", ".join(image_missing_nodes) if image_missing_nodes else "",
            "缺少模型 " + ", ".join(image_missing_models) if image_missing_models else "",
        ]))
    result = {"image": {"enabled": image_available, "presets": image_generation.image_presets(),
                         "default_preset": "landscape-hd", "description": image_description,
                         "missing_nodes": image_missing_nodes, "missing_models": image_missing_models},
              "postprocess": post_processing.capabilities(info),
              "resolution_profiles": resolution_profiles()}
    for preset in result["postprocess"]["presets"]:
        if preset["id"] in ARCHIVED_POSTPROCESS_PRESETS:
            preset.update(archived=True, available=False, hide_when_unavailable=True,
                          reason=ARCHIVED_POSTPROCESS_MESSAGE)
    _capability_cache = (time.monotonic(), result)
    return result


@router.get("/capabilities")
async def capabilities(access=Depends(require_video_access)):
    return await asyncio.to_thread(_capabilities)


def _get(task_id, access):
    try:
        data = tasks.load(task_id)
        if not tasks.authorized(data, access):
            raise FileNotFoundError()
        return data
    except (OSError, ValueError):
        raise HTTPException(404, "任务不存在")


async def _upload(upload, destination, limit, extensions, *, validate_image=False):
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in extensions:
        raise HTTPException(400, "不支持的文件格式")
    path = destination.with_suffix(suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with path.open("wb") as output:
            while block := await upload.read(1024 * 1024):
                size += len(block)
                if size > limit:
                    raise HTTPException(413, "文件超过大小限制")
                output.write(block)
        if not size:
            raise HTTPException(400, "上传文件为空")
        if validate_image or extensions == video_generation.REFERENCE_EXTENSIONS["image"]:
            try:
                with Image.open(path) as image:
                    image.verify()
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
                raise HTTPException(400, "无法读取参考图片")
        return path.resolve()
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def _images(images, folder):
    if len(images) > 4:
        raise HTTPException(400, "最多使用 4 张参考图")
    return [await _upload(image, folder / f"reference_{index}", 25*1024*1024,
                          video_generation.REFERENCE_EXTENSIONS["image"])
            for index, image in enumerate(images)]


def _owned_overlay(access, workspace_id, asset_id, studio_id, studio_asset):
    """Resolve server-owned overlay inputs without accepting filesystem paths."""
    if workspace_id:
        from services import workspaces
        try:
            workspace = workspaces.load_owned(workspace_id, access)
            selected = next(item for item in workspace["assets"] if item["id"] == asset_id)
            if selected["kind"] not in {"image", "video"}:
                raise HTTPException(400, "叠加素材须为图片或视频")
            return workspaces.asset_path(workspace_id, asset_id, access)
        except (OSError, ValueError, StopIteration):
            raise HTTPException(404, "叠加素材不存在")
    if studio_id:
        original = _get(studio_id, access)
        if studio_asset not in {"image", "video", "source_image", "source_video"}:
            raise HTTPException(400, "无效叠加资源类型")
        if studio_asset in {"image", "video"} and original["status"] != "completed":
            raise HTTPException(400, "叠加素材尚未完成")
        try:
            return tasks.asset(original, studio_asset)
        except (OSError, ValueError):
            raise HTTPException(404, "叠加素材不存在")
    return None


def _image_options(prompt, preset):
    if not prompt.strip() or len(prompt) > 16000:
        raise HTTPException(400, "请填写 1–16000 字的图片要求")
    if preset not in image_generation.IMAGE_PRESETS:
        raise HTTPException(400, "无效图片画幅")


@router.post("/images", status_code=202)
async def create_image(prompt: str = Form(...), images: list[UploadFile] = File(default=[]),
                       preset: str = Form("landscape-hd"), seed: int = Form(-1),
                       polish_prompt_first: bool = Form(False), access=Depends(require_video_access)):
    _image_options(prompt, preset)
    if len(images) > 4:
        raise HTTPException(400, "最多使用 4 张参考图")
    data = tasks.create("image", access["key_id"], prompt=prompt.strip(), original_prompt=prompt.strip(),
                        preset=preset, seed=seed, polish_prompt_first=polish_prompt_first)
    try:
        inputs = await _images(images, tasks.directory(data["id"]) / "inputs")
        data = tasks.update(data["id"], _inputs=[str(p) for p in inputs],
                            _input_filenames=[Path(upload.filename or path.name).name for upload, path in zip(images, inputs)],
                            status="queued", _ready=True)
        tasks.start(data["id"])
    except Exception:
        tasks.update(data["id"], status="failed", error="参考图片上传失败")
        raise
    return tasks.public(data)


@router.post("/image-prompt-polish")
async def polish_image(prompt: str = Form(...), images: list[UploadFile] = File(default=[]),
                       preset: str = Form("landscape-hd"), access=Depends(require_video_access)):
    _image_options(prompt, preset)
    with tempfile.TemporaryDirectory(prefix="studio-image-polish-") as temporary:
        paths = await _images(images, Path(temporary))
        try:
            result = await asyncio.to_thread(image_generation.polish_image_prompt, prompt, paths, preset)
        except Exception as error:
            raise HTTPException(502, tasks.safe_error(error))
    return {"prompt": result, "model": VIDEO_MODEL}


@router.post("/postprocess", status_code=202)
async def create_postprocess(video_task_id: str = Form(""), video: UploadFile | None = File(None),
                             image: UploadFile | None = File(None),
                             source_studio_task_id: str = Form(""), source_studio_asset: str = Form("source_video"),
                             source_workspace_id: str = Form(""), source_workspace_asset_id: str = Form(""),
                             preset: str = Form(post_processing.DEFAULT_PRESET), prompt: str = Form(""),
                             loop: bool = Form(False), duration: float = Form(15, ge=1, le=60),
                             target_fps: float | None = Form(None), interpolation: str = Form("none"),
                             crossfade: float = Form(.5, ge=.05, le=2),
                             cadence_stride: int = Form(1, ge=1, le=2),
                             target_resolution: str = Form("1080p"),
                             reference_image: UploadFile | None = File(None),
                             title_image: UploadFile | None = File(None), title_fade: bool = Form(False),
                             preview: bool = Form(False), preview_frame: int = Form(0, ge=0),
                             preview_time: float | None = Form(None, ge=0),
                             dlss_look: str = Form("neutral"), dlss_strength: float = Form(.25, ge=0, le=1),
                             dlss_controls_version: int = Form(1), dlss_style: str = Form("default"),
                             dlss_allow_experimental: bool = Form(False),
                             dlss_output_blend: float = Form(1), dlss_output_blend_enabled: bool = Form(True),
                             dlss_local_tone: float = Form(.4), dlss_local_tone_enabled: bool = Form(True),
                             dlss_local_structure: float = Form(.5), dlss_local_structure_enabled: bool = Form(True),
                             dlss_skin_mask: float = Form(.3), dlss_skin_mask_enabled: bool = Form(True),
                             overlay_media: UploadFile | None = File(None),
                             overlay_workspace_id: str = Form(""), overlay_workspace_asset_id: str = Form(""),
                             overlay_studio_task_id: str = Form(""), overlay_studio_asset: str = Form("image"),
                             overlay_x: int | None = Form(None), overlay_y: int | None = Form(None),
                             overlay_width: int = Form(0, ge=0, le=16384), overlay_height: int = Form(0, ge=0, le=16384),
                             overlay_width_percent: float | None = Form(None, gt=0, le=400),
                             overlay_anchor: str = Form("top-left"), overlay_offset_x: int = Form(0), overlay_offset_y: int = Form(0),
                             overlay_opacity: float = Form(1, ge=0, le=1), overlay_start: float = Form(0, ge=0),
                             overlay_end: float | None = Form(None, gt=0), overlay_loop: bool = Form(False),
                             overlay_frame_time: float = Form(0, ge=0),
                             seed: int = Form(-1),
                             access=Depends(require_video_access)):
    if preset in ARCHIVED_POSTPROCESS_PRESETS:
        raise HTTPException(400, ARCHIVED_POSTPROCESS_MESSAGE)
    if preset not in post_processing.ACTIVE_PRESETS or interpolation not in {"none", "2x"}:
        raise HTTPException(400, "无效后处理方案或插帧倍率")
    from services import dlss_filter
    if bool(overlay_workspace_id) != bool(overlay_workspace_asset_id):
        raise HTTPException(400, "请选择完整的工作区叠加素材")
    if sum(map(bool, (overlay_media, overlay_workspace_id, overlay_studio_task_id))) > 1:
        raise HTTPException(400, "请选择一种叠加素材来源")
    overlay_enabled = bool(overlay_media or overlay_workspace_id or overlay_studio_task_id)
    overlay_options = {"x": overlay_x, "y": overlay_y, "width": overlay_width, "height": overlay_height,
        "width_percent": overlay_width_percent, "anchor": overlay_anchor,
        "offset_x": overlay_offset_x, "offset_y": overlay_offset_y, "opacity": overlay_opacity,
        "start": overlay_start, "end": overlay_end, "loop": overlay_loop, "overlay_frame_time": overlay_frame_time}
    if overlay_enabled:
        from services import media_overlay
        try:
            normalized = media_overlay.validate_options(**overlay_options)
            overlay_options = {key: value for key, value in normalized.items() if key not in {"preview_time", "media_kind"}}
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if title_image:
            raise HTTPException(400, "通用画面叠加不能与旧标题叠加同时使用")
        if overlay_width_percent is not None and (overlay_width or overlay_height):
            raise HTTPException(400, "叠加尺寸请在百分比与像素之间选择一种")
        if overlay_end is not None and overlay_end <= overlay_start:
            raise HTTPException(400, "叠加结束时间须晚于开始时间")
        if overlay_anchor not in {"top-left", "top-center", "top-right", "center-left", "center", "center-right", "bottom-left", "bottom-center", "bottom-right"}:
            raise HTTPException(400, "无效叠加位置")
    overlay_source = _owned_overlay(access, overlay_workspace_id, overlay_workspace_asset_id,
                                    overlay_studio_task_id, overlay_studio_asset)
    advanced_controls = {"style": dlss_style, "allow_experimental": dlss_allow_experimental,
        "output_blend": dlss_output_blend, "output_blend_enabled": dlss_output_blend_enabled,
        "local_tone": dlss_local_tone, "local_tone_enabled": dlss_local_tone_enabled,
        "local_structure": dlss_local_structure, "local_structure_enabled": dlss_local_structure_enabled,
        "skin_mask": dlss_skin_mask, "skin_mask_enabled": dlss_skin_mask_enabled}
    try:
        settings = dlss_filter.filter_options(dlss_look, dlss_strength,
            controls_version=dlss_controls_version, **(advanced_controls if dlss_controls_version == 2 else {}))
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error
    if cadence_stride != 1:
        raise HTTPException(400, "当前后处理保留全部原帧，不支持二拍删帧")
    if preset == "dlss_nr" and (interpolation != "none" or loop or title_image or reference_image):
        raise HTTPException(400, "DLSS滤镜保持原帧率和时长，不使用插帧、循环或角色参考")
    if preset == "redgraft_msr" and (image or preview):
        raise HTTPException(400, "LTX二采需要视频；单帧预览仅适用于DLSS滤镜")
    try:
        target_size(target_resolution)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if bool(source_workspace_id) != bool(source_workspace_asset_id):
        raise HTTPException(400, "请选择工作区中的视频素材")
    if sum(map(bool, (video_task_id, source_studio_task_id, source_workspace_id, video, image))) != 1:
        raise HTTPException(400, "请选择一种素材来源：生成历史、工作区或上传图片/视频")
    if source_studio_asset not in {"source_video", "source_image", "video", "image"}:
        raise HTTPException(400, "无效的历史资源类型")
    caps = await asyncio.to_thread(_capabilities)
    choice = next(p for p in caps["postprocess"]["presets"] if p["id"] == preset)
    if not choice["available"]:
        raise HTTPException(409, choice.get("reason", "后处理环境尚未就绪"))
    if preset == "dlss_nr" and dlss_controls_version == 2 and choice.get("controls_contract_version", 1) < 2:
        raise HTTPException(409, "滤镜服务尚未加载新版参数节点")
    source = reference = None
    if video_task_id:
        try:
            original = video_generation.load_metadata(video_task_id)
            if access["role"] != "admin" and video_generation._task_owner(original) != access["key_id"]:
                raise FileNotFoundError()
            if original["status"] != "completed":
                raise HTTPException(400, "原视频还未完成")
            source = video_generation.asset_path(video_task_id, "video")
            for name in ("start_frame", "first_frame", "reference_image_1"):
                try:
                    reference = video_generation.asset_path(video_task_id, name)
                    break
                except (OSError, ValueError):
                    continue
        except (OSError, ValueError):
            raise HTTPException(404, "原视频不存在")
    elif source_studio_task_id:
        original = _get(source_studio_task_id, access)
        if original["kind"] == "image" and preset != "dlss_nr":
            raise HTTPException(400, "LTX二采需要视频素材")
        if original["kind"] not in {"postprocess", "image"}:
            raise HTTPException(400, "所选任务没有可用图片或视频")
        if source_studio_asset in {"video", "image"} and original["status"] != "completed":
            raise HTTPException(400, "所选历史成片还未完成")
        try:
            source = tasks.asset(original, source_studio_asset)
        except (OSError, ValueError):
            raise HTTPException(404, "所选历史视频资源不存在，请重新上传")
        try:
            reference = tasks.asset(original, "reference_image")
        except (OSError, ValueError):
            pass
    elif source_workspace_id:
        from services import workspaces
        try:
            workspace = workspaces.load_owned(source_workspace_id, access)
            selected = next((item for item in workspace["assets"]
                             if item["id"] == source_workspace_asset_id), None)
            if selected is None:
                raise FileNotFoundError()
            if selected["kind"] not in ({"video", "image"} if preset == "dlss_nr" else {"video"}):
                raise HTTPException(400, "当前处理不支持所选素材类型")
            source = workspaces.asset_path(source_workspace_id, source_workspace_asset_id, access)
        except (OSError, ValueError):
            raise HTTPException(404, "工作区视频素材不存在")
    data = tasks.create("postprocess", access["key_id"], prompt=prompt, preset=preset, loop=loop,
                        duration=duration, requested_duration=duration, target_fps=target_fps,
                        interpolation=interpolation, preview=preview, preview_frame=preview_frame,
                        preview_time=preview_time,
                        dlss_look=dlss_look, dlss_strength=dlss_strength, filter_options=settings,
                        dlss_controls_version=dlss_controls_version,
                        **{f"dlss_{key}": value for key, value in advanced_controls.items()},
                        pipeline_contract=2,
                        overlay_enabled=overlay_enabled, overlay_options=overlay_options,
                        overlay_workspace_id=overlay_workspace_id, overlay_workspace_asset_id=overlay_workspace_asset_id,
                        overlay_studio_task_id=overlay_studio_task_id, overlay_studio_asset=overlay_studio_asset,
                        crossfade=crossfade, cadence_stride=cadence_stride, target_resolution=target_resolution,
                        video_task_id=video_task_id, seed=seed,
                        source_studio_task_id=source_studio_task_id, source_studio_asset=source_studio_asset,
                        source_workspace_id=source_workspace_id, source_workspace_asset_id=source_workspace_asset_id,
                        title_enabled=bool(title_image), title_fade=title_fade)
    folder = tasks.directory(data["id"])
    try:
        if source:
            destination = folder / ("source" + source.suffix)
            await asyncio.to_thread(shutil.copy2, source, destination)
        elif image:
            destination = await _upload(image, folder / "source", 25*1024*1024,
                                        dlss_filter.IMAGE_EXTENSIONS, validate_image=True)
        else:
            destination = await _upload(video, folder / "source", 1024**3, video_generation.REFERENCE_EXTENSIONS["video"])
        try:
            media_info = await asyncio.to_thread(dlss_filter.probe_media, destination)
        except Exception as error:
            raise HTTPException(400, "无法读取所选图片或视频素材") from error
        media_kind = media_info["media_kind"]
        if media_kind == "image" and (preset != "dlss_nr" or destination.stat().st_size > 25*1024*1024):
            raise HTTPException(400, "图片滤镜仅支持25MiB以内的静态图片")
        if media_kind == "video" and (media_info["duration"] > 16.25 or media_info["frames"] < 1):
            raise HTTPException(400, "后处理目前支持16秒以内的有效短片")
        if preset == "redgraft_msr" and (media_kind != "video" or abs(media_info["fps"] - 24) > .001):
            raise HTTPException(400, "LTX二采目前需要原生24fps视频")
        if preview_time is None and preview_frame >= media_info["frames"]:
            raise HTTPException(400, "预览帧超出素材范围")
        preview_timestamp = None
        if preview and media_kind == "video":
            if preview_time is not None and preview_time >= media_info["duration"]:
                raise HTTPException(400, "预览时间超出原视频范围")
            try:
                preview_frame, preview_timestamp = await asyncio.to_thread(dlss_filter.preview_position,
                    destination, frame_index=preview_frame, time_seconds=preview_time)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
        effective_fps = media_info["fps"]
        if preset == "redgraft_msr" and interpolation == "2x":
            effective_fps *= 2
        if target_fps is not None and (effective_fps is None or abs(target_fps-effective_fps) > .001):
            raise HTTPException(400, "帧率由原视频和插帧倍率决定，请移除旧的固定帧率设置")
        overlay_path = None
        overlay_info = None
        if overlay_source:
            overlay_path = folder / ("overlay" + overlay_source.suffix)
            await asyncio.to_thread(shutil.copy2, overlay_source, overlay_path)
        elif overlay_media:
            suffix = Path(overlay_media.filename or "").suffix.lower()
            is_image = suffix in dlss_filter.IMAGE_EXTENSIONS
            overlay_path = await _upload(overlay_media, folder / "overlay", 25*1024*1024 if is_image else 1024**3,
                dlss_filter.IMAGE_EXTENSIONS if is_image else video_generation.REFERENCE_EXTENSIONS["video"], validate_image=is_image)
        if overlay_path:
            from services import media_overlay
            try:
                overlay_info = await asyncio.to_thread(media_overlay.probe_media, overlay_path)
                base_size = (media_info["width"], media_info["height"]) if preset == "dlss_nr" else target_size(target_resolution)
                media_overlay.resolve_geometry(base_size, (overlay_info["width"], overlay_info["height"]), **overlay_options)
            except Exception as error:
                raise HTTPException(400, "无法读取叠加素材") from error
            if overlay_info["kind"] == "image" and overlay_path.stat().st_size > 25*1024*1024:
                raise HTTPException(413, "叠加图片超过25MiB")
            if overlay_info["kind"] == "video" and not overlay_loop and overlay_frame_time >= overlay_info["duration"]:
                raise HTTPException(400, "叠加视频取帧时间超出素材范围")
        refcopy = None
        if reference_image:
            refcopy = await _upload(reference_image, folder / "reference", 25*1024*1024,
                                    video_generation.REFERENCE_EXTENSIONS["image"], validate_image=True)
        elif reference and preset == "redgraft_msr":
            refcopy = folder / ("reference" + reference.suffix)
            await asyncio.to_thread(shutil.copy2, reference, refcopy)
        title_path = None
        if title_image:
            title_path = await _upload(title_image, folder / "title", 25*1024*1024,
                                       {".png", ".webp"}, validate_image=True)
        data = tasks.update(data["id"], _source=str(destination.resolve()),
                            media_kind=media_kind, output_kind="image" if media_kind == "image" or preview else "video",
                            source_info=media_info, target_fps=effective_fps,
                            preview_frame=preview_frame, preview_timestamp=preview_timestamp,
                            _overlay=str(overlay_path.resolve()) if overlay_path else None,
                            overlay_media_kind=overlay_info["kind"] if overlay_info else None,
                            _reference=str(refcopy.resolve()) if refcopy else None, status="queued", _ready=True,
                            _title_image=str(title_path) if title_path else None)
        tasks.start(data["id"])
    except Exception:
        tasks.update(data["id"], status="failed", error="原视频准备失败")
        raise
    return tasks.public(data)


@router.get("/library")
async def library(kind: str = Query("all", pattern="^(all|image|video|postprocess)$"),
                  q: str = Query("", max_length=1000), limit: int = Query(20, ge=1, le=100),
                  offset: int = Query(0, ge=0), access=Depends(require_video_access)):
    return await asyncio.to_thread(tasks.library, access, kind, q, limit, offset)


@router.get("/tasks")
async def list_tasks(kind: str | None = Query(None, pattern="^(image|postprocess)$"),
                     limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                     access=Depends(require_video_access)):
    return await asyncio.to_thread(tasks.list_tasks, access, kind, limit, offset)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, access=Depends(require_video_access)):
    return tasks.public(_get(task_id, access))


@router.get("/tasks/{task_id}/assets/{name}")
async def get_asset(task_id: str, name: str, access=Depends(require_video_access)):
    try:
        path = tasks.asset(_get(task_id, access), name)
    except (OSError, ValueError):
        raise HTTPException(404, "资源不存在")
    return FileResponse(path, filename=path.name, headers={"Cache-Control": "private, max-age=3600"})


@router.post("/tasks/{task_id}/retry", status_code=202)
async def retry_task(task_id: str, access=Depends(require_video_access)):
    data = _get(task_id, access)
    if data["kind"] == "postprocess" and data.get("preset") in ARCHIVED_POSTPROCESS_PRESETS:
        raise HTTPException(409, ARCHIVED_POSTPROCESS_MESSAGE)
    if data["status"] != "failed":
        raise HTTPException(409, "仅失败任务可以重试")
    # A worker may time out while ComfyUI still owns its submitted prompt.
    # Reattach images; don't silently queue a duplicate postprocessing segment.
    try:
        prompt_id = await asyncio.to_thread(_retry_prompt_id, data)
        data = tasks.claim_retry(task_id, resume_prompt_id=prompt_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except requests.RequestException as error:
        raise HTTPException(503, "暂时无法确认 ComfyUI 任务状态，请稍后重试") from error
    tasks.start(task_id)
    return tasks.public(data)


def _retry_prompt_id(data):
    prompt_id = data.get("_comfy_prompt_id")
    if not prompt_id:
        return None
    with requests.Session() as session:
        session.trust_env = False
        base = COMFYUI_BASE_URL.rstrip("/")
        response = session.get(f"{base}/history/{prompt_id}", timeout=10)
        response.raise_for_status()
        item = response.json().get(prompt_id)
        if item:
            if data["kind"] == "image" and item.get("status", {}).get("status_str") != "error":
                return prompt_id
            return None
        response = session.get(f"{base}/queue", timeout=10)
        response.raise_for_status()
        queue = response.json()
        pending = any(len(entry) > 1 and entry[1] == prompt_id
                      for kind in ("queue_running", "queue_pending") for entry in queue.get(kind, []))
        if pending:
            if data["kind"] == "image":
                return prompt_id
            raise ValueError("原后处理仍在 ComfyUI 队列中，请等待该任务结束后重试，避免重复采样")
    return None
