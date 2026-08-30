import json
import asyncio
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import main
import pytest
from fastapi import HTTPException
from config import (
    COMFYUI_TURBO_FL2V_4STEP_LORA,
    COMFYUI_TURBO_FL2V_8STEP_LORA,
    COMFYUI_TURBO_REF2V_4STEP_LORA,
)
from routers.video import _create_video_task_impl, derive_mode, dimensions, resolve_aspect_ratio
from services import storage_maintenance
from services import video_generation
from services.video_generation import _frame_length, _hybrid_keyframe_chain, _workflow, public_task, video_output_filename
import services.video_generation as video_generation_service


def test_shared_workflow_prunes_optional_frame_branches():
    expected = {
        "text_to_video": (False, False),
        "first_frame": (True, False),
        "last_frame": (False, True),
        "first_last_frame": (True, True),
    }
    for mode, flags in expected.items():
        workflow = _workflow(mode, "safe prompt", 608, 352, 5, 123)
        inputs = workflow["104"]["inputs"]
        assert ("first_frame" in inputs, "last_frame" in inputs) == flags
        assert workflow["104"]["inputs"]["length"] == 124
        assert "safe prompt" in json.dumps(workflow)

    reference = _workflow("reference_to_video", "reference prompt", 608, 352, 5, 456)
    assert reference["136"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert reference["127"]["inputs"]["unet_name"] == "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    assert reference["128"]["inputs"]["clip_name"] == video_generation.COMFYUI_TEXT_ENCODER
    assert reference["136"]["inputs"]["prompt"] == "reference prompt"


def test_video_modes_and_resolution_presets():
    assert derive_mode(False, False) == "text_to_video"
    assert derive_mode(True, False) == "first_frame"
    assert derive_mode(False, True) == "last_frame"
    assert derive_mode(True, True) == "first_last_frame"
    assert dimensions(0.2, 16 / 9) == (608, 352)
    assert dimensions(0.4, 16 / 9) == (864, 480)
    assert dimensions(0.5, 16 / 9) == (960, 544)
    assert dimensions(1.0, 16 / 9) == (1344, 768)
    assert _frame_length(5) == 124
    assert resolve_aspect_ratio("auto", 4 / 3, 9 / 16) == ("auto", 4 / 3)
    assert resolve_aspect_ratio("auto", None, 9 / 16) == ("auto", 9 / 16)
    assert resolve_aspect_ratio("auto", None, None, 3 / 2) == ("auto", 3 / 2)
    assert resolve_aspect_ratio("auto", None, None) == ("16:9", 16 / 9)
    with pytest.raises(HTTPException):
        resolve_aspect_ratio("first_frame", 4 / 3, None)
    with pytest.raises(HTTPException):
        resolve_aspect_ratio("last_frame", None, 9 / 16)
    filename = video_output_filename(
        "abcdef1234567890abcdef1234567890",
        {"mode": "first_last_frame", "created_at": "2026-08-04T12:34:56+08:00"},
    )
    assert filename == "AI_Video_first_last_frame_20260804_abcdef12.mp4"


def test_workflow_model_filenames_are_configurable(monkeypatch):
    monkeypatch.setattr(video_generation, "COMFYUI_FL2VA_MODEL", "custom_fl2va.safetensors")
    monkeypatch.setattr(video_generation, "COMFYUI_REF2VA_MODEL", "custom_ref2va.safetensors")
    monkeypatch.setattr(video_generation, "COMFYUI_TEXT_ENCODER", "custom_clip.safetensors")
    monkeypatch.setattr(video_generation, "COMFYUI_VIDEO_VAE", "custom_video_vae.safetensors")
    monkeypatch.setattr(video_generation, "COMFYUI_AUDIO_VAE", "custom_audio_vae.safetensors")

    standard = _workflow("text_to_video", "prompt", 608, 352, 5, 1)
    assert standard["6"]["inputs"]["unet_name"] == "custom_fl2va.safetensors"
    assert standard["13"]["inputs"]["clip_name"] == "custom_clip.safetensors"
    assert standard["11"]["inputs"]["vae_name"] == "custom_video_vae.safetensors"
    assert standard["24"]["inputs"]["vae_name"] == "custom_audio_vae.safetensors"

    reference = _workflow("reference_to_video", "prompt", 608, 352, 5, 1)
    assert reference["127"]["inputs"]["unet_name"] == "custom_ref2va.safetensors"
    assert reference["128"]["inputs"]["clip_name"] == "custom_clip.safetensors"
    assert reference["119"]["inputs"]["vae_name"] == "custom_video_vae.safetensors"
    assert reference["120"]["inputs"]["vae_name"] == "custom_audio_vae.safetensors"


FL2V_AIO_LORA = "HMNSFW-AIO-V2.5.safetensors"
FL2V_VAGINA_LORA = "Vagina_minimax-h3_epoch20.safetensors"
REF2V_NSFW_LORA = "AfterMidnight_ref2va_h3_sexytime_rank64-v1.2.safetensors"
# NSFW 档位示例：fl2v 两档（单 LoRA / 叠加双 LoRA），ref2v 一档。
SAMPLE_NSFW_PRESETS = {
    "fl2v": [
        {"id": "aio", "label": "AIO 1.0", "loras": [{"name": FL2V_AIO_LORA, "strength": 1.0}]},
        {
            "id": "aio-vagina",
            "label": "AIO 0.65 + Vagina 0.35",
            "loras": [
                {"name": FL2V_AIO_LORA, "strength": 0.65},
                {"name": FL2V_VAGINA_LORA, "strength": 0.35},
            ],
        },
    ],
    "ref2v": [
        {
            "id": "aftermidnight",
            "label": "AfterMidnight 1.0",
            "loras": [{"name": REF2V_NSFW_LORA, "strength": 1.0}],
        }
    ],
}


def _use_nsfw_presets(monkeypatch, presets=None) -> dict:
    presets = SAMPLE_NSFW_PRESETS if presets is None else presets
    monkeypatch.setattr(video_generation, "NSFW_PRESETS", presets)
    return presets


def _nsfw_preset(mode: str, preset_id: str | None = None):
    return video_generation.resolve_nsfw_preset(preset_id, mode)


def test_nsfw_preset_stacks_multiple_loras_on_fl2v(monkeypatch):
    _use_nsfw_presets(monkeypatch)
    preset = _nsfw_preset("text_to_video", "aio-vagina")
    workflow = _workflow("text_to_video", "prompt", 608, 352, 5, 1, nsfw=True, nsfw_preset=preset)
    # 基模不变，两个 LoRA 按顺序串联：UNET(6) → 151 → 152 → Spectrum(150)。
    assert workflow["6"]["inputs"]["unet_name"] == video_generation.COMFYUI_FL2VA_MODEL
    assert workflow["151"]["inputs"] == {
        "lora_name": FL2V_AIO_LORA, "strength_model": 0.65, "model": ["6", 0]
    }
    assert workflow["152"]["inputs"] == {
        "lora_name": FL2V_VAGINA_LORA, "strength_model": 0.35, "model": ["151", 0]
    }
    assert workflow["150"]["inputs"]["model"] == ["152", 0]
    assert workflow["9"]["inputs"]["model"] == ["150", 0]
    assert workflow["16"]["inputs"]["model"] == ["150", 0]

    # 单 LoRA 档位只挂一个节点。
    single = _workflow(
        "text_to_video", "prompt", 608, 352, 5, 1, nsfw=True,
        nsfw_preset=_nsfw_preset("text_to_video", "aio"),
    )
    assert single["151"]["inputs"]["strength_model"] == 1.0
    assert "152" not in single

    # 关闭：不注入任何 LoRA，基模直连 Spectrum。
    plain = _workflow("text_to_video", "prompt", 608, 352, 5, 1, nsfw=False)
    assert "151" not in plain and "152" not in plain
    assert plain["150"]["inputs"]["model"] == ["6", 0]


def test_nsfw_preset_wiring_without_spectrum(monkeypatch):
    _use_nsfw_presets(monkeypatch)
    monkeypatch.setattr(video_generation, "COMFYUI_SPECTRUM_ENABLED", False)
    workflow = _workflow(
        "text_to_video", "prompt", 608, 352, 5, 1, nsfw=True,
        nsfw_preset=_nsfw_preset("text_to_video", "aio-vagina"),
    )
    assert "150" not in workflow
    assert workflow["9"]["inputs"]["model"] == ["152", 0]
    assert workflow["16"]["inputs"]["model"] == ["152", 0]


def test_reference_nsfw_preset_uses_own_family(monkeypatch):
    _use_nsfw_presets(monkeypatch)
    preset = _nsfw_preset("reference_to_video", "aftermidnight")
    workflow = _workflow(
        "reference_to_video", "prompt", 608, 352, 5, 1, nsfw=True, nsfw_preset=preset
    )
    assert workflow["127"]["inputs"]["unet_name"] == video_generation.COMFYUI_REF2VA_MODEL
    assert workflow["144"]["inputs"]["lora_name"] == REF2V_NSFW_LORA
    assert workflow["144"]["inputs"]["model"] == ["127", 0]
    assert workflow["150"]["inputs"]["model"] == ["144", 0]
    # fl2v 档位不会串到 ref2v 工作流上。
    assert "151" not in workflow


def test_nsfw_and_turbo_are_mutually_exclusive(monkeypatch):
    _use_nsfw_presets(monkeypatch)
    for mode, turbo_id, preset_id in (
        ("text_to_video", "turbo-0.5-6", "aio"),
        ("reference_to_video", "turbo-0.5-8", "aftermidnight"),
    ):
        turbo = video_generation.resolve_turbo_preset(turbo_id, mode)
        assert turbo is not None
        # 工作流层兜底：turbo + NSFW 档位直接报错，避免蒸馏 lora 与 NSFW lora 叠加。
        with pytest.raises(ValueError):
            _workflow(
                mode, "prompt", 960, 544, 5, 1, nsfw=True, turbo=turbo,
                nsfw_preset=_nsfw_preset(mode, preset_id),
            )

    from routers.video import validate_nsfw_turbo_conflict

    turbo = video_generation.resolve_turbo_preset("turbo-0.5-6", "text_to_video")
    with pytest.raises(HTTPException):
        validate_nsfw_turbo_conflict(_nsfw_preset("text_to_video", "aio"), turbo)
    validate_nsfw_turbo_conflict(None, turbo)
    validate_nsfw_turbo_conflict(_nsfw_preset("text_to_video", "aio"), None)


def test_resolve_nsfw_rejects_unknown_or_missing_presets(monkeypatch):
    from routers.video import resolve_nsfw

    _use_nsfw_presets(monkeypatch)
    # 关闭 → None；未指定档位 → 回落第一个档位。
    assert resolve_nsfw(False, "", "text_to_video") is None
    assert resolve_nsfw(True, "", "text_to_video")["id"] == "aio"
    assert resolve_nsfw(True, "aio-vagina", "text_to_video")["id"] == "aio-vagina"
    # 未知档位 / 未配置档位 → 400。
    with pytest.raises(HTTPException):
        resolve_nsfw(True, "nope", "text_to_video")
    monkeypatch.setattr(video_generation, "NSFW_PRESETS", {"fl2v": [], "ref2v": []})
    with pytest.raises(HTTPException):
        resolve_nsfw(True, "", "text_to_video")
    with pytest.raises(HTTPException):
        resolve_nsfw(True, "", "reference_to_video")


def test_hybrid_workflow_20step_with_spectrum(monkeypatch):
    """混合模式（Dasiwa Hybrid）20 步版：UNET → SigmaShift → Spectrum → 调度器/引导器。"""
    monkeypatch.setattr(video_generation, "COMFYUI_HYBRID_MODEL", "DasiwaMinimaxH3_dasiwaHybridV1.safetensors")
    workflow = _workflow("hybrid_to_video", "prompt", 608, 352, 5, 123)
    assert workflow["6"]["inputs"]["unet_name"] == "DasiwaMinimaxH3_dasiwaHybridV1.safetensors"
    # 采样链：UNET(6) → SigmaShift(123) → Spectrum(150) → 调度器/引导器。
    assert workflow["123"]["inputs"]["model"] == ["6", 0]
    assert workflow["150"]["inputs"]["model"] == ["123", 0]
    assert workflow["9"]["inputs"]["model"] == ["150", 0]
    assert workflow["16"]["inputs"]["model"] == ["150", 0]
    assert workflow["9"]["inputs"]["steps"] == video_generation.HYBRID_STEPS
    assert workflow["9"]["inputs"]["scheduler"] == video_generation.HYBRID_SCHEDULER
    assert workflow["17"]["inputs"]["sampler_name"] == video_generation.HYBRID_SAMPLER
    assert workflow["123"]["inputs"]["shift_video"] == video_generation.HYBRID_SHIFT_VIDEO
    assert workflow["123"]["inputs"]["shift_audio"] == video_generation.HYBRID_SHIFT_AUDIO
    # 双 conditioning 节点（104/136）都保留，运行时二选一。
    assert workflow["104"]["class_type"] == "MiniMaxH3ImageToVideo"
    assert workflow["136"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert workflow["104"]["inputs"]["prompt"] == "prompt"
    assert workflow["136"]["inputs"]["prompt"] == "prompt"


def test_hybrid_turbo_workflow_direct_steps_without_spectrum(monkeypatch):
    """混合模式 turbo 加速（可选开关）：蒸馏基模固定 8 步直接采样，不挂 Spectrum。"""
    monkeypatch.setattr(video_generation, "COMFYUI_HYBRID_TURBO_MODEL", "DasiwaMinimaxH3_dasiwaHybrid8turboV1.safetensors")
    assert video_generation.turbo_preset_ids("hybrid_to_video") == ["hybrid-turbo-8"]
    turbo = video_generation.resolve_turbo_preset("hybrid-turbo-8", "hybrid_to_video")
    assert turbo is not None
    assert turbo["steps"] == video_generation.HYBRID_TURBO_STEPS == 8
    workflow = _workflow("hybrid_to_video", "prompt", 960, 544, 5, 1, turbo=turbo)
    assert workflow["6"]["inputs"]["unet_name"] == "DasiwaMinimaxH3_dasiwaHybrid8turboV1.safetensors"
    assert workflow["9"]["inputs"]["steps"] == 8
    assert workflow["9"]["inputs"]["scheduler"] == video_generation.HYBRID_TURBO_SCHEDULER
    assert workflow["17"]["inputs"]["sampler_name"] == video_generation.HYBRID_TURBO_SAMPLER
    assert workflow["9"]["inputs"]["model"] == ["123", 0]
    assert workflow["16"]["inputs"]["model"] == ["123", 0]
    assert workflow["123"]["inputs"]["model"] == ["6", 0]
    assert workflow["123"]["inputs"]["shift_video"] == video_generation.HYBRID_TURBO_SHIFT_VIDEO
    assert workflow["123"]["inputs"]["shift_audio"] == video_generation.HYBRID_TURBO_SHIFT_AUDIO
    # turbo 版不挂 Spectrum 加速节点。
    assert "150" not in workflow
    # hybrid 模式不支持 4 步档位，也不支持 fl2v/ref2v 的 turbo 档位。
    assert video_generation.resolve_turbo_preset("hybrid-turbo-4", "hybrid_to_video") is None
    assert video_generation.resolve_turbo_preset("turbo-0.5-6", "hybrid_to_video") is None


def test_hybrid_nsfw_not_applied_to_workflow(monkeypatch):
    """混合模式不叠加 NSFW LoRA（Dasiwa 基模自带能力）：传 nsfw 档位也不会改变工作流。"""
    _use_nsfw_presets(monkeypatch)
    monkeypatch.setattr(video_generation, "COMFYUI_HYBRID_MODEL", "hybrid.safetensors")
    workflow = _workflow(
        "hybrid_to_video", "prompt", 608, 352, 5, 1, nsfw=True,
        nsfw_preset=video_generation.resolve_nsfw_preset("aio", "text_to_video"),
    )
    assert workflow["6"]["inputs"]["unet_name"] == "hybrid.safetensors"
    assert "151" not in workflow and "152" not in workflow
    assert workflow["150"]["inputs"]["model"] == ["123", 0]


def test_hybrid_keyframe_chain_variants():
    """混合参考：首/末帧经官方 MiniMaxH3AddGuide 锚定为关键帧，与 136 的 refs 共存。"""
    frame_count = _frame_length(5)  # 124
    # 首尾帧都有：160(首帧) → 161(末帧) → BasicGuider。
    workflow = {"1": {}, "2": {}, "136": {}}
    chain = _hybrid_keyframe_chain(workflow, frame_count, True, True)
    assert chain == ["161", 0]
    assert workflow["160"]["class_type"] == "MiniMaxH3AddGuide"
    assert workflow["160"]["inputs"] == {
        "positive": ["136", 0], "vae": ["11", 0], "latent": ["136", 1],
        "image": ["1", 0], "frame_idx": 0,
    }
    assert workflow["161"]["inputs"]["positive"] == ["160", 0]
    assert workflow["161"]["inputs"]["image"] == ["2", 0]
    assert workflow["161"]["inputs"]["frame_idx"] == frame_count - 1
    # 仅首帧：只保留 160，末帧 LoadImage 节点被裁剪。
    workflow = {"1": {}, "2": {}, "136": {}}
    chain = _hybrid_keyframe_chain(workflow, frame_count, True, False)
    assert chain == ["160", 0]
    assert "161" not in workflow and "2" not in workflow
    # 无关键帧：136 直连引导器，两个 LoadImage 都裁剪。
    workflow = {"1": {}, "2": {}, "136": {}}
    chain = _hybrid_keyframe_chain(workflow, frame_count, False, False)
    assert chain == ["136", 0]
    assert "160" not in workflow and "161" not in workflow
    assert "1" not in workflow and "2" not in workflow


def test_hybrid_20step_workflow_mixed_reference_with_keyframes(monkeypatch):
    """混合模式 20 步版 + 参考素材 + 首尾帧：104/3/4 裁剪，采样链接 136+AddGuide。"""
    monkeypatch.setattr(video_generation, "COMFYUI_HYBRID_MODEL", "hybrid.safetensors")
    workflow = _workflow("hybrid_to_video", "prompt", 608, 352, 5, 7)
    # 有参考素材（含关键帧）：移除 104 与 ImageScale(3/4)，latent 改接 136。
    workflow.pop("104", None)
    workflow.pop("3", None)
    workflow.pop("4", None)
    workflow["14"]["inputs"]["latent_image"] = ["136", 1]
    workflow["16"]["inputs"]["conditioning"] = _hybrid_keyframe_chain(
        workflow, _frame_length(5), True, True
    )
    assert workflow["16"]["inputs"]["conditioning"] == ["161", 0]
    assert workflow["14"]["inputs"]["latent_image"] == ["136", 1]
    assert workflow["160"]["inputs"]["positive"] == ["136", 0]
    assert workflow["161"]["inputs"]["positive"] == ["160", 0]
    assert workflow["161"]["inputs"]["frame_idx"] == _frame_length(5) - 1
    # 采样链不受影响：UNET → SigmaShift → Spectrum → 调度器/引导器。
    assert workflow["9"]["inputs"]["model"] == ["150", 0]
    assert workflow["16"]["inputs"]["model"] == ["150", 0]


def test_resolve_nsfw_has_no_native_preset(monkeypatch):
    from routers.video import resolve_nsfw

    _use_nsfw_presets(monkeypatch)
    # 原生基模档位已移除：任何模式都不存在 native 档位。
    with pytest.raises(HTTPException):
        resolve_nsfw(True, "native", "text_to_video")
    with pytest.raises(HTTPException):
        resolve_nsfw(True, "native", "reference_to_video")


def test_frontend_config_exposes_nsfw_presets(monkeypatch):
    monkeypatch.setattr(main, "NSFW_PRESETS", SAMPLE_NSFW_PRESETS)
    config = asyncio.run(main.frontend_config())
    assert config["nsfw_presets"]["fl2v"] == [
        {"id": "aio", "label": "AIO 1.0"},
        {"id": "aio-vagina", "label": "AIO 0.65 + Vagina 0.35"},
    ]
    assert config["nsfw_presets"]["ref2v"] == [{"id": "aftermidnight", "label": "AfterMidnight 1.0"}]
    assert all(item.get("id") != "native" for item in config["nsfw_presets"]["fl2v"])
    # 混合模式 turbo 档位暴露给前端渲染分辨率下拉。
    assert config["turbo_presets"]["hybrid"] == video_generation.turbo_preset_ids("hybrid_to_video")
    assert "hybrid_enabled" in config and "hybrid_turbo_enabled" in config


def test_turbo_presets_and_resolution_resolution():
    from routers.video import resolve_resolution

    # fl2v：0.5（6step）/0.5（8step）用 8step lora，1.0（4step）用 4step 768p lora。
    assert video_generation.turbo_preset_ids("text_to_video") == [
        "turbo-0.5-6", "turbo-0.5-8", "turbo-1.0-4",
    ]
    # ref2v：0.5 的 4/6/8 step 三档共用同一个 4step lora。
    assert video_generation.turbo_preset_ids("reference_to_video") == [
        "turbo-0.5-4", "turbo-0.5-6", "turbo-0.5-8",
    ]

    megapixels, preset, turbo = resolve_resolution("turbo-0.5-6", 0.4, "text_to_video")
    assert (megapixels, preset) == (0.5, "turbo-0.5-6")
    assert turbo["steps"] == 6
    assert turbo["lora"] == COMFYUI_TURBO_FL2V_8STEP_LORA
    assert turbo["shift_video"] == 12.0 and turbo["shift_audio"] == 3.0

    megapixels4, preset4, turbo4 = resolve_resolution("turbo-1.0-4", 0.4, "text_to_video")
    assert (megapixels4, preset4) == (1.0, "turbo-1.0-4")
    assert turbo4["steps"] == 4
    assert turbo4["lora"] == COMFYUI_TURBO_FL2V_4STEP_LORA
    assert turbo4["shift_video"] == 6.0 and turbo4["shift_audio"] == 3.0

    for steps in (4, 6, 8):
        _, preset_ref, turbo_ref = resolve_resolution(
            f"turbo-0.5-{steps}", 0.4, "reference_to_video"
        )
        assert preset_ref == f"turbo-0.5-{steps}"
        assert turbo_ref["steps"] == steps
        assert turbo_ref["lora"] == COMFYUI_TURBO_REF2V_4STEP_LORA

    # 空档位 → 普通工作流。
    assert resolve_resolution("", 0.4, "text_to_video") == (0.4, None, None)
    # ref2v 不支持 fl2v 专属 turbo 档（turbo-1.0-4），fl2v 不支持 turbo-0.5-4。
    with pytest.raises(HTTPException):
        resolve_resolution("turbo-1.0-4", 0.4, "reference_to_video")
    with pytest.raises(HTTPException):
        resolve_resolution("turbo-0.5-4", 0.4, "text_to_video")
    with pytest.raises(HTTPException):
        resolve_resolution("turbo-9.9-9", 0.4, "text_to_video")


def test_hybrid_turbo_keeps_requested_resolution(monkeypatch):
    """混合模式 turbo 加速不锁定分辨率：有效 MP 直接采用请求值。"""
    from routers.video import resolve_resolution

    monkeypatch.setattr(
        video_generation, "COMFYUI_HYBRID_TURBO_MODEL",
        "DasiwaMinimaxH3_dasiwaHybrid8turboV1.safetensors",
    )
    preset_id = video_generation.turbo_preset_ids("hybrid_to_video")[0]
    megapixels, preset, turbo = resolve_resolution(preset_id, 0.8, "hybrid_to_video")
    assert (megapixels, preset) == (0.8, preset_id)
    assert turbo["steps"] == video_generation.HYBRID_TURBO_STEPS == 8
    assert "megapixels" not in turbo


def test_hybrid_polish_system_is_self_contained(monkeypatch):
    """混合模式专属润色提示词：混合参考理解/动漫偏向独立成文；
    破甲与 NSFW 风格指南按全局 llm.nsfw_armor 开关以拼接方式追加。"""
    system = video_generation._hybrid_polish_system("GUIDE-TEXT")
    for marker in (
        "MIXED-REFERENCE UNDERSTANDING",
        "ANIME-LEANING ART DIRECTION",
        "PROMPT GUIDE:\nGUIDE-TEXT",
    ):
        assert marker in system
    # 开关关闭：不注入破甲与风格指南。
    monkeypatch.setattr(video_generation, "LLM_NSFW_ARMOR", False)
    without = video_generation._hybrid_polish_system("G")
    assert video_generation._NSFW_ARMOR_TEXT not in without
    assert video_generation._NSFW_GUIDE_TEXT not in without
    # 开关开启：拼接破甲与风格指南；有参考素材时要求六段式输出。
    monkeypatch.setattr(video_generation, "LLM_NSFW_ARMOR", True)
    ref_system = video_generation._hybrid_polish_system("G", ref_mode=True)
    assert video_generation._NSFW_ARMOR_TEXT.strip() in ref_system
    assert video_generation._NSFW_GUIDE_TEXT.strip() in ref_system
    assert "six English sections" in ref_system
    assert "without section headers" in system


def test_turbo_fl2v_workflow_direct_steps_without_skipping(monkeypatch):
    turbo = video_generation.resolve_turbo_preset("turbo-0.5-6", "text_to_video")
    workflow = _workflow("text_to_video", "prompt", 960, 544, 5, 1, turbo=turbo)
    # 步数直接写入 BasicScheduler，SigmaShift 按步数重建网格；无跳步参数、无 Spectrum。
    assert workflow["9"]["inputs"]["steps"] == 6
    assert workflow["9"]["inputs"]["model"] == ["123", 0]
    assert workflow["123"]["inputs"]["shift_video"] == 12.0
    assert workflow["123"]["inputs"]["shift_audio"] == 3.0
    assert workflow["123"]["inputs"]["model"] == ["122", 0]
    assert workflow["122"]["inputs"]["lora_name"] == COMFYUI_TURBO_FL2V_8STEP_LORA
    assert workflow["122"]["inputs"]["model"] == ["6", 0]
    assert workflow["16"]["inputs"]["model"] == ["123", 0]
    assert workflow["17"]["inputs"]["sampler_name"] == "euler"
    assert "150" not in workflow and "151" not in workflow
    assert "first_frame" not in workflow["104"]["inputs"]
    # 首尾帧分支照常裁剪/保留。
    first_only = _workflow("first_frame", "prompt", 960, 544, 5, 1, turbo=turbo)
    assert "first_frame" in first_only["104"]["inputs"]
    assert "last_frame" not in first_only["104"]["inputs"]
    # NSFW 与 turbo 互斥：turbo 工作流不换基模、也不挂任何 NSFW lora。
    _use_nsfw_presets(monkeypatch)
    with pytest.raises(ValueError):
        _workflow(
            "text_to_video", "prompt", 960, 544, 5, 1, nsfw=True, turbo=turbo,
            nsfw_preset=video_generation.resolve_nsfw_preset("aio", "text_to_video"),
        )


def test_turbo_reference_workflow_shares_single_lora():
    for steps in (4, 6, 8):
        turbo = video_generation.resolve_turbo_preset(
            f"turbo-0.5-{steps}", "reference_to_video"
        )
        workflow = _workflow("reference_to_video", "prompt", 960, 544, 5, 1, turbo=turbo)
        assert workflow["124"]["inputs"]["steps"] == steps
        assert workflow["143"]["inputs"]["lora_name"] == COMFYUI_TURBO_REF2V_4STEP_LORA
        assert workflow["142"]["inputs"]["shift_video"] == 12.0
        assert workflow["126"]["inputs"]["model"] == ["142", 0]
        assert "150" not in workflow and "151" not in workflow
    # 普通 ref2v 工作流保持 res_multistep + Spectrum 结构。
    plain = _workflow("reference_to_video", "prompt", 960, 544, 5, 1)
    assert plain["123"]["inputs"]["sampler_name"] == "res_multistep"
    assert "143" not in plain


def test_public_task_hides_generation_backend_identifiers():
    record = {
        "id": "a" * 32,
        "mode": "text_to_video",
        "_comfy_prompt_id": "secret-backend-id",
        "_assets": {},
    }
    public = public_task(record)
    assert "_comfy_prompt_id" not in public
    assert "_assets" not in public
    assert public["assets"] == {}


def test_legacy_result_file_is_migrated_to_named_output(tmp_path, monkeypatch):
    task_id = "c" * 32
    task_folder = tmp_path / task_id / "outputs"
    task_folder.mkdir(parents=True)
    legacy = task_folder / "result.mp4"
    legacy.write_bytes(b"video")
    metadata = {
        "id": task_id,
        "mode": "text_to_video",
        "created_at": "2026-08-04T12:34:56+08:00",
        "_assets": {"video": str(legacy.resolve())},
    }
    monkeypatch.setattr(video_generation_service, "VIDEO_DATA_FOLDER", str(tmp_path))
    public = public_task(metadata)
    named = task_folder / "AI_Video_text_to_video_20260804_cccccccc.mp4"
    assert named.is_file()
    assert not legacy.exists()
    assert public["video_filename"] == named.name


def test_reference_history_uses_global_audio_label_order():
    record = {
        "id": "b" * 32,
        "mode": "reference_to_video",
        "reference_counts": {"images": 1, "videos": 2, "audios": 1},
        "use_video_audio": True,
        "_assets": {"reference_audio_1": "ignored-local-path.wav"},
        "_reference_assets": [
            {
                "kind": "audio",
                "index": 1,
                "filename": "rain.wav",
                "asset_name": "reference_audio_1",
                "path": "ignored-local-path.wav",
            }
        ],
    }
    public = public_task(record)
    assert public["references"][0]["label_index"] == 3
    assert "ignored-local-path" not in json.dumps(public)


def _write_task(root: Path, task_id: str, status: str = "completed", age_days: int = 0) -> None:
    folder = root / task_id
    folder.mkdir(parents=True)
    timestamp = (datetime.now().astimezone() - timedelta(days=age_days)).isoformat()
    (folder / "metadata.json").write_text(
        json.dumps({
            "id": task_id, "mode": "text_to_video", "status": status, "prompt": "p",
            "duration": 5, "width": 608, "height": 352, "seed": 1, "progress": 100,
            "created_at": timestamp, "updated_at": timestamp, "completed_at": timestamp,
            "_assets": {},
        }),
        encoding="utf-8",
    )


def test_history_only_returns_explicit_unpredictable_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(video_generation, "VIDEO_DATA_FOLDER", str(tmp_path))
    first, second = "1" * 32, "2" * 32
    _write_task(tmp_path, first)
    _write_task(tmp_path, second)
    assert video_generation.list_tasks([], 100) == []
    assert [task["id"] for task in video_generation.list_tasks([first], 100)] == [first]


def test_terminal_task_delete_and_retention_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(video_generation, "VIDEO_DATA_FOLDER", str(tmp_path))
    old, active = "3" * 32, "4" * 32
    _write_task(tmp_path, old, age_days=91)
    _write_task(tmp_path, active, status="running", age_days=91)
    assert video_generation.cleanup_expired_video_tasks(90) == 1
    assert not (tmp_path / old).exists()
    assert (tmp_path / active).exists()


def test_generation_openapi_uses_one_endpoint_per_workflow_with_optional_polish():
    schema = main.app.openapi()
    for path in ("/api/video/tasks", "/api/video/reference-tasks", "/api/video/hybrid-tasks"):
        operation = schema["paths"][path]["post"]
        body_ref = operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
        body = schema["components"]["schemas"][body_ref.rsplit("/", 1)[-1]]
        prop = body["properties"]["polish_prompt_first"]
        assert prop["default"] is False
    assert "/api/video/tasks/with-prompt-polish" not in schema["paths"]
    assert "/api/video/tasks/{task_id}/result" not in schema["paths"]
    assert main.app.swagger_ui_parameters["docExpansion"] == "none"
    task_operation = schema["paths"]["/api/video/tasks"]["post"]
    task_body_ref = task_operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    task_body = schema["components"]["schemas"][task_body_ref.rsplit("/", 1)[-1]]
    assert task_body["properties"]["aspect_ratio"]["default"] == "auto"
    reference_operation = schema["paths"]["/api/video/reference-tasks"]["post"]
    reference_body_ref = reference_operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    reference_body = schema["components"]["schemas"][reference_body_ref.rsplit("/", 1)[-1]]
    assert reference_body["properties"]["aspect_ratio"]["default"] == "auto"
    # 混合模式端点同时接受首尾帧与参考素材。
    hybrid_operation = schema["paths"]["/api/video/hybrid-tasks"]["post"]
    hybrid_body_ref = hybrid_operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    hybrid_body = schema["components"]["schemas"][hybrid_body_ref.rsplit("/", 1)[-1]]
    for prop_name in ("first_frame", "last_frame", "reference_images", "reference_videos", "reference_audios"):
        assert prop_name in hybrid_body["properties"]


def test_same_generation_implementation_switches_direct_or_polish_first(tmp_path, monkeypatch):
    monkeypatch.setattr(video_generation, "VIDEO_DATA_FOLDER", str(tmp_path))
    started = []
    monkeypatch.setattr(video_generation, "start_generation", lambda task_id: started.append((task_id, "direct")))
    monkeypatch.setattr(
        video_generation,
        "start_polish_and_generation",
        lambda task_id: started.append((task_id, "polish")),
    )
    for auto_polish in (False, True):
        asyncio.run(_create_video_task_impl(
            "a calm lake", 5, 0.2, "", "16:9", 7, None, None, auto_polish
        ))
    assert [kind for _, kind in started] == ["direct", "polish"]


def test_emergency_cleanup_candidates_never_include_active_video_tasks(tmp_path, monkeypatch):
    video_root = tmp_path / "video"
    _write_task(video_root, "5" * 32, status="completed")
    _write_task(video_root, "6" * 32, status="running")
    monkeypatch.setattr(storage_maintenance, "VIDEO_DATA_FOLDER", str(video_root))
    paths = {path for _, path in storage_maintenance._emergency_candidates()}
    assert video_root / ("5" * 32) in paths
    assert video_root / ("6" * 32) not in paths


def _make_test_video(ffmpeg: str, folder: Path, size: str = "640x360") -> Path:
    output = folder / "sample.mp4"
    subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi", "-i", f"testsrc=duration=2:size={size}:rate=24",
            "-pix_fmt", "yuv420p", str(output),
        ],
        capture_output=True,
        check=True,
        timeout=90,
    )
    return output


@pytest.mark.skipif(not video_generation.FFMPEG_PATH, reason="未配置 ffmpeg（ffmpeg.path 为空）")
def test_reference_video_helpers_480p_gif_and_resize(tmp_path):
    """参考视频转 480p GIF（喂润色模型）与重编码到输出分辨率/帧率（喂生成模型）。"""
    from PIL import Image

    folder = tmp_path / "work"
    folder.mkdir()
    video = _make_test_video(video_generation.FFMPEG_PATH, folder)

    gif = video_generation._video_to_480p_gif(video, folder)
    assert gif is not None and gif.is_file() and gif.stat().st_size > 0
    with Image.open(gif) as image:
        assert max(image.size) <= video_generation.POLISH_GIF_MAX_SIDE
        assert image.format == "GIF"

    resized = video_generation._resize_reference_video(video, 608, 352, 124 / 5)
    assert resized is not None and resized.is_file() and resized.stat().st_size > 0
    try:
        # 解码一帧验证输出分辨率与目标一致（608x352）。
        frame = folder / "frame.png"
        subprocess.run(
            [video_generation.FFMPEG_PATH, "-y", "-i", str(resized), "-frames:v", "1", str(frame)],
            capture_output=True,
            check=True,
            timeout=60,
        )
        with Image.open(frame) as image:
            assert image.size == (608, 352)
        # 帧率对齐目标（124 帧 / 5 秒 = 24.8fps）。
        probe = subprocess.run(
            [video_generation.FFMPEG_PATH, "-i", str(resized)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        match = re.search(r"(\d+(?:\.\d+)?) fps", probe.stderr)
        assert match and abs(float(match.group(1)) - 124 / 5) < 0.2
    finally:
        resized.unlink(missing_ok=True)


@pytest.mark.skipif(not video_generation.FFMPEG_PATH, reason="未配置 ffmpeg（ffmpeg.path 为空）")
def test_polish_reference_prompt_feeds_480p_gif_for_video_reference(tmp_path, monkeypatch):
    """配置 ffmpeg 时，参考视频转成 480p GIF 作为视频参考信息喂给润色模型（不再用代表帧）。"""
    video = _make_test_video(video_generation.FFMPEG_PATH, tmp_path)
    calls = []

    class FakeLLM:
        def __init__(self, *args, **kwargs):
            pass

        def complete_text(self, messages, options, **kwargs):
            calls.append(messages)
            return (
                "subject_definitions: OK\nsummary: OK\nretention_analysis: OK\n"
                "detailed_description: OK\noverall_soundscape: OK\nnon_diegetic_music: OK"
            )

    monkeypatch.setattr(video_generation, "LLMClient", FakeLLM)
    result = video_generation.polish_reference_prompt(
        "a quiet lake at dusk", 5, [], [video], [], True, False
    )
    assert "non_diegetic_music: OK" in result
    content = calls[0][1]["content"]
    gif_parts = [
        part for part in content
        if part.get("type") == "image_url"
        and str(part["image_url"]["url"]).startswith("data:image/gif;base64,")
    ]
    assert gif_parts, "参考视频应转成 480p GIF 喂给润色模型"
    assert "Animated GIF overview of <Video 1>" in json.dumps(content, ensure_ascii=False)
    assert not any("Representative frame" in str(part.get("text", "")) for part in content)


def test_cleanup_comfyui_uploads_removes_recorded_copies(tmp_path, monkeypatch):
    """配置 input_dir 后，上传到 ComfyUI input 的临时副本在生成结束时应被删除。"""
    monkeypatch.setattr(video_generation, "COMFYUI_INPUT_DIR", str(tmp_path / "input"))
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "minimax_h3_studio_a.jpg").write_bytes(b"first")
    (input_root / "minimax_h3_studio_b.png").write_bytes(b"ref")
    subfolder = input_root / "sub"
    subfolder.mkdir()
    (subfolder / "minimax_h3_studio_c.jpg").write_bytes(b"video")

    client = video_generation.ComfyClient()
    client.uploaded_files = [
        "minimax_h3_studio_a.jpg",
        "minimax_h3_studio_b.png",
        "sub/minimax_h3_studio_c.jpg",
    ]
    video_generation.cleanup_comfyui_uploads(client)

    assert not (input_root / "minimax_h3_studio_a.jpg").exists()
    assert not (input_root / "minimax_h3_studio_b.png").exists()
    assert not (subfolder / "minimax_h3_studio_c.jpg").exists()
    assert client.uploaded_files == []


def test_cleanup_comfyui_uploads_disabled_or_unsafe_paths(tmp_path, monkeypatch):
    """未配置 input_dir 时不清理；目录穿越路径被拒绝。"""
    monkeypatch.setattr(video_generation, "COMFYUI_INPUT_DIR", "")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    client = video_generation.ComfyClient()
    client.uploaded_files = ["../outside.jpg"]
    video_generation.cleanup_comfyui_uploads(client)
    assert outside.exists(), "未配置 input_dir 时不得删除任何文件"

    monkeypatch.setattr(video_generation, "COMFYUI_INPUT_DIR", str(tmp_path / "input"))
    (tmp_path / "input").mkdir()
    client2 = video_generation.ComfyClient()
    client2.uploaded_files = ["../outside.jpg", "missing/minimax_h3_studio_x.jpg"]
    video_generation.cleanup_comfyui_uploads(client2)
    assert outside.exists(), "目录穿越路径必须被拒绝"
    assert client2.uploaded_files == []


def test_hybrid_turbo_template_keeps_reference_injection_anchors():
    """turbo 混合模板必须保留参考注入路径依赖的节点锚点：
    104/136（conditioning 运行时二选一裁剪）、1/2（首末帧 LoadImage）、
    14/16（采样链重接线）。模板精简时误删任一节点都会让 turbo+混合参考失效。"""
    workflow = json.loads(
        (Path(video_generation.VIDEO_WORKFLOW_FOLDER) / "hybrid_video_turbo_api.json").read_text(encoding="utf-8")
    )
    for node_id in ("1", "2", "14", "16", "104", "136"):
        assert node_id in workflow, f"hybrid turbo 模板缺少节点 {node_id}，参考注入路径会失效"
