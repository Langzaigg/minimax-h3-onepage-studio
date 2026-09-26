import os
import tempfile
import math
import logging
from io import BytesIO
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal

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
from services.delivery_profiles import fit_even_aspect, parse_aspect, ratio_text, source_profile, target_size


logger = logging.getLogger(__name__)


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


ASPECT_RATIOS = {name: parse_aspect(name) for name in
                 ("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9")}
REFERENCE_MAX_BYTES = {"image": 25 * 1024 * 1024, "video": 200 * 1024 * 1024, "audio": 100 * 1024 * 1024}


def image_ratio(content: bytes) -> Fraction:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
            return Fraction(image.width, image.height)
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
    first_ratio: Fraction | None,
    last_ratio: Fraction | None,
    reference_ratio: Fraction | None = None,
) -> tuple[str, Fraction]:
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
    try:
        ratio = parse_aspect(requested)
        return ratio_text(ratio), ratio
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def resolve_source_dimensions(megapixels, ratio, target_resolution, source_resolution_profile,
                              *, turbo=None, mode=None):
    """Separate the model grid from the exact-ratio source video we publish."""
    try:
        target_size(target_resolution)
        profile_id = (source_resolution_profile or "").strip()
        if profile_id:
            profile = source_profile(profile_id)
            if ratio != Fraction(16, 9):
                raise ValueError("作者初采方案仅支持16:9；其他比例请使用自定义像素预算")
            if turbo and mode != "hybrid_to_video" and float(turbo["megapixels"]) != profile["source_megapixels"]:
                raise ValueError("Turbo固定像素预算与初采方案不一致；请取消初采方案或选择相同预算")
            megapixels = profile["source_megapixels"]
            sampling_width, sampling_height = profile["sampling_width"], profile["sampling_height"]
        else:
            sampling_width, sampling_height = dimensions(megapixels, ratio)
        output_width, output_height = fit_even_aspect(sampling_width, sampling_height, ratio)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return megapixels, {
        "sampling_width": sampling_width, "sampling_height": sampling_height,
        "output_width": output_width, "output_height": output_height,
        "aspect_ratio_fraction": ratio_text(ratio), "target_resolution": target_resolution,
        "source_resolution_profile": profile_id,
    }


def _auto_source_options(enabled, duration, aspect_ratio, target_resolution):
    """Only the explicit server-side finishing checkbox selects a source profile."""
    if not enabled:
        return aspect_ratio, ""
    if duration > 16.25:
        raise HTTPException(status_code=400, detail="自动二采目前仅支持16.25秒以内的视频")
    if video_service._frame_length(duration) / 24 > 16.25:
        raise HTTPException(status_code=400, detail="H3帧数网格会使实际视频超过自动二采16.25秒上限；请选择15.79秒或更短")
    return "16:9", target_resolution


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


def resolve_nsfw(nsfw: bool, nsfw_preset: str, mode: str) -> dict | None:
    """解析任务的 NSFW 档位（含该档要叠加的 LoRA 列表），关闭时返回 None。

    - fl2v 家族与多模态参考（ref2v）各自一组 LoRA 档位；
    - 开启但未指定档位时回落到该模式第一个 LoRA 档位（历史任务兼容）。
    """
    if not nsfw:
        return None
    reference = mode == "reference_to_video"
    family = "多模态参考模式" if reference else "当前模式"
    available = list(video_service.nsfw_preset_ids(mode))
    if not available:
        group = "ref2v" if reference else "fl2v"
        detail = f"{family}未配置任何 NSFW 档位（comfyui.nsfw.{group}_presets 为空）"
        raise HTTPException(status_code=400, detail=detail)
    preset = video_service.resolve_nsfw_preset(nsfw_preset, mode)
    if preset is None:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的 NSFW 档位：{nsfw_preset}（{family}可用档位：{' / '.join(available)}）",
        )
    return preset


def validate_nsfw_turbo_conflict(nsfw_preset: dict | None, turbo: dict | None) -> None:
    """NSFW LoRA 与 turbo 蒸馏 LoRA 互斥：两者不可叠加，只能选其一。"""
    if nsfw_preset and turbo:
        raise HTTPException(
            status_code=400,
            detail="NSFW LoRA 与 turbo 加速不能同时使用，请关闭 NSFW 或选择普通分辨率档位",
        )


def _merge_nsfw(nsfw: bool, nsfw_native: bool, nsfw_strength: float) -> bool:
    """兼容历史参数：nsfw_native=true 或 nsfw_strength>0 一律回落为 nsfw=true（开启）。"""
    return bool(nsfw or nsfw_native or float(nsfw_strength or 0) > 0)


def resolve_hybrid(hybrid_preset: str) -> dict:
    try:
        return video_service.resolve_hybrid_preset(hybrid_preset)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def resolve_resolution(
    resolution_preset: str, resolution_megapixels: float, mode: str
) -> tuple[float, str | None, dict | None]:
    """解析分辨率档位。

    返回 (有效兆像素, 档位 id, turbo 工作流参数)：
    - 空档位 → 普通工作流，使用 resolution_megapixels；
    - turbo-* → 校验当前模式是否支持且对应 lora 已配置，返回档位对应的 MP 与 turbo 参数；
      混合模式（hybrid_to_video）turbo 不锁定分辨率，直接采用请求的 MP。
    """
    preset = (resolution_preset or "").strip()
    if not preset:
        return resolution_megapixels, None, None
    turbo = video_service.resolve_turbo_preset(preset, mode)
    if turbo is None:
        allowed = " / ".join(video_service.turbo_preset_ids(mode)) or "无"
        raise HTTPException(
            status_code=400,
            detail=f"不支持的分辨率档位：{preset}（当前模式可用 turbo 档位：{allowed}）",
        )
    if mode == "hybrid_to_video":
        return resolution_megapixels, preset, turbo
    return float(turbo["megapixels"]), preset, turbo


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
    nsfw: Annotated[
        bool, Form(description="NSFW 开关：true 时润色注入 NSFW 风格指南，生成时叠加 NSFW LoRA")
    ] = False,
    nsfw_preset: Annotated[
        str, Form(description="NSFW 档位 id（见 /api/config 的 nsfw_presets.fl2v）；留空用第一个档位")
    ] = "",
    nsfw_strength: Annotated[float, Form(description="已废弃：>0 视为 nsfw=true")] = 0.0,
    nsfw_native: Annotated[bool, Form(description="已废弃：true 视为 nsfw=true")] = False,
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
):
    nsfw = _merge_nsfw(nsfw, nsfw_native, nsfw_strength)
    paths: list[Path] = []
    try:
        for upload in (first_frame, last_frame):
            path = await temporary_upload(upload)
            if path:
                paths.append(path)
        mode = derive_mode(first_frame is not None, last_frame is not None)
        nsfw = bool(resolve_nsfw(nsfw, nsfw_preset, mode))
        result = await run_in_threadpool(
            video_service.polish_prompt, prompt, mode, duration, paths, nsfw
        )
        return {"prompt": result, "model": video_service.VIDEO_MODEL, "mode": mode}
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("prompt-polish 润色失败")
        raise HTTPException(status_code=502, detail=f"提示词润色失败：{error}")
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


@router.post("/reference-prompt-polish", response_model=PromptPolishResponse, tags=[TAG_POLISH])
async def reference_prompt_polish(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    nsfw: Annotated[
        bool, Form(description="NSFW 开关：true 时润色注入 NSFW 风格指南，生成时叠加 ref2v NSFW LoRA")
    ] = False,
    nsfw_preset: Annotated[
        str, Form(description="NSFW 档位 id（见 /api/config 的 nsfw_presets.ref2v）；留空用第一个档位")
    ] = "",
    nsfw_strength: Annotated[float, Form(description="已废弃：>0 视为 nsfw=true")] = 0.0,
    nsfw_native: Annotated[bool, Form(description="已废弃：true 视为 nsfw=true")] = False,
    use_video_audio: Annotated[bool, Form()] = True,
    reference_images: list[UploadFile] = File(default=[]),
    reference_videos: list[UploadFile] = File(default=[]),
    reference_audios: list[UploadFile] = File(default=[]),
):
    nsfw = bool(
        resolve_nsfw(
            _merge_nsfw(nsfw, nsfw_native, nsfw_strength), nsfw_preset, "reference_to_video"
        )
    )
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
            nsfw,
        )
        return {"prompt": result, "model": video_service.VIDEO_MODEL, "mode": "reference_to_video"}
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("reference-prompt-polish 全参考润色失败")
        raise HTTPException(status_code=502, detail=f"全参考提示词润色失败：{error}")
    finally:
        for group in groups:
            for path, _ in group:
                path.unlink(missing_ok=True)


@router.post("/hybrid-prompt-polish", response_model=PromptPolishResponse, tags=[TAG_POLISH])
async def hybrid_prompt_polish(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    nsfw: Annotated[
        bool, Form(description="NSFW 开关：混合模式润色始终自动打开 NSFW 破甲，此处仅控制是否额外注入 NSFW 风格指南")
    ] = False,
    nsfw_strength: Annotated[float, Form(description="已废弃：>0 视为 nsfw=true")] = 0.0,
    nsfw_native: Annotated[bool, Form(description="已废弃：true 视为 nsfw=true")] = False,
    use_video_audio: Annotated[bool, Form()] = True,
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
    reference_images: list[UploadFile] = File(default=[]),
    reference_videos: list[UploadFile] = File(default=[]),
    reference_audios: list[UploadFile] = File(default=[]),
    hybrid_preset: Annotated[Literal["anime", "nsfw"], Form()] = "anime",
):
    """混合模式（Dasiwa Hybrid）润色：上传参考素材时走六段式全参考润色
    （首尾帧不参与分析，作为生成时的关键帧锚定），否则走文本/首尾帧润色；
    无论 nsfw 是否开启，破甲（NSFW 增强）始终自动打开。"""
    resolve_hybrid(hybrid_preset)
    nsfw = _merge_nsfw(nsfw, nsfw_native, nsfw_strength)
    has_reference = bool(reference_images or reference_videos or reference_audios)
    if has_reference:
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
                nsfw,
                True,  # force_armor（仅非混合路径使用）
                True,  # hybrid：混合模式专属提示词；破甲由全局 llm.nsfw_armor 开关拼接控制
            )
            return {"prompt": result, "model": video_service.VIDEO_MODEL, "mode": "hybrid_to_video", "hybrid_preset": hybrid_preset}
        except HTTPException:
            raise
        except Exception as error:
            logger.exception("hybrid-prompt-polish 全参考润色失败")
            raise HTTPException(status_code=502, detail=f"混合模式全参考提示词润色失败：{error}")
        finally:
            for group in groups:
                for path, _ in group:
                    path.unlink(missing_ok=True)
    paths: list[Path] = []
    try:
        for upload in (first_frame, last_frame):
            path = await temporary_upload(upload)
            if path:
                paths.append(path)
        result = await run_in_threadpool(
            video_service.polish_prompt, prompt, "hybrid_to_video", duration, paths, nsfw
        )
        return {"prompt": result, "model": video_service.VIDEO_MODEL, "mode": "hybrid_to_video", "hybrid_preset": hybrid_preset}
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("hybrid-prompt-polish 润色失败")
        raise HTTPException(status_code=502, detail=f"混合模式提示词润色失败：{error}")
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


async def _create_video_task_impl(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    resolution_megapixels: Annotated[
        float, Form(ge=VIDEO_MIN_MEGAPIXELS, le=VIDEO_MAX_MEGAPIXELS)
    ] = VIDEO_DEFAULT_MEGAPIXELS,
    resolution_preset: Annotated[
        str, Form(description="分辨率档位：留空走普通工作流；turbo-0.5-6 / turbo-0.5-8 / turbo-1.0-4 为 turbo 蒸馏档位")
    ] = "",
    aspect_ratio: Annotated[
        str, Form(description="默认 auto：首帧 → 末帧 → 16:9")
    ] = "auto",
    seed: Annotated[int | None, Form()] = None,
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
    auto_polish: bool = False,
    owner: str = "admin",
    nsfw: bool = False,
    nsfw_preset: str = "",
    target_resolution: str = "1080p",
    source_resolution_profile: str = "",
    auto_postprocess: bool = False,
):
    aspect_ratio, source_resolution_profile = _auto_source_options(
        auto_postprocess, duration, aspect_ratio, target_resolution)
    first_content = await first_frame.read() if first_frame else None
    last_content = await last_frame.read() if last_frame else None
    mode = derive_mode(bool(first_content), bool(last_content))
    nsfw_preset_id = resolve_nsfw(nsfw, nsfw_preset, mode)
    first_ratio = image_ratio(first_content) if first_content else None
    last_ratio = image_ratio(last_content) if last_content else None
    effective_megapixels, preset_id, turbo = resolve_resolution(
        resolution_preset, resolution_megapixels, mode
    )
    validate_nsfw_turbo_conflict(nsfw_preset_id, turbo)
    warnings = []
    if duration >= 8:
        warnings.append("时长达到 8 秒或以上，生成时间会很长。")
    if first_ratio and last_ratio and abs(first_ratio - last_ratio) / max(first_ratio, last_ratio) > 0.03:
        warnings.append("首帧与末帧画面比例不一致，生成时会按所选比例居中裁剪。")
    effective_aspect_ratio, ratio = resolve_aspect_ratio(aspect_ratio, first_ratio, last_ratio)
    effective_megapixels, source_dimensions = resolve_source_dimensions(
        effective_megapixels, ratio, target_resolution, source_resolution_profile, turbo=turbo, mode=mode)
    width, height = source_dimensions["output_width"], source_dimensions["output_height"]
    if effective_megapixels > 0.5:
        warnings.append("分辨率高于 0.5 MP，生成时间会很长。")
    extras = {
        **source_dimensions,
        "auto_postprocess": bool(auto_postprocess),
        "nsfw": bool(nsfw_preset_id),
        "nsfw_preset": nsfw_preset_id["id"] if nsfw_preset_id else None,
        "resolution_preset": preset_id,
    }
    if turbo:
        extras["turbo_steps"] = int(turbo["steps"])
    if auto_polish:
        extras["original_prompt"] = prompt.strip()
    record = video_service.create_task_record(
        mode=mode,
        prompt=prompt.strip(),
        duration=duration,
        width=width,
        height=height,
        resolution_megapixels=effective_megapixels,
        aspect_ratio=effective_aspect_ratio,
        seed=seed,
        warnings=warnings,
        owner=owner,
        extras=extras,
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
    resolution_preset: Annotated[
        str, Form(description="分辨率档位：留空走普通工作流；turbo-0.5-4 / turbo-0.5-6 / turbo-0.5-8 共用同一个 turbo lora")
    ] = "",
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
    nsfw: bool = False,
    nsfw_preset: str = "",
    target_resolution: str = "1080p",
    source_resolution_profile: str = "",
    auto_postprocess: bool = False,
):
    aspect_ratio, source_resolution_profile = _auto_source_options(
        auto_postprocess, duration, aspect_ratio, target_resolution)
    # 参考生成接口的 NSFW 由 ref2v 专属 LoRA 档位提供（不替换 Ref2VA 基模），
    # 仅普通工作流可用，与 turbo 加速互斥；未配置档位时拒绝开启。
    nsfw_preset_id = resolve_nsfw(nsfw, nsfw_preset, "reference_to_video")
    validate_reference_counts(len(reference_images), len(reference_videos), len(reference_audios))
    validate_reference_uploads(reference_images, reference_videos, reference_audios)
    if aspect_ratio != "auto":
        resolve_aspect_ratio(aspect_ratio, None, None)
    if ref_image_size not in {"match", "max"}:
        raise HTTPException(status_code=400, detail="ref_image_size 仅支持 match 或 max")
    effective_megapixels, preset_id, turbo = resolve_resolution(
        resolution_preset, resolution_megapixels, "reference_to_video"
    )
    validate_nsfw_turbo_conflict(nsfw_preset_id, turbo)
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
    if ref_image_size == "max":
        warnings.append("参考图保真度选择 max，参考 token 会参与每一步采样，生成时间会显著增加。")
    effective_aspect_ratio, ratio = resolve_aspect_ratio(
        aspect_ratio, None, None, first_reference_ratio
    )
    effective_megapixels, source_dimensions = resolve_source_dimensions(
        effective_megapixels, ratio, target_resolution, source_resolution_profile,
        turbo=turbo, mode="reference_to_video")
    width, height = source_dimensions["output_width"], source_dimensions["output_height"]
    if effective_megapixels > 0.5:
        warnings.append("分辨率高于 0.5 MP，生成时间会很长。")
    counts = {"images": len(groups[0]), "videos": len(groups[1]), "audios": len(groups[2])}
    extras = {
        **source_dimensions,
        "auto_postprocess": bool(auto_postprocess),
        "reference_counts": counts,
        "ref_image_size": ref_image_size,
        "use_video_audio": use_video_audio,
        "nsfw": bool(nsfw_preset_id),
        "nsfw_preset": nsfw_preset_id["id"] if nsfw_preset_id else None,
        "resolution_preset": preset_id,
        "_reference_assets": [],
        **({"original_prompt": prompt.strip()} if auto_polish else {}),
    }
    if turbo:
        extras["turbo_steps"] = int(turbo["steps"])
    record = video_service.create_task_record(
        mode="reference_to_video",
        prompt=prompt.strip(),
        duration=duration,
        width=width,
        height=height,
        resolution_megapixels=effective_megapixels,
        aspect_ratio=effective_aspect_ratio,
        seed=seed,
        warnings=warnings,
        owner=owner,
        extras=extras,
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


async def _create_hybrid_video_task_impl(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    resolution_megapixels: Annotated[
        float, Form(ge=VIDEO_MIN_MEGAPIXELS, le=VIDEO_MAX_MEGAPIXELS)
    ] = VIDEO_DEFAULT_MEGAPIXELS,
    resolution_preset: Annotated[
        str, Form(description="分辨率档位：留空走普通工作流（固定 25 步）；hybrid-turbo-8 为 turbo 蒸馏档位（固定 8 步）")
    ] = "",
    aspect_ratio: Annotated[
        str, Form(description="默认 auto：首帧 → 末帧 → 第一张参考图片 → 16:9")
    ] = "auto",
    seed: Annotated[int | None, Form()] = None,
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
    ref_image_size: Annotated[str, Form()] = "match",
    use_video_audio: Annotated[bool, Form()] = True,
    reference_images: list[UploadFile] = File(default=[]),
    reference_videos: list[UploadFile] = File(default=[]),
    reference_audios: list[UploadFile] = File(default=[]),
    auto_polish: bool = False,
    owner: str = "admin",
    nsfw: bool = False,
    hybrid_preset: str = "anime",
    target_resolution: str = "1080p",
    source_resolution_profile: str = "",
    auto_postprocess: bool = False,
):
    aspect_ratio, source_resolution_profile = _auto_source_options(
        auto_postprocess, duration, aspect_ratio, target_resolution)
    # 混合模式（Dasiwa Hybrid）一个基模同时覆盖 fl2v 与 ref2v 能力：
    # 有参考素材 → 参考通道（136）；此时首尾帧不再忽略，而是作为关键帧锚定，
    # 与参考媒体一起构成纯官方节点的“混合参考”（136 + MiniMaxH3AddGuide）。
    # 无参考素材 → 文本/首尾帧通道（104）。
    # NSFW 不叠加 LoRA（基模自带能力），nsfw 布尔仅影响润色时是否额外注入风格指南，
    # 破甲（NSFW 增强）在润色时始终自动打开。
    selected_hybrid = resolve_hybrid(hybrid_preset)
    if selected_hybrid["distilled"] and resolution_preset:
        raise HTTPException(status_code=400, detail="Eros Max beta5 已内置 Turbo，请取消额外 Turbo 档位")
    first_content = await first_frame.read() if first_frame else None
    last_content = await last_frame.read() if last_frame else None
    groups: list[list[tuple[UploadFile, bytes]]] = []
    for uploads in (reference_images, reference_videos, reference_audios):
        group = []
        for upload in uploads:
            content = await upload.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"参考文件 {upload.filename or ''} 不能为空")
            group.append((upload, content))
        groups.append(group)
    has_reference = any(len(group) for group in groups)
    if has_reference:
        validate_reference_counts(len(groups[0]), len(groups[1]), len(groups[2]))
        validate_reference_uploads(reference_images, reference_videos, reference_audios)
        if ref_image_size not in {"match", "max"}:
            raise HTTPException(status_code=400, detail="ref_image_size 仅支持 match 或 max")
    first_ratio = image_ratio(first_content) if first_content else None
    last_ratio = image_ratio(last_content) if last_content else None
    first_reference_ratio = image_ratio(groups[0][0][1]) if groups[0] else None

    effective_megapixels, preset_id, turbo = resolve_resolution(
        resolution_preset, resolution_megapixels, "hybrid_to_video"
    )

    warnings = []
    if duration >= 8:
        warnings.append("时长达到 8 秒或以上，生成时间会很长。")
    if has_reference:
        if first_content or last_content:
            warnings.append(
                "已上传参考素材与首尾帧：首尾帧将作为关键帧锚定，与参考媒体共同约束画面。"
            )
        if ref_image_size == "max":
            warnings.append("参考图保真度选择 max，参考 token 会参与每一步采样，生成时间会显著增加。")
    if first_ratio and last_ratio and abs(first_ratio - last_ratio) / max(first_ratio, last_ratio) > 0.03:
        warnings.append("首帧与末帧画面比例不一致，生成时会按所选比例居中裁剪。")
    effective_aspect_ratio, ratio = resolve_aspect_ratio(
        aspect_ratio, first_ratio, last_ratio, first_reference_ratio
    )
    effective_megapixels, source_dimensions = resolve_source_dimensions(
        effective_megapixels, ratio, target_resolution, source_resolution_profile,
        turbo=turbo, mode="hybrid_to_video")
    width, height = source_dimensions["output_width"], source_dimensions["output_height"]
    if effective_megapixels > 0.5:
        warnings.append("分辨率高于 0.5 MP，生成时间会很长。")
    extras = {
        **source_dimensions,
        "auto_postprocess": bool(auto_postprocess),
        "nsfw": bool(nsfw),
        "hybrid_preset": selected_hybrid["id"],
        "hybrid_model": turbo["diffusion"] if turbo else selected_hybrid["model"],
        "sampling_steps": int(turbo["steps"] if turbo else selected_hybrid["steps"]),
        "resolution_preset": preset_id,
        "_reference_assets": [],
        **({"original_prompt": prompt.strip()} if auto_polish else {}),
    }
    if has_reference:
        extras.update(
            {
                "reference_counts": {
                    "images": len(groups[0]),
                    "videos": len(groups[1]),
                    "audios": len(groups[2]),
                },
                "ref_image_size": ref_image_size,
                "use_video_audio": use_video_audio,
            }
        )
    if turbo:
        extras["turbo_steps"] = int(turbo["steps"])
    record = video_service.create_task_record(
        mode="hybrid_to_video",
        prompt=prompt.strip(),
        duration=duration,
        width=width,
        height=height,
        resolution_megapixels=effective_megapixels,
        aspect_ratio=effective_aspect_ratio,
        seed=seed,
        warnings=warnings,
        owner=owner,
        extras=extras,
    )
    try:
        if has_reference:
            for kind, group in zip(("image", "video", "audio"), groups):
                for index, (upload, content) in enumerate(group, start=1):
                    video_service.save_reference_asset(
                        record["id"], kind, index, upload.filename or f"reference_{kind}_{index}", content
                    )
        # 首尾帧无论是否上传参考素材都保存：有参考时作为关键帧锚定（混合参考），
        # 无参考时走 104 文本/首尾帧通道。
        for name, upload, content in (
            ("first_frame", first_frame, first_content),
            ("last_frame", last_frame, last_content),
        ):
            if upload and content:
                video_service.save_input_asset(
                    record["id"], name, upload.filename or f"{name}.png", content
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
    "/hybrid-tasks",
    response_model=VideoTaskSummary,
    status_code=202,
    tags=[TAG_WORKFLOWS],
    summary="创建综合视频任务（动画特调 / NSFW 特调）",
)
async def create_hybrid_video_task(
    prompt: Annotated[str, Form(min_length=1)],
    duration: Annotated[float, Form(ge=VIDEO_MIN_DURATION, le=VIDEO_MAX_DURATION)] = VIDEO_DEFAULT_DURATION,
    resolution_megapixels: Annotated[
        float, Form(ge=VIDEO_MIN_MEGAPIXELS, le=VIDEO_MAX_MEGAPIXELS)
    ] = VIDEO_DEFAULT_MEGAPIXELS,
    resolution_preset: Annotated[
        str, Form(description="分辨率档位：留空走普通工作流（固定 25 步）；hybrid-turbo-8 为 turbo 蒸馏档位（固定 8 步）")
    ] = "",
    aspect_ratio: Annotated[
        str, Form(description="默认 auto：首帧 → 末帧 → 第一张参考图片 → 16:9")
    ] = "auto",
    seed: Annotated[int | None, Form()] = None,
    polish_prompt_first: Annotated[
        bool, Form(description="默认 false 直接生成；true 时先润色提示词再自动生成（混合模式润色自动打开 NSFW 增强）")
    ] = False,
    nsfw: Annotated[
        bool, Form(description="NSFW 开关：仅影响润色时是否额外注入 NSFW 风格指南（破甲始终自动打开）")
    ] = False,
    nsfw_strength: Annotated[float, Form(description="已废弃：>0 视为 nsfw=true")] = 0.0,
    nsfw_native: Annotated[bool, Form(description="已废弃：true 视为 nsfw=true")] = False,
    ref_image_size: Annotated[str, Form()] = "match",
    use_video_audio: Annotated[bool, Form()] = True,
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
    reference_images: list[UploadFile] = File(default=[]),
    reference_videos: list[UploadFile] = File(default=[]),
    reference_audios: list[UploadFile] = File(default=[]),
    access: dict = Depends(require_video_access),
    hybrid_preset: Annotated[Literal["anime", "nsfw"], Form()] = "anime",
    target_resolution: Annotated[str, Form()] = "1080p",
    source_resolution_profile: Annotated[str, Form()] = "",
    auto_postprocess: Annotated[bool, Form()] = False,
):
    """选择 Dasiwa Hybrid v2 或 Eros Max beta5：参考素材走多模态参考通道（首/末帧经官方
    MiniMaxH3AddGuide 锚定为关键帧，与参考媒体共存），否则走文本/首尾帧通道；
    Dasiwa 默认 25 步，Eros 内置 Turbo 默认 8 步，两者均保持完整采样。"""
    return await _create_hybrid_video_task_impl(
        prompt, duration, resolution_megapixels, resolution_preset, aspect_ratio, seed,
        first_frame, last_frame, ref_image_size, use_video_audio,
        reference_images, reference_videos, reference_audios,
        polish_prompt_first, access["key_id"],
        _merge_nsfw(nsfw, nsfw_native, nsfw_strength),
        hybrid_preset,
        target_resolution, source_resolution_profile, auto_postprocess,
    )


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
    resolution_preset: Annotated[
        str, Form(description="分辨率档位：留空走普通工作流；turbo-0.5-6 / turbo-0.5-8 / turbo-1.0-4 为 turbo 蒸馏档位")
    ] = "",
    aspect_ratio: Annotated[
        str, Form(description="默认 auto：首帧 → 末帧 → 16:9")
    ] = "auto",
    seed: Annotated[int | None, Form()] = None,
    polish_prompt_first: Annotated[
        bool, Form(description="默认 false 直接生成；true 时先用配置的视觉模型润色，再自动生成")
    ] = False,
    nsfw: Annotated[
        bool, Form(description="NSFW 开关：true 时叠加 NSFW LoRA 并注入风格指南；仅普通档位可用，与 turbo 互斥")
    ] = False,
    nsfw_preset: Annotated[
        str, Form(description="NSFW 档位 id（见 /api/config 的 nsfw_presets.fl2v）；留空用第一个档位")
    ] = "",
    nsfw_strength: Annotated[float, Form(description="已废弃：>0 视为 nsfw=true")] = 0.0,
    nsfw_native: Annotated[bool, Form(description="已废弃：true 视为 nsfw=true")] = False,
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
    access: dict = Depends(require_video_access),
    target_resolution: Annotated[str, Form()] = "1080p",
    source_resolution_profile: Annotated[str, Form()] = "",
    auto_postprocess: Annotated[bool, Form()] = False,
):
    """直接使用提交的提示词生成；不含图片、仅首帧、仅尾帧、首尾帧共用同一工作流。"""
    # 历史参数 nsfw_native=true / nsfw_strength>0 仅视为 nsfw=true（回落默认 LoRA 档位）。
    nsfw_on = _merge_nsfw(nsfw, nsfw_native, nsfw_strength)
    return await _create_video_task_impl(
        prompt, duration, resolution_megapixels, resolution_preset, aspect_ratio, seed,
        first_frame, last_frame, polish_prompt_first, access["key_id"],
        nsfw_on, nsfw_preset,
        target_resolution, source_resolution_profile, auto_postprocess,
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
    resolution_preset: Annotated[
        str, Form(description="分辨率档位：留空走普通工作流；turbo-0.5-4 / turbo-0.5-6 / turbo-0.5-8 共用同一个 turbo lora")
    ] = "",
    aspect_ratio: Annotated[
        str, Form(description="默认 auto：首帧 → 末帧 → 第一张参考图片 → 16:9")
    ] = "auto",
    seed: Annotated[int | None, Form()] = None,
    polish_prompt_first: Annotated[
        bool, Form(description="默认 false 直接生成；true 时先生成六段式参考提示词，再自动生成")
    ] = False,
    nsfw: Annotated[
        bool, Form(description="NSFW 开关：true 时普通 ref2v 工作流叠加 NSFW LoRA 并注入风格指南；与 turbo 档位互斥")
    ] = False,
    nsfw_preset: Annotated[
        str, Form(description="NSFW 档位 id（见 /api/config 的 nsfw_presets.ref2v）；留空用第一个档位")
    ] = "",
    nsfw_strength: Annotated[float, Form(description="已废弃：>0 视为 nsfw=true")] = 0.0,
    nsfw_native: Annotated[bool, Form(description="已废弃：true 视为 nsfw=true")] = False,
    ref_image_size: Annotated[str, Form()] = "match",
    use_video_audio: Annotated[bool, Form()] = True,
    reference_images: list[UploadFile] = File(default=[]),
    reference_videos: list[UploadFile] = File(default=[]),
    reference_audios: list[UploadFile] = File(default=[]),
    access: dict = Depends(require_video_access),
    target_resolution: Annotated[str, Form()] = "1080p",
    source_resolution_profile: Annotated[str, Form()] = "",
    auto_postprocess: Annotated[bool, Form()] = False,
):
    """直接使用提交的六段式提示词，通过 R2V 工作流生成多模态参考视频。"""
    # ref2v 不支持原生基模：历史参数 nsfw_native=true 仅视为 nsfw=true（回落默认 LoRA 档位）。
    return await _create_reference_video_task_impl(
        prompt, duration, resolution_megapixels, resolution_preset, aspect_ratio, seed,
        ref_image_size, use_video_audio, reference_images, reference_videos, reference_audios,
        polish_prompt_first, access["key_id"],
        _merge_nsfw(nsfw, nsfw_native, nsfw_strength), nsfw_preset,
        target_resolution, source_resolution_profile, auto_postprocess,
    )


def parse_task_ids(raw: str) -> list[str]:
    return list(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))[:500]


def authorized_task(task_id: str, access: dict) -> dict:
    try:
        data = video_service.load_metadata(task_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="视频任务不存在")
    if access["role"] != "admin" and video_service._task_owner(data) != access["key_id"]:
        raise HTTPException(status_code=404, detail="视频任务不存在")
    return data


@router.get(
    "/tasks",
    response_model=VideoTaskList,
    tags=[TAG_TASKS],
    summary="按本地任务 ID 查询历史",
)
async def get_video_tasks(
    task_ids: Annotated[str, Query(description="浏览器本地保存的任务 ID，使用英文逗号分隔")] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    access: dict = Depends(require_video_access),
):
    """管理员可查看全部任务；其他身份（API Key / 注册用户）仅能看到自己创建的任务。

    分页返回：``total`` 为过滤后的任务总数，``limit``/``offset`` 回显请求参数，
    前端据此渲染翻页控件，避免历史任务过多时一次性全量加载。
    """
    if access["role"] == "admin":
        tasks, total = video_service.list_tasks_page([], limit, offset, include_all=True)
    else:
        tasks, total = video_service.list_tasks_page([], limit, offset, owner=access["key_id"])
    return {"tasks": tasks, "total": total, "limit": limit, "offset": offset}


@router.get(
    "/tasks/{task_id}",
    response_model=VideoTaskSummary,
    tags=[TAG_TASKS],
    summary="查询异步任务进度与结果",
)
async def get_video_task(task_id: str, access: dict = Depends(require_video_access)):
    """返回任务当前状态；任务完成后同一响应中的 ``assets.video`` 即为结果代理地址。"""
    try:
        return video_service.public_task(authorized_task(task_id, access))
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="视频任务不存在")


@router.get(
    "/tasks/{task_id}/assets/{asset_name}",
    response_class=FileResponse,
    tags=[TAG_TASKS],
    summary="获取任务输入或输出资源",
)
async def get_video_asset(task_id: str, asset_name: str, access: dict = Depends(require_video_access)):
    authorized_task(task_id, access)
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
async def delete_video_task(task_id: str, access: dict = Depends(require_video_access)):
    """仅允许删除 completed/failed 终态任务；删除后文件不可恢复。"""
    authorized_task(task_id, access)
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
async def get_video_gif(task_id: str, access: dict = Depends(require_video_access)):
    authorized_task(task_id, access)
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
