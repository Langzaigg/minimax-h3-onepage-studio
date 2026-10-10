"""Local video finishing. ComfyUI owns GPU work; FFmpeg owns timing and muxing.

The LTX paths are *resampling* passes, not renamed resize filters. LTX 2.5 is
available through publisher mirrors whose artifact hashes match Hugging Face.
DLSS NR is an experimental community bridge, never advertised as official DLSS 5.
"""
from __future__ import annotations

import json
import hashlib
from fractions import Fraction
from bisect import bisect_right
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from typing import Callable

import requests
from services.delivery_profiles import TARGET_RESOLUTIONS as OUTPUT_RESOLUTIONS, target_size as output_size

ROOT = Path(__file__).resolve().parents[2]
COMFY_ROOT = ROOT / "ComfyUI"
MODEL_ROOT = COMFY_ROOT / "models"
OUTPUT_WIDTH, OUTPUT_HEIGHT = 1920, 1080
SOURCE_FPS = 24
DEFAULT_PRESET = "redgraft_msr"
ACTIVE_PRESETS = frozenset({"redgraft_msr", "dlss_nr"})
# Lightricks/LTX-2 a95ab856bf29407b6b066ede0abe1846050db56c,
# packages/ltx-pipelines/src/ltx_pipelines/utils/constants.py.
# These are the actual distilled schedules, not the unrelated REDGraft I2V
# or AiMetatron community schedules previously attributed to this pipeline.
LTX_DISTILLED_SIGMAS = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
LTX_STAGE_2_SIGMAS = "0.909375, 0.725, 0.421875, 0.0"
WORKFLOW_REVISIONS = {"ltx25_pixel_2stage": 2, "redgraft_pixel_2stage": 2,
                      "ltx25_msr": 3, "redgraft_msr": 3, "dlss_nr": 2}
MODEL_DEFAULTS = {
    "h3_model": "DasiwaMinimaxH3_dasiwaHybridV4Turbo_int8.safetensors",
    "h3_clip": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
    "h3_video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "h3_audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "ltx23_checkpoint": "ltx-2.3-22b-distilled-fp8.safetensors",
    "ltx23_clip": "gemma_3_12B_it_fp4_mixed.safetensors",
    "ltx25_model": "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
    "ltx25_clip": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
    "ltx25_video_vae": "ltx-2.5-video-vae-conv-bf16.safetensors",
    "ltx25_audio_vae": "ltx-2.5-audio-vae-bf16.safetensors",
    "ltx25_pixel_lora": "ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors",
    "ltx25_msr_lora": "LTX-2.5-Licon-MSR-V1.safetensors",
    "ltx25_latent_upscaler": "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
    "redgraft_model": "REDGraft_LTX25_Fast2K_INT8.safetensors",
    "rife_model": "rife_v4.26.safetensors",
    "anime_model": "realesr-animevideov3.pth",
}
SOURCES = {
    "anime_sr": "https://github.com/xinntao/Real-ESRGAN",
    "rtx_vsr": "https://github.com/Comfy-Org/Nvidia_RTX_Nodes_ComfyUI",
    "rtx_loop": "https://huggingface.co/Comfy-Org/frame_interpolation",
    "ltx23_refine": "https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_ltx2_3_flf2v.json",
    "ltx25_refine": "https://huggingface.co/Lightricks/LTX-2.5",
    "ltx25_pixel_2x": "https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler",
    "ltx25_pixel_2stage": "https://github.com/Lightricks/LTX-2/blob/main/packages/ltx-pipelines/src/ltx_pipelines/ic_lora.py",
    "ltx25_msr": "https://github.com/liconstudio/ComfyUI-LTX2.5-MSR/tree/03061c18f8a79d26f3c63931dd8e28f5e5d872a6",
    "redgraft_refine": "https://civitai.com/models/1295569?modelVersionId=3250230",
    "redgraft_pixel_2stage": "https://civitai.com/models/1295569?modelVersionId=3250230",
    "redgraft_msr": "https://civitai.com/models/1295569?modelVersionId=3250230",
    "redgraft_loop": "https://huggingface.co/Comfy-Org/frame_interpolation",
    "h3_refine": "https://github.com/mdkberry/comfyui_workflows/tree/main/workflows_by_model/Minimax-H3",
    "dlss_sr": "https://github.com/HECer/ComfyUI-DLSS5",
    "dlss_nr": "https://github.com/HECer/ComfyUI-DLSS5",
}
PRESETS = {
    "anime_sr": {"label": "AnimeVideo-v3 动漫超分（保留原画）", "resampling": False,
      "description": "官方动漫专用4×网络直接读取原分辨率，再缩到所选720p/1080p/2K；不做扩散重绘。帧率改变时用真实RIFE，默认保留全部原帧。"},
    "rtx_loop": {"label": "RIFE + RTX 二拍循环（旧版对照）", "resampling": False,
      "description": "旧版固定二拍动画方案；本次动漫画质验收未通过，保留供对照。无扩散二采，1–16 秒，需开启循环。"},
    "redgraft_loop": {"label": "REDGraft 整段二采 + RIFE（实验）", "resampling": True,
      "description": "旧版候选被否决；时域VAE已修正，仍可能改变外观，仅保留实验对照。需开启循环，1–16 秒。"},
    "redgraft_refine": {"label": "REDGraft LTX 2.5 低噪精修（实验重绘）", "resampling": True,
        "description": "混合 W4A8 + INT8 量化；扩散细化会改变五官、发丝和服装，当前动漫片不推荐使用。"},
    "redgraft_pixel_2stage": {"label": "REDGraft Pixel 8+3 重绘超分", "resampling": True,
        "description": "REDGraft 混合模型的实验适配；先降至采样画布的1/4作为参考，再做Pixel 8步与latent 2× / 3步重绘。可能改变五官与衣装，不是作者原生I2V图复现。"},
    "ltx23_refine": {"label": "LTX 2.3 二次采样 + RTX 超分", "resampling": True,
        "hide_when_unavailable": True,
        "description": "公开可下载的 FP8 版本；整段低降噪再采样与真实RIFE，保留原音轨。"},
    "rtx_vsr": {"label": "NVIDIA RTX 视频超分", "resampling": False,
        "description": "NVIDIA VFX SDK 超分，优先保持画风；不进行扩散再采样。"},
    "h3_refine": {"label": "H3 二次采样 + RTX 超分", "resampling": True,
        "description": "沿用 H3 模型与角色参考图做低降噪细化。"},
    "ltx25_refine": {"label": "LTX 2.5 二次采样", "resampling": True,
        "hide_when_unavailable": True,
        "description": "RTX 超分后用 LTX 2.5 低降噪细化；与专门训练的 Pixel 超分独立比较。"},
    "ltx25_pixel_2x": {"label": "LTX 2.5 Pixel IC-LoRA 2×", "resampling": True,
        "hide_when_unavailable": True,
        "description": "官方distilled基模与Pixel 2×适配器，整段视频作参考、单阶段8步重绘；可能改变角色细节，画质仍需逐片验收。"},
    "ltx25_pixel_2stage": {"label": "LTX 2.5 Pixel + latent 两阶段", "resampling": True,
        "hide_when_unavailable": True,
        "description": "按官方ICLoRAPipeline适配：参考先降至采样画布的1/4，再做Pixel 8步→latent 2×→基础模型3步；比单阶段丢失更多源细节，可能重绘人物。保留原音轨。"},
    "ltx25_msr": {"label": "LTX 2.5 MSR 角色参考二采", "resampling": True,
        "hide_when_unavailable": True,
        "description": "官方distilled＋MSR V1：固定原分辨率latent 2×，仅复制右下边缘补齐网格，解码去补边后输出所选尺寸。参考作者图，本机适配0.45起始3步低噪；无参考则取原视频首帧。高源分辨率增加资源消耗，仍需逐片验收。"},
    "redgraft_msr": {"label": "REDGraft + MSR（NSFW 特调）", "resampling": True,
        "hide_when_unavailable": True,
        "description": "REDGraft＋MSR V1：与官方版同图同参数，固定原分辨率latent 2×，仅复制右下边缘补齐网格，解码去补边后输出所选尺寸。本机适配0.45起始3步低噪；无参考取首帧。高源分辨率增加资源消耗，混合模型无保真保证。"},
    "dlss_sr": {"label": "DLSS 超分（实验桥接）", "resampling": False,
        "description": "社区 Windows D3D12/VapourSynth 桥接；深度和运动从视频估计。"},
    "dlss_nr": {"label": "DLSS5 滤镜 · 社区 Ada NR", "resampling": False,
        "description": "只做神经滤镜，保持原尺寸与帧率；支持图片、视频和单帧预览。使用社区Ada补丁，不代表NVIDIA官方支持。"},
}


def _models(overrides=None):
    return {**MODEL_DEFAULTS, **(overrides or {})}


def aspect_canvas(source_width, target_width, target_height):
    """Crop/rescale before RTX to an exactly matching rational aspect ratio."""
    divisor = math.gcd(int(target_width), int(target_height))
    unit_w, unit_h = target_width//divisor, target_height//divisor
    count = math.ceil(source_width/unit_w)
    return count*unit_w, count*unit_h


def required_models(preset, overrides=None):
    m = _models(overrides)
    if preset == "dlss_nr":
        return []  # NR-only has no SR network or RIFE stage.
    if preset == "anime_sr":
        return [("upscale_models", m["anime_model"]), ("frame_interpolation", m["rife_model"])]
    if preset == "rtx_loop":
        return [("frame_interpolation", m["rife_model"])]
    if preset == "redgraft_loop":
        return required_models("redgraft_refine", overrides) + [("frame_interpolation", m["rife_model"])]
    if preset.startswith("redgraft_"):
        return required_models(preset.replace("redgraft_", "ltx25_", 1),
                               {**(overrides or {}), "ltx25_model": m["redgraft_model"]})
    if preset == "ltx25_pixel_2stage":
        return required_models("ltx25_pixel_2x", overrides) + [("latent_upscale_models", m["ltx25_latent_upscaler"])]
    requirements = {
        "h3_refine": [("diffusion_models", m["h3_model"]), ("text_encoders", m["h3_clip"]),
                      ("vae", m["h3_video_vae"]), ("vae", m["h3_audio_vae"])],
        "ltx23_refine": [("checkpoints", m["ltx23_checkpoint"]), ("text_encoders", m["ltx23_clip"])],
        "ltx25_refine": [("diffusion_models", m["ltx25_model"]), ("text_encoders", m["ltx25_clip"]),
                         ("vae", m["ltx25_video_vae"]), ("vae", m["ltx25_audio_vae"]),
                         ("latent_upscale_models", m["ltx25_latent_upscaler"]), ("frame_interpolation", m["rife_model"])],
        "ltx25_pixel_2x": [("diffusion_models", m["ltx25_model"]), ("text_encoders", m["ltx25_clip"]),
                           ("vae", m["ltx25_video_vae"]), ("vae", m["ltx25_audio_vae"]),
                           ("loras", m["ltx25_pixel_lora"])],
        "ltx25_msr": [("diffusion_models", m["ltx25_model"]), ("text_encoders", m["ltx25_clip"]),
                      ("vae", m["ltx25_video_vae"]), ("vae", m["ltx25_audio_vae"]),
                      ("loras", m["ltx25_msr_lora"]), ("latent_upscale_models", m["ltx25_latent_upscaler"])],
    }.get(preset, [])
    rife = ("frame_interpolation", m["rife_model"])
    return requirements if rife in requirements else [*requirements, rife]


def _dlss_runtime_status(preset):
    """Expose readiness without sending interpreter paths or configuration to UI."""
    runtime = COMFY_ROOT / "custom_nodes" / "ComfyUI-DLSS5" / "runtime"
    names = ["config.json"] + (["vsdlssnr.dll", "nvngx_dlssnr.dll"] if preset == "dlss_nr"
                                else ["vsdlsssr.dll", "nvngx_dlss.dll"])
    missing = [name for name in names if not (runtime / name).is_file()
               or (name.endswith(".dll") and (runtime / name).stat().st_size < 1024)]
    if "config.json" not in missing:
        try:
            config = json.loads((runtime / "config.json").read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict) or not config.get("python") or not Path(config["python"]).is_file():
                missing.append("VapourSynth Python")
        except (OSError, TypeError, ValueError):
            missing.append("有效的 SR 运行时配置")
    # A saved smoke receipt records actual inference, not merely DLL import.
    verified = False
    try:
        receipt = json.loads((runtime / "verification.json").read_text(encoding="utf-8"))
        proof = receipt.get(preset, {})
        verified = bool(proof.get("success")) and not missing
        for name, signature in proof.get("runtime_files", {}).items():
            stat = (runtime / name).stat()
            verified &= [stat.st_size, stat.st_mtime_ns] == signature
        verified &= bool(proof.get("runtime_files"))
    except (OSError, TypeError, ValueError):
        pass
    return missing, verified


def capabilities(object_info=None, *, model_root=None, model_overrides=None):
    """Availability means prerequisites exist, not that GPU quality was certified."""
    root = Path(model_root or MODEL_ROOT)
    result = []
    for key, description in PRESETS.items():
        graph = build_workflow("probe.mp4", preset=key, model_overrides=model_overrides)
        needed_nodes = sorted({n["class_type"] for n in graph.values()})
        if key == "dlss_nr":
            needed_nodes = sorted(set(needed_nodes) | {"LoadImage", "SaveImage"})
        if key == "redgraft_loop":
            needed_nodes = sorted(set(needed_nodes) | {n["class_type"] for n in
                build_loop_finish_workflow("probe.mp4", model_overrides=model_overrides).values()})
        if key not in ("anime_sr", "rtx_loop", "redgraft_loop", "dlss_nr"):
            needed_nodes = sorted(set(needed_nodes) | {n["class_type"] for n in
                build_timeline_workflow("probe.mp4", source_frames=49, source_fps=24, model_overrides=model_overrides).values()})
        missing_nodes = [n for n in needed_nodes if object_info is not None and n not in object_info]
        missing_models = [f"{folder}/{name}" for folder, name in required_models(key, model_overrides)
                          if not (root / folder / name).is_file() or (root / folder / name).stat().st_size < 1024]
        runtime_missing = []
        inference_verified = False
        if key.startswith("dlss_"):
            runtime_missing, inference_verified = _dlss_runtime_status(key)
        # Keep an installed experimental DLL distinct from an inference-tested
        # build. A changed DLL invalidates the saved smoke result immediately.
        display_description = dict(description)
        if key == "dlss_nr":
            display_description["controls_contract_version"] = 2 if object_info and "StudioDLSS5Filter" in object_info else 1
            display_description["experimental_range_supported"] = display_description["controls_contract_version"] == 2
            display_description["control_ranges"] = {name: {"min": 0, "max": 1, "experimental_max": 5,
                "runtime_default": fallback} for name, fallback in
                (("output_blend", 1), ("local_tone", 1), ("local_structure", 1), ("skin_mask", -1))}
            display_description["description"] += (
                "已通过本机短片推理。" if inference_verified
                else "尚未完成本机推理验证。")
        reasons = []
        if object_info is None:
            reasons.append("尚未检查 ComfyUI 节点")
        if missing_nodes:
            reasons.append("缺少节点：" + ", ".join(missing_nodes))
        if missing_models:
            reasons.append("缺少模型：" + ", ".join(missing_models))
        if runtime_missing:
            reasons.append("实验桥接运行时未就绪：" + ", ".join(runtime_missing))
            if key == "dlss_nr":
                reasons.append("RTX 40 需要社区 Ada 兼容运行时；SR 与 NR 是独立功能")
        result.append({"id": key, **display_description, "source": SOURCES[key],
                       "available": object_info is not None and not (missing_nodes or missing_models or runtime_missing),
                       "missing_nodes": missing_nodes, "missing_models": missing_models,
                       "missing_runtime": runtime_missing, "reason": "；".join(reasons),
                       "inference_verified": inference_verified and object_info is not None and not missing_nodes})
    return {"presets": result, "default_preset": DEFAULT_PRESET}


def msr_geometry(source_width, source_height, target_resolution="1080p", *,
                 refine_width=None, refine_height=None):
    """Keep source pixels unchanged, pad right/bottom, and always upscale 2x.

    Delivery size never changes the sampling geometry. Explicit canvas values
    are accepted only when they describe this same native 2x canvas.
    """
    if source_width <= 0 or source_height <= 0 or int(source_width) != source_width or int(source_height) != source_height:
        raise ValueError("MSR source dimensions must be positive integers")
    source = [int(source_width), int(source_height)]
    encode = [(value + 31) // 32 * 32 for value in source]
    canvas = [value * 2 for value in encode]
    if ((refine_width is not None and refine_width != canvas[0]) or
            (refine_height is not None and refine_height != canvas[1])):
        raise ValueError("MSR uses fixed native latent 2x; refinement dimensions must match the padded source")
    return {"mode": "native_latent_2x", "source": source, "encode": encode,
            "padding": {"left": 0, "top": 0, "right": encode[0] - source[0],
                        "bottom": encode[1] - source[1], "mode": "edge_pixel"},
            "latent2x_canvas": canvas, "content2x": [value * 2 for value in source],
            "final": list(output_size(target_resolution))}


def build_workflow(input_filename, prompt="", preset=DEFAULT_PRESET, *, seed=0,
                   filename_prefix="studio/post", reference_image=None, model_overrides=None,
                   frames=97, refine_width=None, refine_height=None, source_width=864, source_height=480,
                   target_resolution="1080p"):
    """Build an API graph. Input must be 24 fps, padded to the chosen model grid.

    LTX accepts 8k+1 frames, H3 17k+5. run_postprocess handles this automatically.
    The sampled canvas follows the model grid; delivery size is explicit.
    """
    if preset not in PRESETS:
        raise ValueError(f"Unknown postprocessing preset: {preset}")
    if preset == "dlss_nr":
        from services.dlss_filter import build_filter_workflow
        return build_filter_workflow(input_filename, filename_prefix=filename_prefix,
                                      source_width=source_width, source_height=source_height)
    target_width, target_height = output_size(target_resolution)
    if preset in ("ltx25_msr", "redgraft_msr"):
        msr_plan = msr_geometry(source_width, source_height, target_resolution,
                                refine_width=refine_width, refine_height=refine_height)
        refine_width, refine_height = msr_plan["latent2x_canvas"]
    else:
        grid = 64 if preset in ("ltx25_pixel_2x", "ltx25_refine", "redgraft_refine") else 32
        refine_width = refine_width or math.ceil(target_width/grid)*grid
        refine_height = refine_height or math.ceil(target_height/grid)*grid
    if refine_width % 32 or refine_height % 32:
        raise ValueError("Refinement dimensions must be multiples of 32")
    m = _models(model_overrides)
    if preset == "anime_sr":
        return build_anime_sr_workflow(input_filename, source_frames=frames, source_fps=24,
                                       filename_prefix=filename_prefix, model_overrides=model_overrides, target_resolution=target_resolution)
    if preset == "rtx_loop":
        return build_rtx_loop_workflow(input_filename, source_frames=frames, duration=(frames-1)/24,
                                       filename_prefix=filename_prefix, model_overrides=model_overrides, target_resolution=target_resolution)
    if preset == "redgraft_loop":
        graph = build_workflow(input_filename, prompt, "redgraft_refine", seed=seed,
            filename_prefix=filename_prefix, model_overrides=model_overrides, frames=frames,
            refine_width=960, refine_height=544, source_width=960, source_height=544, target_resolution=target_resolution)
        # Preserve the source's pixels before the single full-duration pass.
        # Upscaling to delivery resolution happens in a separate queue stage.
        graph.pop("3", None)
        graph["4"] = {"class_type": "ImageScale", "inputs": {"image": ["2", 0],
            "upscale_method": "lanczos", "width": 960, "height": 544, "crop": "center"}}
        graph["28"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": "0.25, 0.15, 0.05, 0.0"}}
        graph["31"]["inputs"].update(width=960, height=544)
        return graph
    if preset.startswith("redgraft_"):
        return build_workflow(input_filename, prompt, preset.replace("redgraft_", "ltx25_", 1),
                               seed=seed, filename_prefix=filename_prefix, reference_image=reference_image,
                               model_overrides={**(model_overrides or {}), "ltx25_model": m["redgraft_model"]},
                               frames=frames, refine_width=refine_width, refine_height=refine_height,
                               source_width=source_width, source_height=source_height, target_resolution=target_resolution)
    if preset == "ltx25_pixel_2stage":
        # Native adaptation of the official ICLoraPipeline: LoRA only in stage 1.
        # Reference is one quarter of the final sampling canvas, not half:
        # 512x288 reference -> 1024x576 Pixel -> 2048x1152 latent -> 1920x1080.
        # This discards native H3 detail and is intentionally not the default.
        first_height = math.ceil(target_height/2/64)*64
        first_width = math.ceil(max(target_width/2, first_height*target_width/target_height)/64)*64
        graph = build_workflow(input_filename, prompt, "ltx25_pixel_2x", seed=seed,
                               filename_prefix=filename_prefix, model_overrides=model_overrides,
                               frames=frames, refine_width=first_width,
                               refine_height=first_height, target_resolution=target_resolution)
        def put(key, kind, **inputs):
            graph[str(key)] = {"class_type": kind, "inputs": inputs}
            return [str(key), 0]
        upscaler = put(35, "LatentUpscaleModelLoader", model_name=m["ltx25_latent_upscaler"])
        upscaled = put(36, "LTXVLatentUpsampler", samples=["32", 2], upscale_model=upscaler, vae=["13", 0])
        av_latent = put(37, "LTXVConcatAVLatent", video_latent=upscaled, audio_latent=["24", 0])
        guider = put(38, "CFGGuider", model=["10", 0], positive=["32", 0], negative=["32", 1], cfg=1.0)
        sigmas = put(39, "ManualSigmas", sigmas=LTX_STAGE_2_SIGMAS)
        noise = put(40, "RandomNoise", noise_seed=int(seed) + 1)
        sampled = put(41, "SamplerCustomAdvanced", noise=noise, guider=guider, sampler=["28", 0],
                      sigmas=sigmas, latent_image=av_latent)
        video = put(42, "LTXVSeparateAVLatent", av_latent=sampled)
        graph["33"]["inputs"]["samples"] = video
        return graph
    graph = {}

    def add(key, kind, **inputs):
        graph[str(key)] = {"class_type": kind, "inputs": inputs}
        return [str(key), 0]

    video = add(1, "LoadVideo", file=str(input_filename))
    add(2, "GetVideoComponents", video=video)
    images, audio = ["2", 0], ["2", 1]
    if preset == "ltx25_msr":
        # The full source video is the initial motion latent. MSR adds a still
        # identity reference; it must not replace the source with an empty latent.
        # Never resize source pixels before VAE encoding. Edge padding is copied
        # only to the right/bottom and removed at twice that size after decode.
        source_images = add(3, "ImagePadKJ", image=images, left=0, top=0,
                            right=msr_plan["padding"]["right"], bottom=msr_plan["padding"]["bottom"],
                            extra_padding=0, pad_mode="edge_pixel", color="0, 0, 0")
        reference = (add(4, "LoadImage", image=reference_image) if reference_image else
                     add(4, "ImageFromBatch", image=images, batch_index=0, length=1))
        model = add(10, "UNETLoader", unet_name=m["ltx25_model"], weight_dtype="default")
        add(11, "ComfyUILTX25MSRICLoRALoader", model=model,
            lora_name=m["ltx25_msr_lora"], strength_model=1.0)
        clip = add(12, "CLIPLoader", clip_name=m["ltx25_clip"], type="ltxv", device="default")
        vae = add(13, "VAELoader", vae_name=m["ltx25_video_vae"])
        audio_vae = add(14, "VAELoader", vae_name=m["ltx25_audio_vae"])
        positive = add(15, "CLIPTextEncode", clip=clip, text=prompt or
            "Image 1 is the character reference. Preserve the source video's character identity, "
            "costume, composition, camera and motion. Clean consistent lines and subtle detail refinement.")
        negative = add(16, "CLIPTextEncode", clip=clip,
                       text="scene cut, changed face, changed costume, new character, flicker, unstable linework, watermark")
        add(17, "LTXVConditioning", positive=positive, negative=negative, frame_rate=SOURCE_FPS)
        encoded = add(20, "VAEEncode", pixels=source_images, vae=vae)
        upscaler = add(21, "LatentUpscaleModelLoader", model_name=m["ltx25_latent_upscaler"])
        upscaled = add(22, "LTXVLatentUpsampler", samples=encoded, upscale_model=upscaler, vae=vae)
        add(23, "ComfyUILTX25MSRMultiReferenceGuide", positive=["17", 0], negative=["17", 1],
            vae=vae, latent=upscaled, pic1=reference, msr_parameters=["11", 1],
            strength=1.0, reference_frames="33", use_tiled_encode=False, tile_size=256, tile_overlap=64)
        encoded_audio = add(24, "VAEEncodeAudio", audio=audio, vae=audio_vae)
        mask = add(25, "SolidMask", value=0.0, width=64, height=64)
        frozen_audio = add(26, "SetLatentNoiseMask", samples=encoded_audio, mask=mask)
        av_latent = add(27, "LTXVConcatAVLatent", video_latent=["23", 2], audio_latent=frozen_audio)
        noise = add(28, "RandomNoise", noise_seed=int(seed))
        guider = add(29, "LTXVDualCFGGuider", model=["11", 0], positive=["23", 0], negative=["23", 1],
                     video_cfg=1.0, audio_cfg=1.0)
        sampler = add(30, "KSamplerSelect", sampler_name="euler")
        # Local H3 short-clip adaptation: the author's .85 start closed speaking
        # mouths. Keep the encoded source latent and lower noise for both bases.
        # This is not the author's original schedule or a long-clip guarantee.
        sigmas = add(31, "ManualSigmas", sigmas="0.45, 0.25, 0.10, 0.0")
        sampled = add(32, "SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler,
                      sigmas=sigmas, latent_image=av_latent)
        video_latent = add(33, "LTXVSeparateAVLatent", av_latent=sampled)
        add(34, "LTXVCropGuides", positive=["23", 0], negative=["23", 1], latent=video_latent)
        decoded = add(35, "VAEDecodeTiled", samples=["34", 2], vae=vae, tile_size=768,
                      overlap=64, temporal_size=4096, temporal_overlap=32)
        content = add(37, "ImageCrop", image=decoded, x=0, y=0,
                      width=msr_plan["content2x"][0], height=msr_plan["content2x"][1])
        delivered = add(36, "ImageScale", image=content, upscale_method="lanczos",
                        width=target_width, height=target_height, crop="center")
        result = add(90, "CreateVideo", images=delivered, audio=audio, fps=["2", 2], bit_depth=8)
        add(91, "SaveVideo", video=result, filename_prefix=filename_prefix, format="mp4",
            **{"format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 14.0})
        return graph
    if preset == "ltx25_pixel_2x":
        # Native equivalent of the official IC-LoRA video workflow. The trained
        # 2x guide is kept at half resolution; it is never pre-upscaled with RTX.
        if refine_width % 64 or refine_height % 64:
            raise ValueError("Pixel IC-LoRA 2x dimensions must be multiples of 64")
        guide = add(3, "ImageScale", image=images, upscale_method="lanczos",
                    width=refine_width // 2, height=refine_height // 2, crop="center")
        model = add(10, "UNETLoader", unet_name=m["ltx25_model"], weight_dtype="default")
        model = add(11, "LoraLoaderModelOnly", model=model, lora_name=m["ltx25_pixel_lora"], strength_model=1.0)
        clip = add(12, "CLIPLoader", clip_name=m["ltx25_clip"], type="ltxv", device="default")
        vae = add(13, "VAELoader", vae_name=m["ltx25_video_vae"])
        audio_vae = add(14, "VAELoader", vae_name=m["ltx25_audio_vae"])
        positive = add(16, "CLIPTextEncode", clip=clip, text=prompt)
        negative = add(17, "ConditioningZeroOut", conditioning=positive)
        add(18, "LTXVConditioning", positive=positive, negative=negative, frame_rate=24)
        latent = add(19, "EmptyLTXVLatentVideo", width=refine_width, height=refine_height, length=frames, batch_size=1)
        parameters = add(20, "GetICLoRAParameters", iclora_model=model)
        add(21, "LTXVAddGuide", positive=["18", 0], negative=["18", 1], vae=vae,
            latent=latent, image=guide, frame_idx=0, strength=1.0, iclora_parameters=parameters)
        encoded_audio = add(22, "VAEEncodeAudio", audio=audio, vae=audio_vae)
        mask = add(23, "SolidMask", value=0.0, width=64, height=64)
        encoded_audio = add(24, "SetLatentNoiseMask", samples=encoded_audio, mask=mask)
        av_latent = add(25, "LTXVConcatAVLatent", video_latent=["21", 2], audio_latent=encoded_audio)
        noise = add(26, "RandomNoise", noise_seed=int(seed))
        guider = add(27, "CFGGuider", model=model, positive=["21", 0], negative=["21", 1], cfg=1.0)
        sampler = add(28, "KSamplerSelect", sampler_name="euler_ancestral")
        sigmas = add(29, "ManualSigmas", sigmas=LTX_DISTILLED_SIGMAS)
        sampled = add(30, "SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler, sigmas=sigmas, latent_image=av_latent)
        video_latent = add(31, "LTXVSeparateAVLatent", av_latent=sampled)
        add(32, "LTXVCropGuides", positive=["21", 0], negative=["21", 1], latent=video_latent)
        images = add(33, "VAEDecodeTiled", samples=["32", 2], vae=vae, tile_size=768,
                     overlap=64, temporal_size=4096, temporal_overlap=32)
        images = add(34, "ImageScale", image=images, upscale_method="lanczos", width=target_width, height=target_height, crop="center")
        result = add(90, "CreateVideo", images=images, audio=audio, fps=24, bit_depth=8)
        add(91, "SaveVideo", video=result, filename_prefix=filename_prefix, format="mp4",
            **{"format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 14.0})
        return graph
    if preset.startswith("dlss_"):
        source_w, source_h = aspect_canvas(source_width, target_width, target_height)
        images = add(3, "ImageScale", image=images, upscale_method="lanczos", width=source_w,
                     height=source_h, crop="center")
        images = add(4, "DLSS5EasyPipeline", image=images, scenario="Short video / best quality",
                     operation="Upscale only" if preset == "dlss_sr" else "Upscale + neural rendering",
                     scale="2x", quality="Quality", look="Neutral / faithful", effect_strength=0.25)
        images = add(5, "ImageScale", image=images, upscale_method="lanczos", width=target_width, height=target_height, crop="center")
    elif preset == "ltx25_refine":
        # PeterDuncan adaptation: learned latent 2x only when the half-canvas
        # does not reduce native pixels. Higher-resolution input uses direct V2V.
        use_latent_2x = source_width <= refine_width//2 and source_height <= refine_height//2
        images = add(4, "ImageScale", image=images, upscale_method="lanczos",
                     width=refine_width//2 if use_latent_2x else refine_width,
                     height=refine_height//2 if use_latent_2x else refine_height, crop="center")
    else:
        w, h = (target_width, target_height) if preset == "rtx_vsr" else (refine_width, refine_height)
        # Crop to the target aspect before VSR, which otherwise stretches pixels.
        source_w, source_h = aspect_canvas(source_width, w, h)
        images = add(3, "ImageScale", image=images, upscale_method="lanczos", width=source_w,
                     height=source_h, crop="center")
        images = add(4, "RTXVideoSuperResolution", images=images, resize_type="target dimensions",
                     **{"resize_type.width": w, "resize_type.height": h}, quality="ULTRA")

    if PRESETS[preset]["resampling"]:
        if preset == "h3_refine":
            model = add(10, "UNETLoader", unet_name=m["h3_model"], weight_dtype="default")
            model = add(11, "MiniMaxH3SigmaShift", model=model, shift_video=12.0, shift_audio=3.0)
            clip = add(12, "CLIPLoader", clip_name=m["h3_clip"], type="minimax", device="default")
            vae = add(13, "VAELoader", vae_name=m["h3_video_vae"])
            audio_vae = add(14, "VAELoader", vae_name=m["h3_audio_vae"])
            ref_inputs = dict(clip=clip, vae=vae, audio_vae=audio_vae, prompt=prompt,
                              width=refine_width, height=refine_height, length=frames, ref_image_size="match")
            if reference_image:
                ref_inputs["ref_images.ref_image_1"] = add(15, "LoadImage", image=reference_image)
            positive = add(16, "MiniMaxH3ReferenceToVideo", **ref_inputs)
            sigmas = "0.45, 0.25, 0.1, 0.0"
        else:
            if preset == "ltx23_refine":
                add(10, "CheckpointLoaderSimple", ckpt_name=m["ltx23_checkpoint"])
                model, vae = ["10", 0], ["10", 2]
                clip = add(12, "LTXAVTextEncoderLoader", text_encoder=m["ltx23_clip"],
                           ckpt_name=m["ltx23_checkpoint"], device="default")
                audio_vae = add(14, "LTXVAudioVAELoader", ckpt_name=m["ltx23_checkpoint"])
            else:
                model = add(10, "UNETLoader", unet_name=m["ltx25_model"], weight_dtype="default")
                clip = add(12, "CLIPLoader", clip_name=m["ltx25_clip"], type="ltxv", device="default")
                vae = add(13, "VAELoader", vae_name=m["ltx25_video_vae"])
                audio_vae = add(14, "VAELoader", vae_name=m["ltx25_audio_vae"])
            positive = add(16, "CLIPTextEncode", clip=clip, text=prompt)
            negative = add(17, "ConditioningZeroOut", conditioning=positive)
            positive = add(18, "LTXVConditioning", positive=positive, negative=negative, frame_rate=24)
            sigmas = "0.4, 0.25, 0.1, 0.0"
        # Controlled roundtrip tests found 32/8 temporal VAE boundaries create
        # periodic ghosting without sampling. LTX keeps full temporal context.
        tile_size, overlap = (512, 64) if preset == "h3_refine" else (2048, 128)
        if preset == "h3_refine":
            encoded = add(20, "VAEEncodeTiled", pixels=images, vae=vae, tile_size=tile_size,
                          overlap=overlap, temporal_size=32, temporal_overlap=8)
        else:
            encoded = add(20, "VAEEncode", pixels=images, vae=vae)
            if preset == "ltx25_refine" and use_latent_2x:
                upscaler = add(50, "LatentUpscaleModelLoader", model_name=m["ltx25_latent_upscaler"])
                encoded = add(51, "LTXVLatentUpsampler", samples=encoded, upscale_model=upscaler, vae=vae)
                first = add(52, "GetImagesFromBatchIndexed", images=images, indexes="0")
                encoded = add(53, "LTXVImgToVideoInplace", vae=vae, image=first, latent=encoded, strength=1., bypass=False)
        encoded_audio = add(21, "VAEEncodeAudio", audio=audio, vae=audio_vae)
        mask = add(22, "SolidMask", value=0.0, width=64, height=64)
        encoded_audio = add(23, "SetLatentNoiseMask", samples=encoded_audio, mask=mask)
        av_latent = add(24, "LTXVConcatAVLatent", video_latent=encoded, audio_latent=encoded_audio)
        noise = add(25, "RandomNoise", noise_seed=int(seed))
        guider = add(26, "BasicGuider", model=model, conditioning=positive)
        sampler = add(27, "KSamplerSelect", sampler_name="euler")
        schedule = add(28, "ManualSigmas", sigmas=sigmas)
        sampled = add(29, "SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler,
                      sigmas=schedule, latent_image=av_latent)
        if preset == "ltx25_refine":
            add(26, "LTXVDualCFGGuider", model=model, positive=["18", 0], negative=["18", 1], video_cfg=1., audio_cfg=1.)
            add(27, "KSamplerSelect", sampler_name="euler_ancestral")
            add(28, "BasicScheduler", model=model, scheduler="simple", steps=4, denoise=.15)
        if preset != "h3_refine":
            sampled = add(54, "LTXVSeparateAVLatent", av_latent=sampled)
        images = add(30, "VAEDecodeTiled", samples=sampled, vae=vae,
                     tile_size=tile_size if preset == "h3_refine" else 768,
                     overlap=overlap if preset == "h3_refine" else 64,
                     temporal_size=32 if preset == "h3_refine" else 4096,
                     temporal_overlap=8 if preset == "h3_refine" else 32)
        images = add(31, "ImageScale", image=images, upscale_method="lanczos", width=target_width, height=target_height, crop="center")
    result = add(90, "CreateVideo", images=images, audio=audio, fps=24, bit_depth=8)
    add(91, "SaveVideo", video=result, filename_prefix=filename_prefix, format="mp4",
        **{"format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 14.0})
    return graph


def loop_frame_plan(*, source_frames, duration, fps=30, cadence_stride=2):
    """Close a 24-fps anime clip at its 12-fps pose cadence, then interpolate.

    Native RIFE samples five substeps for 30-fps output, two for 24-fps.
    The usual integer/half-second durations have exact uniform sample positions;
    other durations use the nearest generated substep, never duplicate an index.
    """
    if fps not in (24, 30) or not 1 <= duration <= 16 or source_frames < 24:
        raise ValueError("稳定循环仅支持 1–16 秒、24/30 fps 的短片")
    if not .8 <= duration / (source_frames / 24) <= 1.2:
        raise ValueError("稳定循环时长需接近原视频（0.8–1.2 倍）；请调整循环目标时长")
    if cadence_stride == 1:
        return video_timeline_plan(source_frames=source_frames, source_fps=24, fps=fps,
                                   loop_seconds=duration, cadence_stride=1)
    if cadence_stride != 2:
        raise ValueError("cadence_stride must be 1 or 2")
    span = min(int(source_frames), round(duration * 24))
    anchors = list(range(0, span, 2))
    multiplier = 5 if fps == 30 else 2
    intervals = len(anchors) * multiplier
    count = round(duration * fps)
    selection = [round(index * intervals / count) for index in range(count)]
    if len(set(selection)) != count:
        raise ValueError("循环时长过长，会产生重复帧，请缩短目标时长")
    return {"anchors": anchors + [0], "multiplier": multiplier,
            "selection": selection, "output_frames": count, "duration": count / fps, "fps": fps}


def build_loop_finish_workflow(input_filename, *, source_frames=361, duration=15, fps=30, cadence_stride=2,
                               filename_prefix="studio/loop", model_overrides=None, target_resolution="1080p"):
    plan = loop_frame_plan(source_frames=source_frames, duration=duration, fps=fps, cadence_stride=cadence_stride)
    m = _models(model_overrides)
    width, height = output_size(target_resolution)
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": input_filename}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "GetImagesFromBatchIndexed", "inputs": {"images": ["2", 0],
            "indexes": ",".join(map(str, plan["anchors"][:-1]))}},
        "8": {"class_type": "ImageScale", "inputs": {"image": ["3", 0], "upscale_method": "lanczos",
            "width": 960, "height": 540, "crop": "center"}},
        "4": {"class_type": "RTXVideoSuperResolution", "inputs": {"images": ["8", 0],
            "resize_type": "target dimensions", "resize_type.width": width, "resize_type.height": height,
            "quality": "ULTRA"}},
        "5": {"class_type": "FrameInterpolationModelLoader", "inputs": {"model_name": m["rife_model"]}},
        # Close after temporal VSR: processing a duplicate input at the end can
        # otherwise produce a different image from VSR's cold first frame.
        "9": {"class_type": "GetImagesFromBatchIndexed", "inputs": {"images": ["4", 0],
            "indexes": ",".join(map(str, [*range(len(plan["anchors"]) - 1), 0]))}},
        "6": {"class_type": "StudioFrameInterpolateSampled", "inputs": {"images": ["9", 0],
            "interp_model": ["5", 0], "multiplier": plan["multiplier"], "selection": ",".join(map(str, plan["selection"]))}},
        "7": {"class_type": "GetImagesFromBatchIndexed", "inputs": {"images": ["6", 0],
            "indexes": ",".join(map(str, range(plan["output_frames"])))}},
        "90": {"class_type": "CreateVideo", "inputs": {"images": ["7", 0], "audio": ["2", 1], "fps": fps, "bit_depth": 8}},
        "91": {"class_type": "SaveVideo", "inputs": {"video": ["90", 0], "filename_prefix": filename_prefix,
            "format": "mp4", "format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 14.0}},
    }


def build_rtx_loop_workflow(input_filename, *, source_frames=362, duration=15, fps=30, cadence_stride=2,
                            source_width=864, source_height=480,
                            filename_prefix="studio/loop", model_overrides=None, target_resolution="1080p"):
    """The approved motion baseline first, then spatial RTX enhancement only."""
    plan = loop_frame_plan(source_frames=source_frames, duration=duration, fps=fps, cadence_stride=cadence_stride)
    m = _models(model_overrides)
    width, height = output_size(target_resolution)
    source_w, source_h = aspect_canvas(source_width, width, height)
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": input_filename}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "ImageScale", "inputs": {"image": ["2", 0], "upscale_method": "lanczos",
            "width": source_w, "height": source_h, "crop": "center"}},
        "4": {"class_type": "GetImagesFromBatchIndexed", "inputs": {"images": ["3", 0],
            "indexes": ",".join(map(str, plan["anchors"]))}},
        "5": {"class_type": "FrameInterpolationModelLoader", "inputs": {"model_name": m["rife_model"]}},
        "6": {"class_type": "StudioFrameInterpolateSampled", "inputs": {"images": ["4", 0],
            "interp_model": ["5", 0], "multiplier": plan["multiplier"], "selection": ",".join(map(str, plan["selection"]))}},
        "7": {"class_type": "GetImagesFromBatchIndexed", "inputs": {"images": ["6", 0],
            "indexes": ",".join(map(str, range(plan["output_frames"])))}},
        "8": {"class_type": "RTXVideoSuperResolution", "inputs": {"images": ["7", 0],
            "resize_type": "target dimensions", "resize_type.width": width, "resize_type.height": height, "quality": "ULTRA"}},
        "90": {"class_type": "CreateVideo", "inputs": {"images": ["8", 0], "audio": ["2", 1], "fps": fps, "bit_depth": 8}},
        "91": {"class_type": "SaveVideo", "inputs": {"video": ["90", 0], "filename_prefix": filename_prefix,
            "format": "mp4", "format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 14.0}},
    }


def video_timeline_plan(*, source_frames, source_fps, fps=30, loop_seconds=None, cadence_stride=1):
    """Uniform output timestamps with explicit source cadence and endpoint policy.

    Normal footage keeps every source frame. A finite clip holds its last frame
    for the last source-frame duration; a loop substitutes its first frame for
    that endpoint. Only an explicit stride=2 drops paired animation poses.
    """
    source_frames, source_fps, fps = int(source_frames), float(source_fps), float(fps)
    if source_frames < 2 or not 1 <= source_fps <= 120 or fps not in (24, 30, 48):
        raise ValueError("动漫超分需要至少2帧、1–120fps源视频，输出支持24/30fps")
    if cadence_stride not in (1, 2) or (cadence_stride == 2 and abs(source_fps-24) > .01):
        raise ValueError("cadence_stride=2 仅适用于明确的24fps二拍动画；普通素材请设1")
    duration = source_frames/source_fps if loop_seconds is None else float(loop_seconds)
    if duration <= 0 or (loop_seconds is not None and not .8 <= duration/(source_frames/source_fps) <= 1.2):
        raise ValueError("循环目标时长需要接近原片（0.8–1.2倍）")
    span = source_frames if loop_seconds is None else min(source_frames, round(duration*source_fps))
    positions = list(range(0, span, cadence_stride))
    anchors = positions + [0 if loop_seconds is not None else span-1]
    positions = positions + [span]
    ratio = Fraction(str(source_fps))/cadence_stride/Fraction(str(fps))
    multiplier = min(16, max(2, ratio.denominator))
    count = max(1, round(duration*fps))
    selection = []
    for index in range(count):
        source_position = index*source_fps/fps if loop_seconds is None else index*span/count
        pair = min(bisect_right(positions, source_position)-1, len(positions)-2)
        fraction = (source_position-positions[pair])/(positions[pair+1]-positions[pair])
        selection.append(pair*multiplier+round(fraction*multiplier))
    if len(selection) != len(set(selection)):
        raise ValueError("源帧率过低，有限RIFE插值步无法满足此输出帧率")
    return {"source_frames": source_frames, "source_fps": source_fps, "fps": fps,
            "source_duration": source_frames/source_fps, "source_span": span/source_fps,
            "duration": count/fps, "output_frames": count,
            "cadence_stride": cadence_stride, "anchors": anchors, "multiplier": multiplier,
            "selection": selection, "needs_interpolation": any(i % multiplier for i in selection),
            "loop": loop_seconds is not None, "endpoint": "first_frame" if loop_seconds is not None else "last_frame_hold"}


def build_anime_sr_workflow(input_filename, *, source_frames=193, source_fps=24, fps=30,
                            loop_seconds=None, cadence_stride=1, filename_prefix="studio/anime_sr",
                            model_overrides=None, target_resolution="1080p"):
    plan = video_timeline_plan(source_frames=source_frames, source_fps=source_fps, fps=fps,
                               loop_seconds=loop_seconds, cadence_stride=cadence_stride)
    m = _models(model_overrides)
    width, height = output_size(target_resolution)
    graph = {"1": {"class_type": "LoadVideo", "inputs": {"file": input_filename}},
             "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}}}
    if plan["needs_interpolation"]:
        graph["3"] = {"class_type": "GetImagesFromBatchIndexed", "inputs": {"images": ["2", 0],
            "indexes": ",".join(map(str, plan["anchors"]))}}
        graph["7"] = {"class_type": "FrameInterpolationModelLoader", "inputs": {"model_name": m["rife_model"]}}
        graph["8"] = {"class_type": "StudioFrameInterpolateSampled", "inputs": {"images": ["3", 0],
            "interp_model": ["7", 0], "multiplier": plan["multiplier"],
            "selection": ",".join(map(str, plan["selection"]))}}
        images = ["8", 0]
    else:
        indexes = [plan["anchors"][i//plan["multiplier"]] for i in plan["selection"]]
        if indexes == list(range(source_frames)):
            images = ["2", 0]
        else:
            graph["3"] = {"class_type": "GetImagesFromBatchIndexed", "inputs": {"images": ["2", 0],
                "indexes": ",".join(map(str, indexes))}}
            images = ["3", 0]
    graph["10"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": m["anime_model"]}}
    graph["11"] = {"class_type": "StudioImageUpscaleToSize", "inputs": {"upscale_model": ["10", 0],
        "images": images, "width": width, "height": height}}
    # The worker muxes original audio on the same timeline after rendering.
    graph["90"] = {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "fps": fps, "bit_depth": 8}}
    graph["91"] = {"class_type": "SaveVideo", "inputs": {"video": ["90", 0], "filename_prefix": filename_prefix,
        "format": "mp4", "format.codec": "h264", "format.codec.encoding": "re-encode", "format.codec.encoding.crf": 14.0}}
    return graph


def aspect_policy(width, height):
    """Only crop small model-grid padding; contain materially different ratios."""
    relative = abs((width / height) / (1920 / 1080) - 1)
    return "center_crop_small_grid_padding" if relative <= .03 else "contain_black_padding"


def build_timeline_workflow(input_filename, *, source_frames, source_fps, fps=30,
                            loop_seconds=None, cadence_stride=1, filename_prefix="studio/timeline", model_overrides=None,
                            target_resolution="1080p"):
    """Interpolate final-size frames only; never apply a second SR network."""
    graph = build_anime_sr_workflow(input_filename, source_frames=source_frames, source_fps=source_fps,
        fps=fps, loop_seconds=loop_seconds, cadence_stride=cadence_stride,
        filename_prefix=filename_prefix, model_overrides=model_overrides, target_resolution=target_resolution)
    images = graph.pop("11")["inputs"]["images"]
    graph.pop("10")
    if images == ["2", 0]:
        # A diffusion render may have extra grid-padding frames at its tail.
        graph["3"] = {"class_type": "GetImagesFromBatchIndexed", "inputs": {"images": ["2", 0],
            "indexes": ",".join(map(str, range(source_frames)))}}
        images = ["3", 0]
    graph["90"]["inputs"]["images"] = images
    return graph


def ffmpeg_executable():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run_ffmpeg(args, *, timeout=900):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    command = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, args)]
    result = subprocess.run(command, capture_output=True, timeout=timeout, creationflags=flags)
    if result.returncode:
        raise RuntimeError("FFmpeg: " + result.stderr.decode("utf-8", errors="replace")[-3000:])


def probe_video(path):
    import av
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError("Source has no video stream")
        stream = container.streams.video[0]
        fps = float(stream.average_rate or stream.base_rate or 24)
        duration = float(stream.duration * stream.time_base) if stream.duration else float(container.duration or 0) / 1_000_000
        frames = stream.frames or round(duration * fps)
        return {"width": stream.width, "height": stream.height, "fps": fps,
                "frames": frames, "duration": duration, "has_audio": bool(container.streams.audio)}


def _grid_frames(frames, model):
    step, base = (17, 5) if model == "h3" else (8, 1)
    return base + math.ceil(max(0, frames - base) / step) * step


def plan_segments(duration, *, segment_seconds=4.0, overlap_seconds=0.5, fps=24):
    if duration <= 0 or segment_seconds <= overlap_seconds or overlap_seconds < 0:
        raise ValueError("Invalid segment timing")
    total_frames = max(1, round(duration * fps))
    window = max(1, round(segment_seconds * fps))
    overlap = round(overlap_seconds * fps)
    stride = window - overlap
    result = []
    for start in range(0, total_frames, stride):
        end = min(start + window, total_frames)
        result.append({"start_frame": start, "frames": end - start, "start": start / fps,
                       "duration": (end - start) / fps,
                       "overlap": overlap / fps if start else 0.0})
        if end == total_frames:
            break
    return result


def prepare_input(source, destination, *, frame_grid="ltx", start=0.0, duration=None, fps=24):
    """Create an independent, lossless-timing, padded clip with an audio track."""
    info = probe_video(source)
    duration = float(duration if duration is not None else info["duration"])
    frames = max(1, round(duration * fps))
    padded = _grid_frames(frames, frame_grid)
    out = Path(destination)
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["-ss", str(start), "-i", str(source)]
    if not info["has_audio"]:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    args += ["-map", "0:v:0", "-map", "0:a:0" if info["has_audio"] else "1:a:0",
             "-vf", f"trim=duration={frames/fps},setpts=PTS-STARTPTS,fps={fps},tpad=stop_mode=clone:stop={padded-frames}",
             "-frames:v", padded, "-fps_mode", "cfr", "-r", fps,
             "-af", f"apad,atrim=duration={padded / fps}",
             "-c:v", "libx264", "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    _run_ffmpeg(args)
    actual = probe_video(out)
    if actual["frames"] != padded or abs(actual["fps"] - fps) > .01:
        raise RuntimeError(f"Prepared clip has {actual['frames']} frames at {actual['fps']} fps; expected {padded} at {fps}")
    return {"path": str(out), "frames": padded, "source_frames": frames, "duration": frames / fps}


def finalize_video(source, destination, *, loop_seconds=None, fps=30, seam_seconds=0.5, duration=None, target_resolution="1080p"):
    """Legacy FFmpeg utility; current workers use uniform frame-preserving mux."""
    info = probe_video(source)
    width, height = output_size(target_resolution)
    length = min(float(duration or info["duration"]), info["duration"])
    out = Path(destination)
    if Path(source).resolve() == out.resolve():
        raise ValueError("Destination must differ from source")
    out.parent.mkdir(parents=True, exist_ok=True)
    filters = f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
    args = ["-i", str(source)]
    if loop_seconds is not None:
        if loop_seconds <= 0:
            raise ValueError("loop_seconds must be positive")
        if seam_seconds <= 0:
            raise ValueError("Loop seam duration must be positive")
        overlap = min(float(seam_seconds), length / 4)
        period = length - overlap
        if period <= 0 or not 0.5 <= period / loop_seconds <= 2:
            raise ValueError("Loop length must stay within 0.5x–2x of the source period")
        chains = [f"[0:v]{filters},trim=duration={length},settb=AVTB,fps={fps},split=2[a][b]",
                  f"[a][b]xfade=transition=fade:duration={overlap}:offset={period},"
                  f"trim=start={overlap}:end={length},setpts=(PTS-STARTPTS)*{loop_seconds}/{period},"
                  f"fps={fps},tpad=stop_mode=clone:stop_duration=1[v]"]
        if info["has_audio"]:
            chains += [f"[0:a]atrim=duration={length},asetpts=PTS-STARTPTS,asplit=2[c][d]",
                       f"[c][d]acrossfade=d={overlap}:c1=tri:c2=tri,atrim=start={overlap}:end={length},"
                       f"asetpts=PTS-STARTPTS,atempo={period / loop_seconds}[aout]"]
        args += ["-filter_complex", ";".join(chains), "-map", "[v]"]
        if info["has_audio"]:
            args += ["-map", "[aout]"]
        length = loop_seconds
    else:
        args += ["-vf", filters + ",tpad=stop_mode=clone:stop_duration=1", "-map", "0:v:0", "-map", "0:a:0?"]
    args += ["-t", str(length), "-frames:v", round(length * fps), "-c:v", "libx264", "-preset", "medium",
             "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", "-metadata", "comment=Studio local postprocessing", str(out)]
    _run_ffmpeg(args)
    result = probe_video(out)
    if (result["width"], result["height"]) != (width, height):
        raise RuntimeError("Final output failed the target-resolution check")
    if result["frames"] != round(length * fps) or abs(result["fps"] - fps) > .01 or abs(result["duration"] - length) > 1.1 / fps:
        raise RuntimeError("Final output failed timing validation")
    return {**result, "path": str(out), "loop": loop_seconds is not None,
            "loop_method": "wrapped_crossfade" if loop_seconds else None}


def finalize_uniform_video(rendered, original, destination, *, plan, target_resolution="1080p"):
    """Keep the exact rendered frames; apply the same time map to source audio."""
    rendered_info, source_info = probe_video(rendered), probe_video(original)
    width, height = output_size(target_resolution)
    out = Path(destination)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.resolve() in (Path(rendered).resolve(), Path(original).resolve()):
        raise ValueError("Destination must differ from source and rendered video")
    args = ["-i", str(rendered), "-i", str(original), "-map", "0:v:0", "-c:v", "copy"]
    audio_method = "none"
    if source_info["has_audio"]:
        length, span = plan["duration"], plan["source_span"]
        # Match the exact video time map, including non-integer source fps.
        tempo = span/length
        filters = f"atrim=duration={span},asetpts=PTS-STARTPTS,atempo={tempo:.12g},apad,atrim=duration={length}"
        audio_method = "source_audio_same_time_map"
        if plan["loop"]:
            fade = min(.04, length/8)
            filters += f",afade=t=in:st=0:d={fade},afade=t=out:st={length-fade}:d={fade}"
            audio_method += "; 40ms endpoint fades (no audio phase shift)"
        args += ["-map", "1:a:0", "-af", filters, "-c:a", "aac", "-b:a", "192k"]
    args += ["-t", str(plan["duration"]), "-movflags", "+faststart", str(out)]
    _run_ffmpeg(args)
    result = probe_video(out)
    for key, expected in (("width", width), ("height", height), ("frames", plan["output_frames"])):
        if result[key] != expected:
            raise RuntimeError(f"Uniform video finalization changed {key}: {result[key]} != {expected}")
    if abs(result["fps"]-plan["fps"]) > .001 or abs(result["duration"]-plan["duration"]) > 1/plan["fps"]:
        raise RuntimeError("Uniform video finalization changed the rendered time base")
    return {**result, "path": str(out), "audio_method": audio_method}


def stitch_segments(paths, plans, destination, *, fps=24):
    if not paths or len(paths) != len(plans):
        raise ValueError("Segments and timing plans must match")
    args, chains = [], []
    for index, (path, plan) in enumerate(zip(paths, plans)):
        args += ["-i", str(path)]
        chains += [f"[{index}:v]trim=duration={plan['duration']},setpts=PTS-STARTPTS,settb=AVTB,format=yuv420p,fps={fps}[v{index}]",
                   f"[{index}:a]atrim=duration={plan['duration']},asetpts=PTS-STARTPTS[a{index}]"]
    v, a, duration = "v0", "a0", plans[0]["duration"]
    for index in range(1, len(paths)):
        overlap = plans[index]["overlap"]
        if overlap <= 0:
            chains += [f"[{v}][{a}][v{index}][a{index}]concat=n=2:v=1:a=1[xv{index}][xa{index}]"]
        else:
            chains += [f"[{v}][v{index}]xfade=transition=fade:duration={overlap}:offset={duration-overlap}[xv{index}]",
                       f"[{a}][a{index}]acrossfade=d={overlap}:c1=tri:c2=tri[xa{index}]"]
        v, a = f"xv{index}", f"xa{index}"
        duration += plans[index]["duration"] - overlap
    args += ["-filter_complex", ";".join(chains), "-map", f"[{v}]", "-map", f"[{a}]",
             "-t", duration, "-c:v", "libx264", "-preset", "fast", "-crf", 14,
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(destination)]
    _run_ffmpeg(args)
    return probe_video(destination)


class PostprocessComfyClient:
    def __init__(self, base_url="http://127.0.0.1:8188", *, timeout=3600):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.upload_prefix = uuid.uuid4().hex

    def upload(self, path):
        with open(path, "rb") as file:
            response = self.session.post(self.base_url + "/upload/image",
                files={"image": (self.upload_prefix + "_" + Path(path).name, file, "application/octet-stream")},
                data={"overwrite": "true", "type": "input"}, timeout=120)
        response.raise_for_status()
        data = response.json()
        return "/".join(p for p in (data.get("subfolder"), data["name"]) if p)

    def render(self, workflow, destination, *, callback=None, on_submitted=None):
        response = self.session.post(self.base_url + "/prompt", json={"prompt": workflow, "client_id": uuid.uuid4().hex}, timeout=30)
        if not response.ok:
            raise RuntimeError("ComfyUI rejected postprocessing graph: " + response.text[:3000])
        submitted = response.json()
        if submitted.get("node_errors") or not submitted.get("prompt_id"):
            raise RuntimeError("Invalid ComfyUI graph: " + json.dumps(submitted, ensure_ascii=False)[:3000])
        prompt_id = submitted["prompt_id"]
        if on_submitted:
            on_submitted(prompt_id)
        deadline = time.monotonic() + self.timeout
        last_report = 0
        while time.monotonic() < deadline:
            response = self.session.get(self.base_url + f"/history/{prompt_id}", timeout=30)
            response.raise_for_status()
            history = response.json().get(prompt_id)
            if history:
                status = history.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError("ComfyUI postprocessing failed: " + json.dumps(status.get("messages", []), ensure_ascii=False)[-3500:])
                if status.get("completed"):
                    outputs = history.get("outputs", {})
                    # Cached LoadVideo UI records can be appended after SaveVideo
                    # in history. They point to the input, never the rendered result.
                    image_output = Path(destination).suffix.lower() == ".png"
                    saver = "SaveImage" if image_output else "SaveVideo"
                    extensions = (".png",) if image_output else (".mp4", ".webm", ".mov", ".mkv")
                    saved_outputs = [value for key, value in outputs.items()
                                     if workflow.get(str(key), {}).get("class_type") == saver]
                    videos = [item for output in (saved_outputs or outputs.values())
                              for key in ("videos", "gifs", "images") for item in output.get(key, [])
                              if item.get("type", "output") != "input"
                              and item.get("filename", "").lower().endswith(extensions)]
                    if not videos:
                        raise RuntimeError("ComfyUI completed without a video output")
                    asset = videos[-1]
                    response = self.session.get(self.base_url + "/view", params={
                        "filename": asset["filename"], "subfolder": asset.get("subfolder", ""), "type": asset.get("type", "output")}, stream=True, timeout=120)
                    response.raise_for_status()
                    with open(destination, "wb") as file:
                        for block in response.iter_content(1024 * 1024):
                            file.write(block)
                    receipt = {"prompt_id": prompt_id, "asset": asset, "path": str(destination)}
                    for key, node in workflow.items():
                        if node.get("class_type") == "StudioDLSS5Filter":
                            reports = outputs.get(str(key), {}).get("text", [])
                            if len(reports) != 1:
                                raise RuntimeError("滤镜节点缺少原生参数回执")
                            receipt["native_parameters"] = json.loads(reports[0])
                    return receipt
            if callback and time.monotonic() - last_report > 20:
                callback(f"ComfyUI 正在处理整段（任务 {prompt_id[:8]}）")
                last_report = time.monotonic()
            time.sleep(1)
        # Do not interrupt a different caller's GPU job if ours was only queued.
        queue = self.session.get(self.base_url + "/queue", timeout=15).json()
        if any(item[1] == prompt_id for item in queue.get("queue_running", [])):
            self.session.post(self.base_url + "/interrupt", timeout=15)
        else:
            self.session.post(self.base_url + "/queue", json={"delete": [prompt_id]}, timeout=15)
        raise TimeoutError(f"Postprocessing exceeded {self.timeout}s for prompt {prompt_id}")


def _run_loop_postprocess(source, output, *, source_info, client, work, signature_data, preset,
                          loop_seconds, fps, prompt, seed, model_overrides, report, on_submitted, cadence_stride,
                          target_resolution):
    """Cyclic interpolation with optional whole-clip diffusion; no chunk joins.

    Intended for same-image endpoints and animation on twos. Subsampling retains
    the approved 12-pose cadence, RIFE supplies uniform output instants including
    the closing interval, and the duplicated closing anchor is never emitted.
    """
    if loop_seconds is None:
        raise ValueError("稳定循环预设需要开启循环，并设置接近原片的目标时长")
    width, height = output_size(target_resolution)
    if source_info["duration"] > 16.25:
        raise ValueError("整段稳定循环暂限 16 秒以内，避免长序列超出显存")
    if abs(source_info["fps"]-24) > .001:
        raise ValueError("旧循环预设仅支持原生24fps素材；其他帧率请使用动漫超分")
    loop_frame_plan(source_frames=round(source_info["duration"] * 24), duration=loop_seconds, fps=fps)
    signature_data = {**signature_data, "pipeline_version": "cyclic-rife-v3",
                      "loop_seconds": loop_seconds, "fps": fps, "cadence_stride": cadence_stride,
                      "target_resolution": target_resolution}
    signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()
    jobs = []

    def stage(name, graph_factory, expected, percent):
        path, receipt_path = work / f"{name}.mp4", work / f"{name}_receipt.json"
        if path.is_file() and receipt_path.is_file():
            try:
                saved = json.loads(receipt_path.read_text(encoding="utf-8"))
                info = probe_video(path)
                if saved.get("signature") == signature and all(info[k] == v for k, v in expected.items()):
                    report(percent, f"复用已完成的循环阶段：{name}")
                    jobs.append(saved)
                    return path
            except (ValueError, OSError, KeyError):
                pass
        graph = graph_factory()
        (work / f"{name}_workflow.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        # Releases the previous diffusion model before RTX/RIFE allocate GPU RAM.
        # The server processes this between jobs; this never interrupts a caller.
        client.session.post(client.base_url + "/free", json={"unload_models": True, "free_memory": True}, timeout=30).raise_for_status()
        receipt = client.render(graph, path, callback=lambda message: report(percent, message), on_submitted=on_submitted)
        info = probe_video(path)
        if not all(info[k] == v for k, v in expected.items()):
            raise RuntimeError(f"循环阶段 {name} 输出不符合预期：{info}")
        receipt.update(signature=signature, info=info)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        jobs.append(receipt)
        return path

    report(5, "准备整段循环，保留同一时间轴")
    prepared_duration = min(source_info["duration"], loop_seconds + 1 / 24)
    frame_count = _grid_frames(round(prepared_duration * 24), "ltx")

    def refinement_graph():
        prepared = prepare_input(source, work / "full_input.mp4", frame_grid="ltx", duration=prepared_duration)
        graph = build_workflow(client.upload(prepared["path"]), prompt, "redgraft_loop", seed=seed,
                              model_overrides=model_overrides, frames=prepared["frames"],
                              filename_prefix=f"studio/loop/{output.stem}_refined")
        graph["30"]["inputs"].update(tile_size=384 if frame_count > 193 else 512, overlap=64)
        return graph

    if preset == "redgraft_loop":
        refined = stage("full_refined", refinement_graph,
                        {"width": 960, "height": 544, "frames": frame_count, "fps": 24.0}, 15)
        def finish_graph():
            return build_loop_finish_workflow(client.upload(refined), source_frames=frame_count,
                duration=loop_seconds, fps=fps, cadence_stride=cadence_stride, model_overrides=model_overrides,
                target_resolution=target_resolution,
                filename_prefix=f"studio/loop/{output.stem}_rife")
        report(65, f"RTX 放大至 {target_resolution}，RIFE 均匀插帧并跨过首尾连接")
    else:
        def finish_graph():
            if abs(source_info["fps"] - 24) < .01:
                path, frames = source, source_info["frames"]
            else:
                prepared = prepare_input(source, work / "full_input.mp4", frame_grid="ltx", duration=prepared_duration)
                path, frames = prepared["path"], prepared["frames"]
            return build_rtx_loop_workflow(client.upload(path), source_frames=frames,
                duration=loop_seconds, fps=fps, cadence_stride=cadence_stride, model_overrides=model_overrides,
                target_resolution=target_resolution,
                source_width=source_info["width"], source_height=source_info["height"],
                filename_prefix=f"studio/loop/{output.stem}_stable")
        report(15, "保留原画面：RIFE 闭环插帧，再由 RTX 放大；不做扩散二采")
    planning_frames = source_info["frames"] if preset == "rtx_loop" and abs(source_info["fps"] - 24) < .01 else frame_count
    plan = loop_frame_plan(source_frames=planning_frames, duration=loop_seconds, fps=fps, cadence_stride=cadence_stride)
    finished = stage("cyclic_rife", finish_graph,
                     {"width": width, "height": height, "frames": plan["output_frames"], "fps": float(fps)}, 70)
    report(95, "导出循环，保持已插值的均匀时间轴")
    # No wrapped dissolve/retiming here: the graph already closes and samples it.
    audio_plan = video_timeline_plan(source_frames=source_info["frames"], source_fps=source_info["fps"],
                                     fps=fps, loop_seconds=loop_seconds, cadence_stride=cadence_stride)
    metadata = finalize_uniform_video(finished, source, output, plan=audio_plan, target_resolution=target_resolution)
    metadata.update(preset=preset, resampling=PRESETS[preset]["resampling"], source=SOURCES[preset],
                    source_video=str(source), segments=1, jobs=jobs, loop=True, loop_method="cyclic_rife",
                    interpolation=f"RIFE v4.26; cyclic {24/cadence_stride:g}-pose/s anchors, uniform 24/30-fps output",
                    cadence_stride=cadence_stride,
                    target_resolution=target_resolution,
                    frame_plan=plan, refinement_canvas=[960, 544] if preset == "redgraft_loop" else None,
                    pipeline_version="cyclic-rife-v3")
    (work / "result.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    report(100, "RIFE 循环处理完成，需实际播放验收" + ("（包含实验二采）" if preset == "redgraft_loop" else "（无扩散二采）"))
    return metadata


def _run_anime_sr(source, output, *, source_info, client, work, signature_data,
                  loop_seconds, fps, cadence_stride, model_overrides, report, on_submitted, target_resolution):
    plan = video_timeline_plan(source_frames=source_info["frames"], source_fps=source_info["fps"],
                               fps=fps, loop_seconds=loop_seconds, cadence_stride=cadence_stride)
    width, height = output_size(target_resolution)
    signature_data = {**signature_data, "pipeline_version": "anime-native-stream-v1",
        "loop_seconds": loop_seconds, "fps": fps, "cadence_stride": cadence_stride,
        "source_fps": source_info["fps"], "source_frames": source_info["frames"],
        "target_resolution": target_resolution,
        "source_width": source_info["width"], "source_height": source_info["height"]}
    signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()
    stage_path, receipt_path = work / "anime_sr.mp4", work / "anime_sr_receipt.json"
    expected = dict(width=width, height=height, fps=float(fps), frames=plan["output_frames"])
    receipt = None
    if receipt_path.is_file() and stage_path.is_file():
        try:
            saved = json.loads(receipt_path.read_text(encoding="utf-8"))
            info = probe_video(stage_path)
            if saved.get("signature") == signature and all(info[key] == value for key, value in expected.items()):
                receipt = saved
                report(85, "复用已完成的整片动漫超分")
        except (ValueError, OSError, KeyError):
            pass
    if receipt is None:
        report(5, "保留原分辨率与时间轴，准备动漫专用超分")
        workflow = build_anime_sr_workflow(client.upload(source), source_frames=source_info["frames"],
            source_fps=source_info["fps"], fps=fps, loop_seconds=loop_seconds, cadence_stride=cadence_stride,
            filename_prefix=f"studio/anime_sr/{output.stem}", model_overrides=model_overrides, target_resolution=target_resolution)
        (work / "anime_sr_workflow.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        client.session.post(client.base_url + "/free", json={"unload_models": True, "free_memory": True}, timeout=30).raise_for_status()
        receipt = client.render(workflow, stage_path, callback=lambda message: report(30, message), on_submitted=on_submitted)
        info = probe_video(stage_path)
        if not all(info[key] == value for key, value in expected.items()):
            raise RuntimeError(f"动漫超分输出尺寸或时间轴不符：{info}")
        receipt.update(signature=signature, info=info, frame_plan=plan)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    report(95, f"导出{target_resolution}，保留插值后的时间轴与原音轨")
    metadata = finalize_uniform_video(stage_path, source, output, plan=plan, target_resolution=target_resolution)
    metadata.update(preset="anime_sr", resampling=False, source=SOURCES["anime_sr"], source_video=str(source),
        source_resolution=[source_info["width"], source_info["height"]], segments=1, jobs=[receipt],
        loop=loop_seconds is not None, loop_method="cyclic_rife" if loop_seconds is not None else None,
        interpolation="RIFE v4.26 at selected timeline positions" if plan["needs_interpolation"] else "original source frames",
        cadence_stride=cadence_stride, frame_plan=plan, pipeline_version="anime-native-stream-v1",
        target_resolution=target_resolution,
        aspect_policy=aspect_policy(source_info["width"], source_info["height"]),
        upscaler="Official Real-ESRGAN animevideov3 4x, native input; final-size resize per frame",
        memory_strategy="Native RIFE in bounded pair groups; per-frame SR 4x intermediate released before next frame")
    (work / "result.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    report(100, "动漫超分完成（无扩散二采）")
    return metadata


def _run_whole_postprocess(source, output, *, source_info, client, work, signature_data, preset,
                           loop_seconds, fps, cadence_stride, prompt, seed, reference_image,
                           model_overrides, report, on_submitted, target_resolution):
    """One source timeline, one full-context render, then actual interpolation.

    No independent diffusion windows or seam dissolves are permitted here.
    Memory errors are surfaced; they never trigger a silent chunked fallback.
    """
    width, height = output_size(target_resolution)
    if source_info["duration"] > 16.25:
        raise ValueError("整段实验后处理目前限16秒以内；请缩短输入，不能用独立分段重绘替代")
    if aspect_policy(source_info["width"], source_info["height"]) != "center_crop_small_grid_padding":
        raise ValueError("此实验预设仅支持接近16:9的视频；其他比例请使用动漫超分（保留完整构图）")
    resampling = PRESETS[preset]["resampling"]
    if resampling and abs(source_info["fps"]-24) > .001:
        raise ValueError("扩散二采目前需要原生24fps输入；不会用重复/丢帧偷偷转换素材")
    plan = video_timeline_plan(source_frames=source_info["frames"], source_fps=source_info["fps"],
                               fps=fps, loop_seconds=loop_seconds, cadence_stride=cadence_stride)
    signature = hashlib.sha256(json.dumps({**signature_data, "pipeline_version": "whole-context-v1",
        "loop_seconds": loop_seconds, "fps": fps, "cadence_stride": cadence_stride,
        "target_resolution": target_resolution,
        "source_info": source_info}, sort_keys=True).encode()).hexdigest()
    jobs = []
    msr_plan = None
    if preset in ("ltx25_msr", "redgraft_msr"):
        msr_plan = msr_geometry(source_info["width"], source_info["height"], target_resolution)
        (work/"msr_geometry.json").write_text(json.dumps(msr_plan, ensure_ascii=False, indent=2), encoding="utf8")
    def stage(name, factory, expected, percent):
        target, record = work/f"{name}.mp4", work/f"{name}_receipt.json"
        if target.is_file() and record.is_file():
            try:
                receipt = json.loads(record.read_text(encoding="utf8"))
                actual = probe_video(target)
                if receipt.get("signature") == signature and all(actual[k] == v for k,v in expected.items()):
                    jobs.append(receipt)
                    report(percent, f"复用整段阶段 {name}")
                    return target
            except (ValueError, OSError, KeyError):
                pass
        graph = factory()
        (work/f"{name}_workflow.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf8")
        client.session.post(client.base_url+"/free", json={"unload_models": True, "free_memory": True}, timeout=30).raise_for_status()
        receipt = client.render(graph, target, callback=lambda message: report(percent, message), on_submitted=on_submitted)
        actual = probe_video(target)
        if any(actual[k] != v for k,v in expected.items()):
            raise RuntimeError(f"整段后处理尺寸/时间轴异常：{actual}")
        receipt.update(signature=signature, info=actual)
        record.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf8")
        jobs.append(receipt)
        return target
    grid = "h3" if preset == "h3_refine" else "ltx"
    render_frames = _grid_frames(source_info["frames"], grid) if resampling else source_info["frames"]
    def render_graph():
        path = source
        if resampling:
            prepared = prepare_input(source, work/"whole_input.mp4", frame_grid=grid,
                                     duration=source_info["frames"]/source_info["fps"])
            path = prepared["path"]
        graph = build_workflow(client.upload(path), prompt, preset, seed=seed, frames=render_frames,
            source_width=source_info["width"], source_height=source_info["height"],
            reference_image=client.upload(reference_image) if reference_image and preset in ("h3_refine", "ltx25_msr", "redgraft_msr") else None,
            model_overrides=model_overrides, filename_prefix=f"studio/whole/{output.stem}_render", target_resolution=target_resolution)
        graph["90"]["inputs"].update(fps=source_info["fps"])
        graph["90"]["inputs"].pop("audio", None)
        # Long temporal context trades spatial tile size for memory, without
        # splitting time. This does not assert all long clips fit every GPU.
        if "ltx" in preset or "redgraft" in preset:
            for node in graph.values():
                if node["class_type"] == "VAEDecodeTiled":
                    node["inputs"].update(temporal_size=4096, temporal_overlap=32,
                        tile_size=384 if render_frames > 193 else 512 if render_frames > 49 else 768, overlap=64)
        return graph
    report(10, "正在二采")
    rendered = stage("whole_render", render_graph,
        dict(width=width, height=height, frames=render_frames, fps=source_info["fps"]), 25)
    def timeline_graph():
        return build_timeline_workflow(client.upload(rendered), source_frames=source_info["frames"],
            source_fps=source_info["fps"], fps=fps, loop_seconds=loop_seconds, cadence_stride=cadence_stride,
            filename_prefix=f"studio/whole/{output.stem}_timeline", model_overrides=model_overrides, target_resolution=target_resolution)
    report(80, "插帧" if plan["needs_interpolation"] else "整理成片")
    finished = stage("whole_timeline", timeline_graph,
        dict(width=width, height=height, frames=plan["output_frames"], fps=float(fps)), 85)
    metadata = finalize_uniform_video(finished, source, output, plan=plan, target_resolution=target_resolution)
    metadata.update(preset=preset, resampling=resampling, source=SOURCES[preset], source_video=str(source),
        source_resolution=[source_info["width"], source_info["height"]], segments=1, jobs=jobs,
        frame_plan=plan, cadence_stride=cadence_stride, loop=plan["loop"],
        target_resolution=target_resolution,
        loop_method="cyclic_rife" if plan["loop"] else None, pipeline_version="whole-context-v1",
        interpolation="RIFE v4.26 sampled timeline" if plan["needs_interpolation"] else "original source frames",
        temporal_context="full clip; no independent generative chunks",
        aspect_policy="center_crop_small_grid_padding")
    if msr_plan is not None:
        metadata["msr_geometry"] = msr_plan
    (work/"result.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf8")
    report(100, "已完成")
    return metadata


def run_postprocess(source, output, preset=DEFAULT_PRESET, prompt="", *, loop_seconds=None,
                    fps=None, interpolation="none", progress_callback: Callable[[int, str], None] | None = None,
                    comfy_url="http://127.0.0.1:8188", reference_image=None, seed=0,
                    model_overrides=None, segment_seconds=4.0, overlap_seconds=0.5,
                    on_submitted=None, crossfade=0.5, cadence_stride=1, target_resolution="1080p",
                    media_kind="video", preview=False, preview_frame=0, preview_time=None, filter_options=None):
    """Bounded synchronous worker for asyncio.to_thread; all GPU passes use ComfyUI."""
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset}")
    if preset == "dlss_nr":
        if interpolation != "none" or loop_seconds is not None or cadence_stride != 1:
            raise ValueError("DLSS滤镜不进行插帧、循环改时或二拍处理")
        from services.dlss_filter import run_filter
        return run_filter(source, output, media_kind=media_kind, preview=preview, preview_frame=preview_frame,
            preview_time=preview_time,
            options=filter_options, comfy_url=comfy_url, progress_callback=progress_callback, on_submitted=on_submitted)
    output_size(target_resolution)
    source, output = Path(source), Path(output)
    source_info = probe_video(source)
    if preset in ("redgraft_msr", "ltx25_msr"):
        if interpolation not in ("none", "2x"):
            raise ValueError("LTX插帧仅支持不插帧或2倍")
        expected_fps = source_info["fps"] * (2 if interpolation == "2x" else 1)
        if fps is not None and abs(fps - expected_fps) > .001:
            raise ValueError("LTX帧率必须等于源帧率或源帧率的2倍；请选择插帧倍率")
        fps = expected_fps
    elif fps is None:
        fps = 30  # Historical workers retain their old timing contract.
    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.parent / (output.stem + "_stages")
    work.mkdir(parents=True, exist_ok=True)
    # Reuse only receipts for the same source and settings, never merely a path.
    source_stat = source.stat()
    reference_stat = Path(reference_image).stat() if reference_image else None
    signature_data = {"source": str(source.resolve()), "size": source_stat.st_size,
        "mtime_ns": source_stat.st_mtime_ns, "preset": preset, "prompt": prompt,
        "seed": seed, "models": _models(model_overrides), "reference_image": str(reference_image),
        "reference_size": reference_stat.st_size if reference_stat else None,
        "reference_mtime_ns": reference_stat.st_mtime_ns if reference_stat else None,
        "segment_seconds": segment_seconds, "overlap_seconds": overlap_seconds,
        "workflow_revision": WORKFLOW_REVISIONS.get(preset, 1),
        "pipeline_version": 4}
    signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()
    client = PostprocessComfyClient(comfy_url)
    object_response = client.session.get(client.base_url + "/object_info", timeout=30)
    object_response.raise_for_status()
    capability = next(c for c in capabilities(object_response.json(), model_overrides=model_overrides)["presets"] if c["id"] == preset)
    if not capability["available"]:
        raise RuntimeError("Backend unavailable: " + json.dumps(capability, ensure_ascii=False))
    report = progress_callback or (lambda percent, message: None)
    if preset == "anime_sr":
        return _run_anime_sr(source, output, source_info=source_info, client=client, work=work,
            signature_data=signature_data, loop_seconds=loop_seconds, fps=fps, cadence_stride=cadence_stride,
            model_overrides=model_overrides, report=report, on_submitted=on_submitted, target_resolution=target_resolution)
    if preset in ("redgraft_loop", "rtx_loop"):
        return _run_loop_postprocess(source, output, source_info=source_info, client=client, work=work,
            signature_data=signature_data, preset=preset, loop_seconds=loop_seconds, fps=fps, prompt=prompt,
            seed=seed, model_overrides=model_overrides, report=report, on_submitted=on_submitted,
            cadence_stride=cadence_stride, target_resolution=target_resolution)
    return _run_whole_postprocess(source, output, source_info=source_info, client=client, work=work,
        signature_data=signature_data, preset=preset, loop_seconds=loop_seconds, fps=fps,
        cadence_stride=cadence_stride, prompt=prompt, seed=seed, reference_image=reference_image,
        model_overrides=model_overrides, report=report, on_submitted=on_submitted, target_resolution=target_resolution)
