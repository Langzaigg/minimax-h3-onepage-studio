import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from config import (
    DAILY_STORAGE_CHECK_HOUR,
    DAILY_STORAGE_CHECK_MINUTE,
    HISTORY_RETENTION_DAYS,
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
from routers import access, admin, video
from services import access_control
from services.storage_maintenance import cleanup_expired_history, emergency_cleanup_if_needed
from services.video_generation import resume_incomplete_tasks, shutdown_background_tasks


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
    jobs = [asyncio.create_task(weekly_history_cleanup()), asyncio.create_task(daily_storage_check())]
    try:
        yield
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await shutdown_background_tasks()


app = FastAPI(
    title="MiniMax H3 一页式创作台",
    description="带访问控制、提示词润色、异步历史和资源代理的独立视频生成 API",
    version="1.0.0",
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
    ],
)

BUSINESS_API_TAGS = {video.TAG_WORKFLOWS, video.TAG_POLISH, video.TAG_TASKS}


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
        }
    }


@app.get("/api/health", tags=["系统"])
async def health_check():
    return {"status": "ok", "docs": "/docs"}
