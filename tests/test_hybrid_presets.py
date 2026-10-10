import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routers import video as routes
from services import video_generation as video


@pytest.fixture(autouse=True)
def installed_models(monkeypatch):
    monkeypatch.setattr(
        video, "COMFYUI_HYBRID_MODEL", "DasiwaMinimaxH3_dasiwaHybridV4Turbo_int8.safetensors"
    )
    monkeypatch.setattr(video, "COMFYUI_HYBRID_NSFW_MODEL", "ErosMax_H3_beta5_Turbo_INT8.safetensors")


def test_hybrid_model_choice_changes_sampling_without_extra_acceleration(monkeypatch):
    monkeypatch.setattr(video, "COMFYUI_SPECTRUM_ENABLED", True)
    for choice, model, steps, sampler in (
        # 动画特调 = Dasiwa V4 Turbo（蒸馏）：euler/simple 直采。
        ("anime", video.COMFYUI_HYBRID_MODEL, video.HYBRID_STEPS, video.HYBRID_SAMPLER),
        ("nsfw", video.COMFYUI_HYBRID_NSFW_MODEL, 8, "res_multistep"),
    ):
        workflow = video._workflow("hybrid_to_video", "A quiet moonlit castle", 768, 432, 5, 42,
                                   hybrid_preset=choice)
        assert workflow["6"]["inputs"]["unet_name"] == model
        assert workflow["9"]["inputs"]["steps"] == steps
        assert workflow["17"]["inputs"]["sampler_name"] == sampler
        assert workflow["9"]["inputs"]["scheduler"] == "simple"
        assert workflow["16"]["class_type"] == "BasicGuider"
        assert "104" in workflow and "136" in workflow
        assert not any("Lora" in node["class_type"] or "Spectrum" in node["class_type"]
                       or "Cache" in node["class_type"] for node in workflow.values())


def test_hybrid_distilled_flag_follows_the_configured_checkpoint(monkeypatch):
    """comfyui.hybrid.distilled 描述动画特调基模是否自带 Turbo：
    蒸馏档（V4 Turbo）走无 Spectrum 的 turbo 模板；非蒸馏档（旧 V2 一类）走普通模板。
    两种情况都不挂 Spectrum，也不叠加任何 LoRA。"""
    monkeypatch.setattr(video, "HYBRID_DISTILLED", True)
    monkeypatch.setattr(video, "HYBRID_STEPS", 8)
    distilled = video._workflow("hybrid_to_video", "prompt", 768, 432, 5, 7, hybrid_preset="anime")
    assert distilled["9"]["inputs"]["steps"] == 8
    assert distilled["123"]["inputs"]["model"] == ["6", 0]
    assert distilled["9"]["inputs"]["model"] == ["123", 0]
    assert "150" not in distilled

    monkeypatch.setattr(video, "HYBRID_DISTILLED", False)
    monkeypatch.setattr(video, "HYBRID_STEPS", 25)
    plain = video._workflow("hybrid_to_video", "prompt", 768, 432, 5, 7, hybrid_preset="anime")
    assert plain["9"]["inputs"]["steps"] == 25
    assert plain["9"]["inputs"]["model"] == ["123", 0]
    assert "150" not in plain

    # 蒸馏档位下旧版 turbo 基模不再作为可选档位出现（配置了也不提供）。
    monkeypatch.setattr(video, "COMFYUI_HYBRID_TURBO_MODEL", "legacy-hybrid-turbo.safetensors")
    monkeypatch.setattr(video, "HYBRID_DISTILLED", True)
    assert video.turbo_preset_ids("hybrid_to_video") == []


def test_distilled_presets_reject_legacy_turbo_checkpoint():
    """自带 Turbo 的档位（V4 Turbo / Eros）不得再叠加旧版 turbo 基模。"""
    legacy_turbo = {"diffusion": "old_anime_turbo.safetensors"}
    for choice in ("nsfw", "anime"):
        with pytest.raises(ValueError, match="已内置 Turbo"):
            video._workflow("hybrid_to_video", "A quiet moonlit castle", 768, 432, 5, 42,
                            hybrid_preset=choice, turbo=legacy_turbo)
    with pytest.raises(ValueError, match="未配置"):
        video.resolve_hybrid_preset("unknown")


def test_hybrid_task_persists_choice_and_old_history_defaults_to_anime(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "VIDEO_DATA_FOLDER", str(tmp_path))
    started = []
    monkeypatch.setattr(video, "start_generation", started.append)
    record = asyncio.run(routes._create_hybrid_video_task_impl(
        "A quiet moonlit castle", duration=5, resolution_megapixels=0.2,
        first_frame=None, last_frame=None, reference_images=[], reference_videos=[],
        reference_audios=[], hybrid_preset="nsfw",
    ))
    saved = video.load_metadata(record["id"])
    assert record["hybrid_preset"] == saved["hybrid_preset"] == "nsfw"
    assert record["hybrid_model"] == video.COMFYUI_HYBRID_NSFW_MODEL
    assert record["sampling_steps"] == 8
    assert started == [record["id"]]
    assert record["nsfw"] is False  # Model selection never rewrites prompt intent.
    old = video.public_task({"id": "a" * 32, "mode": "hybrid_to_video", "_assets": {}})
    assert old["hybrid_preset"] == "anime"


def test_hybrid_endpoints_validate_and_echo_choice(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_video_access] = lambda: {"key_id": "admin"}
    monkeypatch.setattr(video, "polish_prompt", lambda *args, **kwargs: "The moon rises above a castle.")
    client = TestClient(app)
    good = client.post("/api/video/hybrid-prompt-polish", data={
        "prompt": "A moonlit castle", "hybrid_preset": "nsfw",
    })
    assert good.status_code == 200
    assert good.json()["hybrid_preset"] == "nsfw"
    bad = client.post("/api/video/hybrid-tasks", data={
        "prompt": "A moonlit castle", "hybrid_preset": "unknown",
    })
    assert bad.status_code == 422
    conflict = client.post("/api/video/hybrid-tasks", data={
        "prompt": "A moonlit castle", "hybrid_preset": "nsfw", "resolution_preset": "hybrid-turbo-8",
    })
    assert conflict.status_code == 400
    assert "已内置 Turbo" in conflict.json()["detail"]


def test_unconfigured_preset_is_rejected_before_upload_or_generation(monkeypatch):
    monkeypatch.setattr(video, "COMFYUI_HYBRID_NSFW_MODEL", "")
    with pytest.raises(HTTPException) as error:
        routes.resolve_hybrid("nsfw")
    assert error.value.status_code == 400
    assert [preset["id"] for preset in video.hybrid_presets()] == ["anime"]
