from fractions import Fraction
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from routers import video as routes
from services import video_generation as video
from services.delivery_profiles import fit_even_aspect, parse_aspect, resolution_profiles


def png(width=160, height=90):
    output = BytesIO()
    Image.new("RGB", (width, height), "navy").save(output, "PNG")
    return output.getvalue()


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_video_access] = lambda: {"key_id": "admin"}
    started = []
    monkeypatch.setattr(video, "start_generation", started.append)
    monkeypatch.setattr(video, "COMFYUI_HYBRID_MODEL", "DasiwaMinimaxH3_dasiwaHybridV2_int8.safetensors")
    return TestClient(app), started


def create(client, endpoint="tasks", **data):
    files = None
    if endpoint == "reference-tasks":
        files = {"reference_images": ("castle.png", png(), "image/png")}
    return client.post(f"/api/video/{endpoint}", data={"prompt": "A moonlit castle", **data}, files=files)


@pytest.mark.parametrize("endpoint", ["tasks", "reference-tasks", "hybrid-tasks"])
@pytest.mark.parametrize("profile,sampling,output", [
    ("720p", (736, 416), (736, 414)),
    ("1080p", (960, 544), (960, 540)),
    ("1440p", (1376, 768), (1344, 756)),
])
def test_all_video_routes_persist_sampling_and_exact_source_dimensions(api, endpoint, profile, sampling, output):
    client, started = api
    response = create(client, endpoint, aspect_ratio="4:3", target_resolution=profile,
                      auto_postprocess=True, source_resolution_profile="stale", resolution_megapixels=.2)
    assert response.status_code == 202, response.text
    task = response.json()
    saved = video.load_metadata(task["id"])
    assert (task["width"], task["height"]) == output
    assert (task["sampling_width"], task["sampling_height"]) == sampling
    assert (task["output_width"], task["output_height"]) == output
    assert task["width"] * 9 == task["height"] * 16
    assert all(value % 32 == 0 for value in sampling)
    assert task["target_resolution"] == task["source_resolution_profile"] == profile
    assert task["aspect_ratio_fraction"] == "16:9"
    assert task["auto_postprocess"] is True
    assert saved["auto_postprocess"] is True
    assert task["auto_postprocess_task_id"] is None
    for key in ("sampling_width", "sampling_height", "output_width", "output_height",
                "target_resolution", "source_resolution_profile", "aspect_ratio_fraction"):
        assert saved[key] == task[key]
    assert task["resolution_megapixels"] == {"720p": .3, "1080p": .5, "1440p": 1.0}[profile]
    assert any("0.5 MP" in item for item in task["warnings"]) == (profile == "1440p")
    assert started == [task["id"]]


def test_automatic_source_profile_follows_delivery_target_ignoring_stale_profile(api):
    client, _ = api
    response = create(client, auto_postprocess=True, source_resolution_profile="720p", target_resolution="1440p")
    assert response.status_code == 202
    assert response.json()["width"] == 1344
    assert response.json()["target_resolution"] == "1440p"


@pytest.mark.parametrize("endpoint", ["tasks", "reference-tasks", "hybrid-tasks"])
def test_automatic_off_ignores_stale_profile_and_preserves_manual_budget_and_ratio(api, endpoint):
    client, _ = api
    response = create(client, endpoint, auto_postprocess=False, aspect_ratio="4:3",
                      resolution_megapixels=.4, source_resolution_profile="missing", target_resolution="1440p")
    assert response.status_code == 202, response.text
    task = response.json()
    assert task["auto_postprocess"] is False
    assert task["source_resolution_profile"] == ""
    assert task["aspect_ratio_fraction"] == "4:3"
    assert task["width"] * 3 == task["height"] * 4
    assert task["resolution_megapixels"] == .4


@pytest.mark.parametrize("aspect,expected", [("37:20", "37:20"), ("1.85:1", "37:20"), ("21:9", "7:3")])
def test_custom_ratios_use_exact_fractions_without_changing_sampling_grid(api, aspect, expected):
    client, _ = api
    response = create(client, aspect_ratio=aspect, resolution_megapixels=.4)
    assert response.status_code == 202, response.text
    task = response.json()
    ratio = parse_aspect(expected)
    assert task["aspect_ratio_fraction"] == expected
    assert Fraction(task["width"], task["height"]) == ratio
    assert task["width"] <= task["sampling_width"] and task["height"] <= task["sampling_height"]
    assert task["width"] % 2 == task["height"] % 2 == 0


def test_auto_ratio_uses_original_image_dimensions(api):
    client, _ = api
    response = client.post("/api/video/tasks", data={"prompt": "A calm castle", "aspect_ratio": "auto"},
                           files={"first_frame": ("input.png", png(1000, 750), "image/png")})
    assert response.status_code == 202, response.text
    task = response.json()
    assert task["aspect_ratio"] == "auto"
    assert task["aspect_ratio_fraction"] == "4:3"
    assert task["width"] * 3 == task["height"] * 4


@pytest.mark.parametrize("data,detail", [
    ({"target_resolution": "4K"}, "交付目标"),
    ({"aspect_ratio": "2048:1150", "resolution_megapixels": .2}, "最小偶数尺寸"),
    ({"aspect_ratio": "16:0"}, "必须大于0"),
])
def test_invalid_dimension_choices_reject_before_creating_or_starting_a_task(api, data, detail):
    client, started = api
    response = create(client, **data)
    assert response.status_code == 400, response.text
    assert detail in response.json()["detail"]
    assert started == []


@pytest.mark.parametrize("endpoint", ["tasks", "reference-tasks"])
def test_fixed_turbo_budget_cannot_silently_change_under_a_source_profile(api, monkeypatch, endpoint):
    client, started = api
    monkeypatch.setattr(video, "resolve_turbo_preset", lambda *_: {"megapixels": .5, "steps": 8})
    response = create(client, endpoint, resolution_preset="test-turbo", auto_postprocess=True, target_resolution="720p")
    assert response.status_code == 400
    assert "Turbo固定像素预算" in response.json()["detail"]
    assert started == []
    compatible = create(client, endpoint, resolution_preset="test-turbo", auto_postprocess=True, target_resolution="1080p")
    assert compatible.status_code == 202, compatible.text
    assert compatible.json()["width"] == 960


def test_hybrid_turbo_keeps_its_profile_while_auto_chooses_source_dimensions(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(video, "resolve_turbo_preset", lambda *_: {"megapixels": .5, "steps": 8, "diffusion": "fixture-hybrid.safetensors"})
    response = create(client, "hybrid-tasks", resolution_preset="fixture", auto_postprocess=True, target_resolution="720p")
    assert response.status_code == 202, response.text
    assert response.json()["sampling_steps"] == 8
    assert response.json()["hybrid_model"] == "fixture-hybrid.safetensors"
    assert response.json()["width"] == 736


@pytest.mark.parametrize("duration", [16, 16.25, 17])
def test_auto_duration_rejects_unsupported_actual_grid_before_generation(duration):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exception:
        routes._auto_source_options(True, duration, "4:3", "1080p")
    assert exception.value.status_code == 400
    assert routes._auto_source_options(False, duration, "4:3", "1080p") == ("4:3", "")


def test_profile_capabilities_distinguish_author_grid_source_video_and_delivery():
    profiles = resolution_profiles()
    assert [row["id"] for row in profiles] == ["720p", "1080p", "1440p"]
    assert [row["author_saved_selection"] for row in profiles] == [False, False, True]
    for row in profiles:
        assert row["workflow_source"].endswith("MINIMAX_H3_LTX2.5_Upscaler_v1.json")
        assert row["source_width"] * 9 == row["source_height"] * 16
        assert row["source_width"] <= row["sampling_width"]
        assert row["source_height"] <= row["sampling_height"]
        assert row["output_width"] * 9 == row["output_height"] * 16
    assert fit_even_aspect(1376, 768, Fraction(16, 9)) == (1344, 756)


@pytest.mark.parametrize("name,source,native,sampling,target", [
    ("720p", (736, 414), (1472, 828), (1472, 832), (1280, 720)),
    ("1080p", (960, 540), (1920, 1080), (1920, 1088), (1920, 1080)),
    ("1440p", (1344, 756), (2688, 1512), (2688, 1536), (2560, 1440)),
])
def test_msr_profile_separates_source_native_two_times_content_padding_and_delivery(name, source, native, sampling, target):
    row = next(row for row in resolution_profiles() if row["id"] == name)
    assert (row["source_width"], row["source_height"]) == source
    assert (row["native_2x_width"], row["native_2x_height"]) == native
    assert (row["msr_sampling_width"], row["msr_sampling_height"]) == sampling
    assert (row["output_width"], row["output_height"]) == target
    assert row["msr_padded_source_width"] * 2 == sampling[0]
    assert row["msr_padded_source_height"] * 2 == sampling[1]
    assert 0 <= row["msr_padded_source_width"] - source[0] < 32
    assert 0 <= row["msr_padded_source_height"] - source[1] < 32
    assert f"{native[0]}×{native[1]}" in row["description"]
    assert f"{target[0]}×{target[1]}" in row["description"]
