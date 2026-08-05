import os
import tempfile
import math
from io import BytesIO
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from PIL import Image

from config import (
    VIDEO_DEFAULT_DURATION,
    VIDEO_DEFAULT_MEGAPIXELS,
    VIDEO_MAX_DURATION,
    VIDEO_MAX_MEGAPIXELS,
    VIDEO_MIN_DURATION,
    VIDEO_MIN_MEGAPIXELS,
)
from models.video import PromptPolishResponse, VideoQueueStatus, VideoTaskList, VideoTaskSummary
from routers.access import require_video_access
from services import video_generation as video_service


router = APIRouter(prefix="/api/video", dependencies=[Depends(require_video_access)])
TAG_WORKFLOWS = "视频 1｜生成工作流"
TAG_POLISH = "视频 2｜提示词润色"
TAG_TASKS = "视频 3｜任务进度与结果"


def derive_mode(has_first: bool, has_last: bool) -> str:
    if has_first and has_last:
        return "first_last_frame"
    if has_first:
        return "first_frame"
    if has_last:
        return "last_frame"
    return "text_to_video"


ASPECT_RATIOS = {
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "1:1": 1,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "3:2": 3 / 2,
    "2:3": 2 / 3,
    "21:9": 21 / 9,
}
REFERENCE_MAX_BYTES = {"image": 25 * 1024 * 1024, "video": 200 * 1024 * 1024, "audio": 100 * 1024 * 1024}


def image_ratio(content: bytes) -> float:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
            return image.width / image.height
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"参考图片不是有效图片：{error}")


def dimensions(megapixels: float, ratio: float) -> tuple[int, int]:
    pixels = megapixels * 1_000_000
    width = math.ceil((pixels * ratio) ** 0.5 / 32) * 32
    height = math.ceil((pixels / ratio) ** 0.5 / 32) * 32
    if max(width, height) > 1344:
        if ratio >= 1:
            width = 1344
            height = round((width / ratio) / 32) * 32
        else:
            height = 1344
            width = round((height * ratio) / 32) * 32
    return max(256, width), max(256, height)


def resolve_aspect_ratio(
    requested: str,
    first_ratio: float | None,
    last_ratio: float | None,
    reference_ratio: float | None = None,
) -> tuple[str, float]:
    if requested == "auto":
        # Automatic selection is intentionally shared by every video mode:
        # first frame, then last frame, then the first reference image, and
        # finally the standard landscape ratio.
        selected = first_ratio or last_ratio or reference_ratio
        if selected:
            return "auto", selected
        return "16:9", ASPECT_RATIOS["16:9"]
    if requested in ASPECT_RATIOS:
        return requested, ASPECT_RATIOS[requested]
    raise HTTPException(status_code=400, detail="不支持的画面比例")


async def temporary_upload(upload: UploadFile | None) -> Path | None:
    if upload is None:
        return None
    suffix = Path(upload.filename or "image.png").suffix or ".png"
    fd, name = tempfile.mkstemp(prefix="video_polish_", suffix=suffix)
    os.close(fd)
    path = Path(name)
    path.write_bytes(await upload.read())
    return path


async def temporary_uploads(uploads: list[UploadFile]) -> list[tuple[Path, str]]:
    paths = []
    for upload in uploads:
        path = await temporary_upload(upload)
        if path:
            paths.append((path, upload.filename or path.name))
    return paths


def validate_reference_counts(images: int, videos: int, audios: int) -> None:
    if images > 9:
        raise HTTPException(status_code=400, detail="参考图片最多上传 9 张")
    if videos > 3:
        raise HTTPException(status_code=400, detail="参考视频最多上传 3 个")
    if audios > 3:
        raise HTTPException(status_code=400, detail="独立参考音频最多上传 3 个")
    if images + videos + audios == 0:
        raise HTTPException(status_code=400, detail="请至少上传一个参考图片、视频或音频")


def validate_reference_uploads(
    images: list[UploadFile], videos: list[UploadFile], audios: list[UploadFile]
) -> None:
    for kind, uploads in zip(("image", "video", "audio"), (images, videos, audios)):
        allowed = video_service.REFERENCE_EXTENSIONS[kind]
        for upload in uploads:
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in allowed:
                raise HTTPException(status_code=400, detail=f"不支持的参考{kind}格式：{suffix or '无扩展名'}")
            if upload.size is not None and upload.size > REFERENCE_MAX_BYTES[kind]:
                limit_mb = REFERENCE_MAX_BYTES[kind] // (1024 * 1024)
                raise HTTPException(status_code=413, detail=f"参考文件 {upload.filename} 超过 {limit_mb} MB 限制")


@router.post("/prompt-polish", response_model=PromptPolishResponse, tags=[TAG_POLISH])
async def prompt_polish(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
):
    paths: list[Path] = []
    try:
        for upload in (first_frame, last_frame):
            path = await temporary_upload(upload)
            if path:
                paths.append(path)
        mode = derive_mode(first_frame is not None, last_frame is not None)
        result = await run_in_threadpool(video_service.polish_prompt, prompt, mode, duration, paths)
        return {"prompt": result, "model": video_service.VIDEO_MODEL, "mode": mode}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"提示词润色失败：{error}")
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


@router.post("/reference-prompt-polish", response_model=PromptPolishResponse, tags=[TAG_POLISH])
async def reference_prompt_polish(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    use_video_audio: Annotated[bool, Form()] = True,
    reference_images: list[UploadFile] = File(default=[]),
    reference_videos: list[UploadFile] = File(default=[]),
    reference_audios: list[UploadFile] = File(default=[]),
):
    validate_reference_counts(len(reference_images), len(reference_videos), len(reference_audios))
    validate_reference_uploads(reference_images, reference_videos, reference_audios)
    groups: list[list[tuple[Path, str]]] = []
    try:
        for uploads in (reference_images, reference_videos, reference_audios):
            groups.append(await temporary_uploads(uploads))
        result = await run_in_threadpool(
            video_service.polish_reference_prompt,
            prompt,
            duration,
            [path for path, _ in groups[0]],
            [path for path, _ in groups[1]],
            [path for path, _ in groups[2]],
            use_video_audio,
        )
        return {"prompt": result, "model": video_service.VIDEO_MODEL, "mode": "reference_to_video"}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"全参考提示词润色失败：{error}")
    finally:
        for group in groups:
            for path, _ in group:
                path.unlink(missing_ok=True)


async def _create_video_task_impl(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    resolution_megapixels: Annotated[
        float, Form(ge=VIDEO_MIN_MEGAPIXELS, le=VIDEO_MAX_MEGAPIXELS)
    ] = VIDEO_DEFAULT_MEGAPIXELS,
    aspect_ratio: Annotated[
        str, Form(description="默认 auto：首帧 → 末帧 → 16:9")
    ] = "auto",
    seed: Annotated[int | None, Form()] = None,
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
    auto_polish: bool = False,
    owner: str = "admin",
):
    first_content = await first_frame.read() if first_frame else None
    last_content = await last_frame.read() if last_frame else None
    mode = derive_mode(bool(first_content), bool(last_content))
    first_ratio = image_ratio(first_content) if first_content else None
    last_ratio = image_ratio(last_content) if last_content else None
    warnings = []
    if duration >= 8:
        warnings.append("时长达到 8 秒或以上，生成时间会很长。")
    if resolution_megapixels > 0.5:
        warnings.append("分辨率高于 0.5 MP，生成时间会很长。")
    if first_ratio and last_ratio and abs(first_ratio - last_ratio) / max(first_ratio, last_ratio) > 0.03:
        warnings.append("首帧与末帧画面比例不一致，生成时会按所选比例居中裁剪。")
    effective_aspect_ratio, ratio = resolve_aspect_ratio(aspect_ratio, first_ratio, last_ratio)
    width, height = dimensions(resolution_megapixels, ratio)
    record = video_service.create_task_record(
        mode=mode,
        prompt=prompt.strip(),
        duration=duration,
        width=width,
        height=height,
        resolution_megapixels=resolution_megapixels,
        aspect_ratio=effective_aspect_ratio,
        seed=seed,
        warnings=warnings,
        owner=owner,
        extras={"original_prompt": prompt.strip()} if auto_polish else None,
    )
    try:
        for name, upload, content in (
            ("first_frame", first_frame, first_content),
            ("last_frame", last_frame, last_content),
        ):
            if upload and content:
                video_service.save_input_asset(record["id"], name, upload.filename or f"{name}.png", content)
        if auto_polish:
            video_service.start_polish_and_generation(record["id"])
        else:
            video_service.start_generation(record["id"])
        return video_service.public_task(video_service.load_metadata(record["id"]))
    except Exception as error:
        failed = video_service.load_metadata(record["id"])
        failed.update(status="failed", current_stage="创建任务失败", error=str(error))
        video_service._save_metadata(failed)
        raise HTTPException(status_code=400, detail=str(error))


async def _create_reference_video_task_impl(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    resolution_megapixels: Annotated[
        float, Form(ge=VIDEO_MIN_MEGAPIXELS, le=VIDEO_MAX_MEGAPIXELS)
    ] = VIDEO_DEFAULT_MEGAPIXELS,
    aspect_ratio: Annotated[
        str, Form(description="默认 auto：首帧 → 末帧 → 第一张参考图片 → 16:9")
    ] = "auto",
    seed: Annotated[int | None, Form()] = None,
    ref_image_size: Annotated[str, Form()] = "match",
    use_video_audio: Annotated[bool, Form()] = True,
    reference_images: list[UploadFile] = File(default=[]),
    reference_videos: list[UploadFile] = File(default=[]),
    reference_audios: list[UploadFile] = File(default=[]),
    auto_polish: bool = False,
    owner: str = "admin",
):
    validate_reference_counts(len(reference_images), len(reference_videos), len(reference_audios))
    validate_reference_uploads(reference_images, reference_videos, reference_audios)
    if aspect_ratio != "auto" and aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(status_code=400, detail="不支持的画面比例")
    if ref_image_size not in {"match", "max"}:
        raise HTTPException(status_code=400, detail="ref_image_size 仅支持 match 或 max")
    groups: list[list[tuple[UploadFile, bytes]]] = []
    for uploads in (reference_images, reference_videos, reference_audios):
        group = []
        for upload in uploads:
            content = await upload.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"参考文件 {upload.filename or ''} 不能为空")
            group.append((upload, content))
        groups.append(group)

    first_reference_ratio = image_ratio(groups[0][0][1]) if groups[0] else None

    warnings = []
    if duration >= 8:
        warnings.append("时长达到 8 秒或以上，生成时间会很长。")
    if resolution_megapixels > 0.5:
        warnings.append("分辨率高于 0.5 MP，生成时间会很长。")
    if ref_image_size == "max":
        warnings.append("参考图保真度选择 max，参考 token 会参与每一步采样，生成时间会显著增加。")
    effective_aspect_ratio, ratio = resolve_aspect_ratio(
        aspect_ratio, None, None, first_reference_ratio
    )
    width, height = dimensions(resolution_megapixels, ratio)
    counts = {"images": len(groups[0]), "videos": len(groups[1]), "audios": len(groups[2])}
    record = video_service.create_task_record(
        mode="reference_to_video",
        prompt=prompt.strip(),
        duration=duration,
        width=width,
        height=height,
        resolution_megapixels=resolution_megapixels,
        aspect_ratio=effective_aspect_ratio,
        seed=seed,
        warnings=warnings,
        owner=owner,
        extras={
            "reference_counts": counts,
            "ref_image_size": ref_image_size,
            "use_video_audio": use_video_audio,
            "_reference_assets": [],
            **({"original_prompt": prompt.strip()} if auto_polish else {}),
        },
    )
    try:
        for kind, group in zip(("image", "video", "audio"), groups):
            for index, (upload, content) in enumerate(group, start=1):
                video_service.save_reference_asset(
                    record["id"], kind, index, upload.filename or f"reference_{kind}_{index}", content
                )
        if auto_polish:
            video_service.start_polish_and_generation(record["id"])
        else:
            video_service.start_generation(record["id"])
        return video_service.public_task(video_service.load_metadata(record["id"]))
    except Exception as error:
        failed = video_service.load_metadata(record["id"])
        failed.update(status="failed", current_stage="创建任务失败", error=str(error))
        video_service._save_metadata(failed)
        raise HTTPException(status_code=400, detail=str(error))


@router.post(
    "/tasks",
    response_model=VideoTaskSummary,
    status_code=202,
    tags=[TAG_WORKFLOWS],
    summary="创建文本/首帧/尾帧视频任务",
)
async def create_video_task(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    resolution_megapixels: Annotated[
        float, Form(ge=VIDEO_MIN_MEGAPIXELS, le=VIDEO_MAX_MEGAPIXELS)
    ] = VIDEO_DEFAULT_MEGAPIXELS,
    aspect_ratio: Annotated[
        str, Form(description="默认 auto：首帧 → 末帧 → 16:9")
    ] = "auto",
    seed: Annotated[int | None, Form()] = None,
    polish_prompt_first: Annotated[
        bool, Form(description="默认 false 直接生成；true 时先用配置的视觉模型润色，再自动生成")
    ] = False,
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
    access: dict = Depends(require_video_access),
):
    """直接使用提交的提示词生成；不含图片、仅首帧、仅尾帧、首尾帧共用同一工作流。"""
    return await _create_video_task_impl(
        prompt, duration, resolution_megapixels, aspect_ratio, seed, first_frame, last_frame,
        polish_prompt_first, access["key_id"],
    )


@router.post(
    "/reference-tasks",
    response_model=VideoTaskSummary,
    status_code=202,
    tags=[TAG_WORKFLOWS],
    summary="创建多模态参考视频任务",
)
async def create_reference_video_task(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    resolution_megapixels: Annotated[
        float, Form(ge=VIDEO_MIN_MEGAPIXELS, le=VIDEO_MAX_MEGAPIXELS)
    ] = VIDEO_DEFAULT_MEGAPIXELS,
    aspect_ratio: Annotated[
        str, Form(description="默认 auto：首帧 → 末帧 → 第一张参考图片 → 16:9")
    ] = "auto",
    seed: Annotated[int | None, Form()] = None,
    polish_prompt_first: Annotated[
        bool, Form(description="默认 false 直接生成；true 时先生成六段式参考提示词，再自动生成")
    ] = False,
    ref_image_size: Annotated[str, Form()] = "match",
    use_video_audio: Annotated[bool, Form()] = True,
    reference_images: list[UploadFile] = File(default=[]),
    reference_videos: list[UploadFile] = File(default=[]),
    reference_audios: list[UploadFile] = File(default=[]),
    access: dict = Depends(require_video_access),
):
    """直接使用提交的六段式提示词，通过 R2V 工作流生成多模态参考视频。"""
    return await _create_reference_video_task_impl(
        prompt, duration, resolution_megapixels, aspect_ratio, seed, ref_image_size,
        use_video_audio, reference_images, reference_videos, reference_audios, polish_prompt_first,
        access["key_id"],
    )


def parse_task_ids(raw: str) -> list[str]:
    return list(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))[:500]


@router.get(
    "/tasks",
    response_model=VideoTaskList,
    tags=[TAG_TASKS],
    summary="按本地任务 ID 查询历史",
)
async def get_video_tasks(
    task_ids: Annotated[str, Query(description="浏览器本地保存的任务 ID，使用英文逗号分隔")] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    access: dict = Depends(require_video_access),
):
    """管理员可查看全部任务；其他身份（API Key / 注册用户）仅能看到自己创建的任务。"""
    if access["role"] == "admin":
        return {"tasks": video_service.list_tasks([], limit, include_all=True)}
    return {"tasks": video_service.list_tasks([], limit, owner=access["key_id"])}


@router.get(
    "/tasks/{task_id}",
    response_model=VideoTaskSummary,
    tags=[TAG_TASKS],
    summary="查询异步任务进度与结果",
)
async def get_video_task(task_id: str):
    """返回任务当前状态；任务完成后同一响应中的 ``assets.video`` 即为结果代理地址。"""
    try:
        return video_service.public_task(video_service.load_metadata(task_id))
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="视频任务不存在")


@router.get(
    "/tasks/{task_id}/assets/{asset_name}",
    response_class=FileResponse,
    tags=[TAG_TASKS],
    summary="获取任务输入或输出资源",
)
async def get_video_asset(task_id: str, asset_name: str):
    try:
        path = video_service.asset_path(task_id, asset_name)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="资源不存在")
    media_type = "video/mp4" if asset_name == "video" and path.suffix.lower() == ".mp4" else None
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name if asset_name == "video" else None,
        content_disposition_type="inline",
    )


@router.delete(
    "/tasks/{task_id}",
    status_code=204,
    tags=[TAG_TASKS],
    summary="删除本地任务记录及全部资源",
)
async def delete_video_task(task_id: str):
    """仅允许删除 completed/failed 终态任务；删除后文件不可恢复。"""
    try:
        await run_in_threadpool(video_service.delete_task_record, task_id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="视频任务不存在")
    except OSError as error:
        raise HTTPException(status_code=409, detail=f"删除失败，文件可能被占用：{error}")
    return Response(status_code=204)


@router.get(
    "/queue",
    response_model=VideoQueueStatus,
    tags=[TAG_TASKS],
    summary="查询本地任务的排队情况",
)
async def get_video_queue(
    task_ids: Annotated[str, Query(description="浏览器本地保存的任务 ID，使用英文逗号分隔")] = "",
    access: dict = Depends(require_video_access),
):
    try:
        if access["role"] == "admin":
            return await run_in_threadpool(video_service.sanitized_queue, [], True)
        return await run_in_threadpool(video_service.sanitized_queue, [], False, access["key_id"])
    except Exception:
        raise HTTPException(status_code=502, detail="暂时无法获取生成队列，请稍后重试")


@router.get(
    "/tasks/{task_id}/gif",
    tags=[TAG_TASKS],
    summary="将已完成视频转换为 GIF（需配置 ffmpeg）",
)
async def get_video_gif(task_id: str):
    try:
        path = await run_in_threadpool(video_service.ensure_video_gif, task_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="视频资源不存在")
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)[:300])
    return FileResponse(
        path,
        media_type="image/gif",
        filename=path.name,
        content_disposition_type="inline",
    )
