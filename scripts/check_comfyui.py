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
    COMFYUI_REF2VA_MODEL,
    COMFYUI_SPECTRUM_ENABLED,
    COMFYUI_TEXT_ENCODER,
    COMFYUI_TIMEOUT,
    COMFYUI_VIDEO_VAE,
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
# 采样加速节点仅在 comfyui.spectrum_apply.enabled=true 时才被工作流引用。
if COMFYUI_SPECTRUM_ENABLED:
    REQUIRED_NODES.add("SpectrumApplyMiniMaxH3")
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
