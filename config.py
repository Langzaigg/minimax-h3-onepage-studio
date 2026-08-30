import os
from pathlib import Path
from typing import Any, Callable

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("VIDEO_SERVICE_CONFIG", PROJECT_ROOT / "config.yml")).resolve()


def _load_config() -> dict[str, Any]:
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as error:
        raise RuntimeError(f"配置文件不存在：{CONFIG_PATH}") from error
    if not isinstance(data, dict):
        raise RuntimeError("config.yml 顶层必须是对象")
    # 全部配置（含 API Key、本机路径等私有项）统一放在 config.yml；
    # config.yml 已被 .gitignore 忽略，不随仓库分发。
    # 仓库内仅提供 config.example.yml 作为默认值范例。
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

# 访问模式：
#   admin（默认）= 管理员单用户模式：无需任何凭据，直接以管理员身份访问（全部历史可见）；
#   key    = 密钥模式：使用管理员密钥或 API Key 验证；
#   register = 注册模式：用户名即注册（无密码），各自历史隔离，输入管理员密钥则以管理员身份登录。
ACCESS_MODE = _setting("access.mode", "ACCESS_MODE", "admin")

COMFYUI_BASE_URL = _setting("comfyui.base_url", "COMFYUI_BASE_URL", "http://127.0.0.1:8188")
COMFYUI_TIMEOUT = _setting("comfyui.timeout_seconds", "COMFYUI_TIMEOUT", 60, int)
# ComfyUI 的 input 目录（本机磁盘路径）。配置后每次生成结束会自动删除
# 上传到该目录的临时图片/视频副本（minimax_h3_studio_* 前缀），避免长期堆积。
# 留空则不清理（保持旧行为）。
COMFYUI_INPUT_DIR = _setting("comfyui.input_dir", "COMFYUI_INPUT_DIR", "")
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
# NSFW 档位：fl2v 家族（文/首帧/尾帧/首尾帧）与多模态参考（ref2v）各自一组 LoRA 配方，
# 每个档位可叠加多个 LoRA（按配置顺序串联）。与原生基模档位互斥（二选一）。
# LoRA 档位与 turbo 加速互斥：蒸馏 LoRA 与 NSFW LoRA 不能叠加；
# 原生基模档位不挂 LoRA，因此仍可与 turbo 组合。
# 未配置任何档位时该模式的 NSFW 下拉不渲染，接口传 nsfw=true 返回 400。
# 结构为列表，仅支持 config.yml 配置（不提供环境变量覆盖）。
def _nsfw_presets(path: str) -> list[dict[str, Any]]:
    """解析 NSFW LoRA 档位：每档 = 一组按序叠加的 (lora 文件名, strength)。"""
    presets: list[dict[str, Any]] = []
    for index, item in enumerate(_nested(path, []) or []):
        if not isinstance(item, dict):
            continue
        loras: list[dict[str, Any]] = []
        for entry in item.get("loras") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("lora") or entry.get("name") or "").strip()
            if not name:
                continue
            try:
                strength = float(entry.get("strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            loras.append({"name": name, "strength": strength})
        if not loras:
            continue
        preset_id = str(item.get("id") or f"preset-{index + 1}").strip()
        presets.append(
            {"id": preset_id, "label": str(item.get("label") or preset_id).strip(), "loras": loras}
        )
    return presets


NSFW_PRESETS = {
    "fl2v": _nsfw_presets("comfyui.nsfw.fl2v_presets"),
    "ref2v": _nsfw_presets("comfyui.nsfw.ref2v_presets"),
}

# Turbo 蒸馏 Lora（lightx2v）：fl2v（文/首帧/尾帧/首尾帧）与 ref2v（多模态参考）各自一份。
# 步数直接写入 BasicScheduler.steps（配合 MiniMaxH3SigmaShift 重新生成对应步数的 sigma 网格，
# 不做任何跳步/sigma 子集处理）；shift 参数为官方文档给定的 video/audio 训练偏移。
# lora 文件名留空表示该档未配置，前端不渲染对应分辨率档位。
COMFYUI_TURBO_FL2V_8STEP_LORA = _setting(
    "comfyui.turbo.fl2v_8step_lora", "COMFYUI_TURBO_FL2V_8STEP_LORA", ""
)
COMFYUI_TURBO_FL2V_8STEP_SHIFT = _setting(
    "comfyui.turbo.fl2v_8step_shift", "COMFYUI_TURBO_FL2V_8STEP_SHIFT", "12,3"
)
COMFYUI_TURBO_FL2V_4STEP_LORA = _setting(
    "comfyui.turbo.fl2v_4step_lora", "COMFYUI_TURBO_FL2V_4STEP_LORA", ""
)
COMFYUI_TURBO_FL2V_4STEP_SHIFT = _setting(
    "comfyui.turbo.fl2v_4step_shift", "COMFYUI_TURBO_FL2V_4STEP_SHIFT", "6,3"
)
COMFYUI_TURBO_REF2V_4STEP_LORA = _setting(
    "comfyui.turbo.ref2v_4step_lora", "COMFYUI_TURBO_REF2V_4STEP_LORA", ""
)
COMFYUI_TURBO_REF2V_4STEP_SHIFT = _setting(
    "comfyui.turbo.ref2v_4step_shift", "COMFYUI_TURBO_REF2V_4STEP_SHIFT", "12,3"
)

# 混合基模（Dasiwa MiniMax H3 Hybrid）：一个模型同时覆盖 fl2v（文本/首尾帧）与 ref2v
# （多模态参考）两种能力，作为独立的「混合生成」模式（hybrid_to_video）出现。
#   hybrid_diffusion       → 20 步版本（Non-Distilled），配合采样加速节点跳步；
#   hybrid_turbo_diffusion → 蒸馏版本（Distilled），4 / 8 步直接采样，不跳步。
# 任一文件名留空即禁用对应档位（前端不渲染该模式/档位）。
COMFYUI_HYBRID_MODEL = _setting(
    "comfyui.models.hybrid_diffusion",
    "COMFYUI_HYBRID_MODEL",
    "",
)
COMFYUI_HYBRID_TURBO_MODEL = _setting(
    "comfyui.models.hybrid_turbo_diffusion",
    "COMFYUI_HYBRID_TURBO_MODEL",
    "",
)
HYBRID_ENABLED = bool(COMFYUI_HYBRID_MODEL)
HYBRID_TURBO_ENABLED = bool(COMFYUI_HYBRID_TURBO_MODEL)
# 混合模式普通档（Non-Distilled）采样参数：
# 推荐 res_multistep/simple 或 euler/simple，Shift Video 10-12，Shift Audio 3-5；
# 默认固定 25 步，配合 Spectrum 加速节点跳步。
HYBRID_STEPS = _setting("comfyui.hybrid.steps", "COMFYUI_HYBRID_STEPS", 25, int)
HYBRID_SAMPLER = _setting("comfyui.hybrid.sampler", "COMFYUI_HYBRID_SAMPLER", "res_multistep")
HYBRID_SCHEDULER = _setting("comfyui.hybrid.scheduler", "COMFYUI_HYBRID_SCHEDULER", "simple")
HYBRID_SHIFT_VIDEO = _setting("comfyui.hybrid.shift_video", "COMFYUI_HYBRID_SHIFT_VIDEO", 12.0, float)
HYBRID_SHIFT_AUDIO = _setting("comfyui.hybrid.shift_audio", "COMFYUI_HYBRID_SHIFT_AUDIO", 3.0, float)
# 混合模式 turbo 加速（Distilled，前端可选开关）：固定 8 步直接采样、不挂 Spectrum，
# 分辨率档位不锁定，跟随用户所选档位。
HYBRID_TURBO_STEPS = _setting("comfyui.hybrid.turbo_steps", "COMFYUI_HYBRID_TURBO_STEPS", 8, int)
HYBRID_TURBO_SAMPLER = _setting(
    "comfyui.hybrid.turbo_sampler", "COMFYUI_HYBRID_TURBO_SAMPLER", "euler"
)
HYBRID_TURBO_SCHEDULER = _setting(
    "comfyui.hybrid.turbo_scheduler", "COMFYUI_HYBRID_TURBO_SCHEDULER", "simple"
)
HYBRID_TURBO_SHIFT_VIDEO = _setting(
    "comfyui.hybrid.turbo_shift_video", "COMFYUI_HYBRID_TURBO_SHIFT_VIDEO", 12.0, float
)
HYBRID_TURBO_SHIFT_AUDIO = _setting(
    "comfyui.hybrid.turbo_shift_audio", "COMFYUI_HYBRID_TURBO_SHIFT_AUDIO", 3.0, float
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
# 思考模式力度（reasoning effort）：low / medium / high；留空表示不发送该参数，
# 同时回退为 chat_template_kwargs.enable_thinking=false（本地 vLLM 风格部署）。
LLM_REASONING_EFFORT = _setting(
    "llm.reasoning_effort", "LLM_REASONING_EFFORT", "low"
).strip().lower()
# NSFW 破甲提示词开关：true 时润色系统提示词追加成人向放行指令，
# 由该全局配置单独控制，与任务 NSFW 开关无关（NSFW 风格指南仍按任务开关注入）。
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
