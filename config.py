import os
from pathlib import Path
from typing import Any, Callable

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("VIDEO_SERVICE_CONFIG", PROJECT_ROOT / "config.yml")).resolve()


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 中的 dict 与 base 同名键逐层合并，其余覆盖。"""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_config() -> dict[str, Any]:
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as error:
        raise RuntimeError(f"配置文件不存在：{CONFIG_PATH}") from error
    if not isinstance(data, dict):
        raise RuntimeError("config.yml 顶层必须是对象")
    # config.local.yml 与本机私密覆盖项（已被 .gitignore 忽略），
    # 逐层合并到 config.yml 之上，用于存放 API Key、本机路径等不提交的配置。
    local_path = CONFIG_PATH.with_name("config.local.yml")
    if local_path.is_file():
        local_data = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        if not isinstance(local_data, dict):
            raise RuntimeError("config.local.yml 顶层必须是对象")
        data = _deep_merge(data, local_data)
    return data


CONFIG = _load_config()


def _nested(path: str, default: Any = None) -> Any:
    value: Any = CONFIG
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def _setting(path: str, env_name: str, default: Any, cast: Callable[[Any], Any] = str) -> Any:
    raw = os.getenv(env_name)
    if raw is None:
        raw = _nested(path, default)
    try:
        return cast(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"配置项 {path} / {env_name} 格式无效：{raw!r}") from error


def _project_path(path: str, env_name: str, default: str) -> str:
    raw = Path(_setting(path, env_name, default))
    return str((raw if raw.is_absolute() else PROJECT_ROOT / raw).resolve())


SERVICE_RESTART_HOST = _setting("server.host", "SERVICE_RESTART_HOST", "0.0.0.0")
SERVICE_RESTART_PORT = _setting("server.port", "SERVICE_RESTART_PORT", 7788, int)
SERVICE_ADMIN_KEY = _setting("server.admin_key", "SERVICE_ADMIN_KEY", "admin")
ACCESS_SESSION_MAX_AGE_SECONDS = _setting(
    "server.session_max_age_seconds", "ACCESS_SESSION_MAX_AGE_SECONDS", 31536000, int
)
SERVICE_RESTART_DELAY_SECONDS = _setting(
    "server.restart_delay_seconds", "SERVICE_RESTART_DELAY_SECONDS", 1.5, float
)
SERVICE_GIT_PULL_TIMEOUT = _setting(
    "server.git_pull_timeout_seconds", "SERVICE_GIT_PULL_TIMEOUT", 120, int
)

# 访问模式：key = 密钥模式（默认，管理员/API Key）；register = 注册模式（用户名即注册，无密码）。
ACCESS_MODE = _setting("access.mode", "ACCESS_MODE", "key")

COMFYUI_BASE_URL = _setting("comfyui.base_url", "COMFYUI_BASE_URL", "http://127.0.0.1:8188")
COMFYUI_TIMEOUT = _setting("comfyui.timeout_seconds", "COMFYUI_TIMEOUT", 60, int)
COMFYUI_FL2VA_MODEL = _setting(
    "comfyui.models.fl2va_diffusion",
    "COMFYUI_FL2VA_MODEL",
    "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
)
COMFYUI_REF2VA_MODEL = _setting(
    "comfyui.models.ref2va_diffusion",
    "COMFYUI_REF2VA_MODEL",
    "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
)
COMFYUI_TEXT_ENCODER = _setting(
    "comfyui.models.text_encoder",
    "COMFYUI_TEXT_ENCODER",
    "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
)
# 采样加速（Spectrum Apply MiniMax H3）默认不启用；仅在 comfyui.spectrum_apply.enabled=true
# 时工作流才引用该节点，关闭时工作流完全移除节点，ComfyUI 侧无需安装对应依赖。
COMFYUI_SPECTRUM_ENABLED = _setting(
    "comfyui.spectrum_apply.enabled", "COMFYUI_SPECTRUM_ENABLED", False, bool
)
# ffmpeg 可执行文件路径；留空时前端不提供视频转 GIF 下载。
FFMPEG_PATH = _setting("ffmpeg.path", "FFMPEG_PATH", "")
COMFYUI_VIDEO_VAE = _setting(
    "comfyui.models.video_vae",
    "COMFYUI_VIDEO_VAE",
    "minimax_h3_video_vae_fp16.safetensors",
)
COMFYUI_AUDIO_VAE = _setting(
    "comfyui.models.audio_vae",
    "COMFYUI_AUDIO_VAE",
    "minimax_h3_audio_vae_fp32.safetensors",
)

LLM_BASE_URL = _setting("llm.base_url", "LLM_BASE_URL", "http://127.0.0.1:8000/v1")
LLM_API_KEY = _setting("llm.api_key", "LLM_API_KEY", "")
VIDEO_MODEL = _setting("llm.model", "LLM_MODEL", "vision-model")
LLM_TIMEOUT = _setting("llm.timeout_seconds", "LLM_TIMEOUT", 180, int)
LLM_CONTEXT_WINDOW = _setting("llm.context_window", "LLM_CONTEXT_WINDOW", 131000, int)
LLM_CONTEXT_MARGIN_TOKENS = _setting(
    "llm.context_margin_tokens", "LLM_CONTEXT_MARGIN_TOKENS", 512, int
)
LLM_MIN_COMPLETION_TOKENS = _setting(
    "llm.min_completion_tokens", "LLM_MIN_COMPLETION_TOKENS", 512, int
)
# NSFW 破甲提示词：开启后润色系统提示词追加成人向放行指令（默认关闭，
# 仅影响润色阶段，不体现在网页上）。
LLM_NSFW_ARMOR = _setting("llm.nsfw_armor", "LLM_NSFW_ARMOR", False, bool)

VIDEO_DEFAULT_DURATION = _setting("video.duration.default", "VIDEO_DEFAULT_DURATION", 5, float)
VIDEO_MIN_DURATION = _setting("video.duration.min", "VIDEO_MIN_DURATION", 5, float)
VIDEO_MAX_DURATION = _setting("video.duration.max", "VIDEO_MAX_DURATION", 15, float)
VIDEO_DEFAULT_MEGAPIXELS = _setting(
    "video.resolution_megapixels.default", "VIDEO_DEFAULT_MEGAPIXELS", 0.4, float
)
VIDEO_MIN_MEGAPIXELS = _setting(
    "video.resolution_megapixels.min", "VIDEO_MIN_MEGAPIXELS", 0.2, float
)
VIDEO_MAX_MEGAPIXELS = _setting(
    "video.resolution_megapixels.max", "VIDEO_MAX_MEGAPIXELS", 1.0, float
)

VIDEO_DATA_FOLDER = _project_path("storage.task_data_dir", "VIDEO_DATA_FOLDER", "video_data")
VIDEO_WORKFLOW_FOLDER = _project_path("storage.workflow_dir", "VIDEO_WORKFLOW_FOLDER", "video_workflows")
ACCESS_DATA_FOLDER = _project_path("storage.access_data_dir", "ACCESS_DATA_FOLDER", "access_data")
HISTORY_RETENTION_DAYS = _setting("storage.retention_days", "HISTORY_RETENTION_DAYS", 90, int)
WEEKLY_CLEANUP_WEEKDAY = _setting(
    "storage.weekly_cleanup.weekday", "WEEKLY_CLEANUP_WEEKDAY", 6, int
)
WEEKLY_CLEANUP_HOUR = _setting("storage.weekly_cleanup.hour", "WEEKLY_CLEANUP_HOUR", 3, int)
DAILY_STORAGE_CHECK_HOUR = _setting(
    "storage.daily_disk_check.hour", "DAILY_STORAGE_CHECK_HOUR", 0, int
)
DAILY_STORAGE_CHECK_MINUTE = _setting(
    "storage.daily_disk_check.minute", "DAILY_STORAGE_CHECK_MINUTE", 5, int
)
STORAGE_USAGE_THRESHOLD_PERCENT = _setting(
    "storage.usage_threshold_percent", "STORAGE_USAGE_THRESHOLD_PERCENT", 90, float
)


def ensure_directories() -> None:
    for folder in (VIDEO_DATA_FOLDER, VIDEO_WORKFLOW_FOLDER, ACCESS_DATA_FOLDER):
        Path(folder).mkdir(parents=True, exist_ok=True)
