import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import (
    ACCESS_MODE,
    DAILY_STORAGE_CHECK_HOUR,
    DAILY_STORAGE_CHECK_MINUTE,
    FFMPEG_PATH,
    HISTORY_RETENTION_DAYS,
    NSFW_PRESETS,
    PROJECT_ROOT,
    VIDEO_DEFAULT_DURATION,
    VIDEO_DEFAULT_MEGAPIXELS,
    VIDEO_MAX_DURATION,
    VIDEO_MAX_MEGAPIXELS,
    VIDEO_MIN_DURATION,
    VIDEO_MIN_MEGAPIXELS,
    WEEKLY_CLEANUP_HOUR,
    WEEKLY_CLEANUP_WEEKDAY,
    ensure_directories,
)
from routers import access, admin, video, studio, workspaces
from services import studio_tasks
from services import access_control
from services.storage_maintenance import cleanup_expired_history, emergency_cleanup_if_needed
from services.video_generation import resume_incomplete_tasks, shutdown_background_tasks
from services import video_generation as video_service


def seconds_until_weekly_cleanup(now: datetime) -> float:
    days = (WEEKLY_CLEANUP_WEEKDAY - now.weekday()) % 7
    target = (now + timedelta(days=days)).replace(
        hour=WEEKLY_CLEANUP_HOUR, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


def seconds_until_daily_storage_check(now: datetime) -> float:
    target = now.replace(
        hour=DAILY_STORAGE_CHECK_HOUR,
        minute=DAILY_STORAGE_CHECK_MINUTE,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def weekly_history_cleanup() -> None:
    while True:
        await asyncio.sleep(seconds_until_weekly_cleanup(datetime.now()))
        result = await asyncio.to_thread(cleanup_expired_history, HISTORY_RETENTION_DAYS)
        print(f"[{datetime.now()}] Weekly history cleanup: {result}")


async def daily_storage_check() -> None:
    while True:
        await asyncio.sleep(seconds_until_daily_storage_check(datetime.now()))
        result = await asyncio.to_thread(emergency_cleanup_if_needed)
        print(f"[{datetime.now()}] Daily storage check: {result}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_directories()
    await resume_incomplete_tasks()
    await studio_tasks.resume()
    from services.auto_postprocess import resume_completed_tasks
    await resume_completed_tasks()
    jobs = [asyncio.create_task(weekly_history_cleanup()), asyncio.create_task(daily_storage_check())]
    try:
        yield
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await shutdown_background_tasks()
        await studio_tasks.shutdown()


app = FastAPI(
    title="兔酱拍片工作室",
    description="Qwen 参考图、MiniMax H3 视频、LTX 2× 二采与 DLSS 5 滤镜，支持素材工作区和任务复用",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    swagger_ui_parameters={"docExpansion": "none", "filter": True},
    openapi_tags=[
        {"name": video.TAG_WORKFLOWS, "description": "两种异步工作流；polish_prompt_first 控制是否先润色。"},
        {"name": video.TAG_POLISH, "description": "使用多模态模型单独润色视频提示词。"},
        {"name": video.TAG_TASKS, "description": "按不可预测任务 ID 查询进度、结果和代理资源。"},
        {"name": "访问验证", "description": "管理员密钥或 API Key 验证。"},
        {"name": "系统运维", "description": "API Key、统计及更新重启。"},
        {"name": "系统", "description": "健康检查和非敏感前端配置。"},
        {"name": "创作流程", "description": "参考图、视频后处理、跨步骤素材及可恢复任务。"},
    ],
)

BUSINESS_API_TAGS = {video.TAG_WORKFLOWS, video.TAG_POLISH, video.TAG_TASKS, "创作流程"}


@app.middleware("http")
async def record_business_api_usage(request, call_next):
    response = await call_next(request)
    route = request.scope.get("route")
    tags = set(getattr(route, "tags", []) or [])
    if response.status_code < 400 and request.url.path.startswith("/api/") and tags & BUSINESS_API_TAGS:
        try:
            access_info = access_control.identify_access(request.headers, request.cookies, touch=True)
            key_id = access_info["key_id"] if access_info else access_control.ANONYMOUS_KEY_ID
            endpoint = f"{request.method} {getattr(route, 'path', request.url.path)}"
            await asyncio.to_thread(access_control.record_api_call, endpoint, key_id)
        except Exception as error:
            print(f"API usage statistics write failed: {error}")
    return response


app.include_router(admin.router)
app.include_router(access.router)
app.include_router(video.router)
app.include_router(studio.router)
app.include_router(workspaces.router)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static", check_dir=False), name="static")


def read_html_template(filename: str) -> str:
    return (PROJECT_ROOT / "templates" / filename).read_text(encoding="utf-8")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/video-generate", status_code=307)


@app.get("/video-generate", response_class=HTMLResponse, include_in_schema=False)
async def video_generate_page():
    return read_html_template("video_generate.html")


@app.get("/admin-console", response_class=HTMLResponse, include_in_schema=False)
async def admin_console_page():
    return read_html_template("admin_console.html")


@app.get("/api/config", tags=["系统"])
async def frontend_config():
    return {
        "video_generation_defaults": {
            "duration": VIDEO_DEFAULT_DURATION,
            "duration_min": VIDEO_MIN_DURATION,
            "duration_max": VIDEO_MAX_DURATION,
            "resolution_megapixels": VIDEO_DEFAULT_MEGAPIXELS,
            "resolution_megapixels_min": VIDEO_MIN_MEGAPIXELS,
            "resolution_megapixels_max": VIDEO_MAX_MEGAPIXELS,
        },
        "ffmpeg_enabled": bool(FFMPEG_PATH),
        "access_mode": ACCESS_MODE,
        # NSFW 档位：fl2v 家族与多模态参考（ref2v）各自一组 LoRA 配方。
        # 混合模式（Dasiwa Hybrid）不叠加 NSFW LoRA（基模自带能力），无档位可渲染。
        "nsfw_presets": {
            "fl2v": [{"id": preset["id"], "label": preset["label"]} for preset in NSFW_PRESETS["fl2v"]],
            "ref2v": [
                {"id": preset["id"], "label": preset["label"]} for preset in NSFW_PRESETS["ref2v"]
            ],
        },
        # turbo 蒸馏档位可用性：按模式返回可用档位 id，供前端渲染分辨率下拉。
        "turbo_presets": {
            "fl2v": video_service.turbo_preset_ids("text_to_video"),
            "ref2v": video_service.turbo_preset_ids("reference_to_video"),
            "hybrid": video_service.turbo_preset_ids("hybrid_to_video"),
        },
        # 混合生成模式（Dasiwa Hybrid）能力开关：基模配置存在即可用。
        "hybrid_enabled": bool(video_service.COMFYUI_HYBRID_MODEL),
        "hybrid_presets": video_service.hybrid_presets(),
        "hybrid_turbo_enabled": bool(video_service.COMFYUI_HYBRID_TURBO_MODEL),
        "hybrid_steps": video_service.HYBRID_STEPS,
        "hybrid_sampler": video_service.HYBRID_SAMPLER,
        "hybrid_scheduler": video_service.HYBRID_SCHEDULER,
        "hybrid_shift_video": video_service.HYBRID_SHIFT_VIDEO,
        "hybrid_shift_audio": video_service.HYBRID_SHIFT_AUDIO,
        "hybrid_turbo_sampler": video_service.HYBRID_TURBO_SAMPLER,
        "hybrid_turbo_scheduler": video_service.HYBRID_TURBO_SCHEDULER,
        "hybrid_turbo_shift_video": video_service.HYBRID_TURBO_SHIFT_VIDEO,
        "hybrid_turbo_shift_audio": video_service.HYBRID_TURBO_SHIFT_AUDIO,
    }


@app.get("/api/health", tags=["系统"])
async def health_check():
    return {"status": "ok", "docs": "/docs"}
