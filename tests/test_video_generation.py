import json
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import main
import pytest
from fastapi import HTTPException
from routers.video import _create_video_task_impl, derive_mode, dimensions, resolve_aspect_ratio
from services import storage_maintenance
from services import video_generation
from services.video_generation import _frame_length, _workflow, public_task, video_output_filename
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


def test_nsfw_lora_applies_only_to_fl2v_workflow(monkeypatch):
    monkeypatch.setattr(video_generation, "COMFYUI_NSFW_LORA_NAME", "SexyGod-NaughtyTimes-v2-rank512.safetensors")
    # 0 档（关闭）：不插入 Lora 节点，模型保持 6 → 150 直连。
    plain = _workflow("text_to_video", "prompt", 608, 352, 5, 1, nsfw_strength=0.0)
    assert "151" not in plain
    assert plain["150"]["inputs"]["model"] == ["6", 0]
    assert plain["104"]["inputs"]["clip"] == ["13", 0]

    # 0.3 档：fl2v 工作流插入 LoraLoader，串在 UNETLoader(6) 与 Spectrum(150) 之间，
    # clip 改为 Lora 输出（13 → 151 → 104）。
    workflow = _workflow("text_to_video", "prompt", 608, 352, 5, 1, nsfw_strength=0.3)
    lora = workflow["151"]
    assert lora["class_type"] == "LoraLoader"
    assert lora["inputs"]["lora_name"] == "SexyGod-NaughtyTimes-v2-rank512.safetensors"
    assert lora["inputs"]["model"] == ["6", 0]
    assert lora["inputs"]["clip"] == ["13", 0]
    assert lora["inputs"]["strength_model"] == 0.3
    assert lora["inputs"]["strength_clip"] == 0.3
    assert workflow["150"]["inputs"]["model"] == ["151", 0]
    assert workflow["104"]["inputs"]["clip"] == ["151", 1]

    # 0.5 档：强度随参数变化。
    strong = _workflow("first_last_frame", "prompt", 608, 352, 5, 1, nsfw_strength=0.5)
    assert strong["151"]["inputs"]["strength_model"] == 0.5
    assert strong["151"]["inputs"]["strength_clip"] == 0.5

    # 参考生成模式：即使传入强度也不插入 Lora 节点（选项保留但不生效）。
    reference = _workflow("reference_to_video", "prompt", 608, 352, 5, 1, nsfw_strength=0.5)
    assert "151" not in reference
    assert reference["150"]["inputs"]["model"] == ["127", 0]


def test_nsfw_lora_wiring_without_spectrum(monkeypatch):
    monkeypatch.setattr(video_generation, "COMFYUI_SPECTRUM_ENABLED", False)
    workflow = _workflow("text_to_video", "prompt", 608, 352, 5, 1, nsfw_strength=0.3)
    assert "150" not in workflow
    assert workflow["9"]["inputs"]["model"] == ["151", 0]
    assert workflow["16"]["inputs"]["model"] == ["151", 0]
    assert workflow["104"]["inputs"]["clip"] == ["151", 1]


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
    for path in ("/api/video/tasks", "/api/video/reference-tasks"):
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
            "a calm lake", 5, 0.2, "16:9", 7, None, None, auto_polish
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
