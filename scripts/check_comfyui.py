import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    COMFYUI_AUDIO_VAE,
    COMFYUI_BASE_URL,
    COMFYUI_FL2VA_MODEL,
    COMFYUI_HYBRID_MODEL,
    COMFYUI_HYBRID_TURBO_MODEL,
    COMFYUI_REF2VA_MODEL,
    COMFYUI_SPECTRUM_ENABLED,
    COMFYUI_TEXT_ENCODER,
    COMFYUI_TIMEOUT,
    COMFYUI_TURBO_FL2V_4STEP_LORA,
    COMFYUI_TURBO_FL2V_8STEP_LORA,
    COMFYUI_TURBO_REF2V_4STEP_LORA,
    COMFYUI_VIDEO_VAE,
    NSFW_PRESETS,
)


REQUIRED_NODES = {
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
    "LoadImage",
    "ImageScale",
    "LoadVideo",
    "GetVideoComponents",
    "LoadAudio",
    "CreateVideo",
    "SaveVideo",
}
# 采样加速节点仅在 comfyui.spectrum_apply.enabled=true 时才被工作流引用（turbo 工作流不用）。
if COMFYUI_SPECTRUM_ENABLED:
    REQUIRED_NODES.add("SpectrumApplyMiniMaxH3")
# turbo 蒸馏工作流（lightx2v lora）需要 SigmaShift 与 LoraLoader 节点。
TURBO_LORAS = [
    lora for lora in (
        COMFYUI_TURBO_FL2V_8STEP_LORA,
        COMFYUI_TURBO_FL2V_4STEP_LORA,
        COMFYUI_TURBO_REF2V_4STEP_LORA,
    ) if lora
]
if TURBO_LORAS:
    REQUIRED_NODES.update({"MiniMaxH3SigmaShift", "LoraLoaderModelOnly"})
# NSFW 档位（fl2v / ref2v 各组）走 LoraLoaderModelOnly，仅普通工作流生效（与 turbo 互斥）。
NSFW_LORAS = {
    lora["name"]
    for presets in NSFW_PRESETS.values()
    for preset in presets
    for lora in preset["loras"]
}
if NSFW_LORAS:
    REQUIRED_NODES.add("LoraLoaderModelOnly")
REQUIRED_MODELS = {
    "diffusion_models": {
        COMFYUI_FL2VA_MODEL,
        COMFYUI_REF2VA_MODEL,
    },
    "text_encoders": {COMFYUI_TEXT_ENCODER},
    "vae": {
        COMFYUI_VIDEO_VAE,
        COMFYUI_AUDIO_VAE,
    },
}
# Lora 位于 ComfyUI models/loras 目录：turbo 蒸馏 lora 与全部 NSFW 档位 lora。
REQUIRED_MODELS["loras"] = set(TURBO_LORAS) | NSFW_LORAS
# 混合生成基模（Dasiwa Hybrid）：普通档 Non-Distilled（25 步）与 turbo Distilled 各一个，
# 与常规模型同在 diffusion_models；配置了哪个就校验哪个。
if COMFYUI_HYBRID_MODEL:
    REQUIRED_MODELS["diffusion_models"].add(COMFYUI_HYBRID_MODEL)
if COMFYUI_HYBRID_TURBO_MODEL:
    REQUIRED_MODELS["diffusion_models"].add(COMFYUI_HYBRID_TURBO_MODEL)
# 混合模式普通档（25 步）工作流串联 Spectrum 加速节点（与 fl2v 普通工作流一致）。
if COMFYUI_HYBRID_MODEL and COMFYUI_SPECTRUM_ENABLED:
    REQUIRED_NODES.add("SpectrumApplyMiniMaxH3")
# 混合模式 turbo 工作流需要 SigmaShift 节点（蒸馏档位重建 sigma 网格）。
if COMFYUI_HYBRID_TURBO_MODEL:
    REQUIRED_NODES.add("MiniMaxH3SigmaShift")


def get_json(session: requests.Session, path: str):
    response = session.get(f"{COMFYUI_BASE_URL.rstrip('/')}{path}", timeout=COMFYUI_TIMEOUT)
    response.raise_for_status()
    return response.json()


def main() -> int:
    session = requests.Session()
    session.trust_env = False
    missing: list[str] = []
    for node in sorted(REQUIRED_NODES):
        body = get_json(session, f"/object_info/{node}")
        if node not in body:
            missing.append(f"节点：{node}")
    for folder, expected in REQUIRED_MODELS.items():
        available = set(get_json(session, f"/models/{folder}"))
        missing.extend(f"模型：models/{folder}/{name}" for name in sorted(expected - available))
    queue = get_json(session, "/queue")
    print(f"ComfyUI: {COMFYUI_BASE_URL}")
    print(
        f"队列：运行 {len(queue.get('queue_running', []))}，"
        f"等待 {len(queue.get('queue_pending', []))}"
    )
    if missing:
        print("缺少以下依赖：")
        print("\n".join(f"- {item}" for item in missing))
        return 1
    print("节点、模型和队列接口检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
