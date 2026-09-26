from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

import main
from routers import studio
from routers.access import require_video_access
from services import studio_tasks as tasks


def png(color):
    output = BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "ROOT", tmp_path / "jobs")
    monkeypatch.setattr(tasks, "start", lambda task_id: None)
    monkeypatch.setattr(studio.post_processing, "probe_video", lambda _: {
        "width": 1280, "height": 720, "frames": 192, "fps": 24.0, "duration": 8.0, "has_audio": False})
    monkeypatch.setattr(studio, "_capabilities", lambda: {"postprocess": {"presets": [
        {"id": preset, "available": True} for preset in studio.post_processing.PRESETS]}})
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "alice"}
    try:
        with TestClient(main.app) as client:
            yield client
    finally:
        main.app.dependency_overrides.clear()


def upload(client, reference=None):
    files = {"video": ("input.mp4", b"owned-video", "video/mp4")}
    if reference is not None:
        files["reference_image"] = ("../../reference.png", reference, "image/png")
    return client.post("/api/studio/postprocess", files=files)


def test_explicit_reference_upload_is_owned_and_reusable_from_history(client):
    image = png("red")
    response = upload(client, image)
    assert response.status_code == 202
    public = response.json()
    saved = tasks.load(public["id"])
    reference = Path(saved["_reference"])
    assert reference.is_relative_to(tasks.directory(public["id"]))
    assert reference.read_bytes() == image
    assert client.get(public["assets"]["reference_image"]).content == image
    assert "_reference" not in public

    repeated = client.post("/api/studio/postprocess", data={"source_studio_task_id": public["id"]})
    assert repeated.status_code == 202
    restored = tasks.load(repeated.json()["id"])
    assert Path(restored["_reference"]).read_bytes() == image
    assert Path(restored["_reference"]) != reference

    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "bob"}
    assert client.get(public["assets"]["reference_image"]).status_code == 404
    assert client.get(f"/api/studio/tasks/{public['id']}").status_code == 404
    unauthorized = client.post("/api/studio/postprocess", data={"source_studio_task_id": public["id"]},
                               files={"reference_image": ("override.png", png("blue"), "image/png")})
    assert unauthorized.status_code == 404


def test_explicit_reference_takes_priority_over_inherited_history_reference(client):
    original = upload(client, png("red")).json()
    override = png("blue")
    response = client.post("/api/studio/postprocess", data={"source_studio_task_id": original["id"]},
                           files={"reference_image": ("new.png", override, "image/png")})
    assert response.status_code == 202
    saved = tasks.load(response.json()["id"])
    assert Path(saved["_reference"]).read_bytes() == override
    assert Path(tasks.load(original["id"])["_reference"]).read_bytes() == png("red")


def test_reference_inherits_video_task_image_and_explicit_upload_can_replace_it(client, tmp_path, monkeypatch):
    source, reference = tmp_path / "source.mp4", tmp_path / "source.png"
    source.write_bytes(b"owned-video")
    reference.write_bytes(png("red"))
    monkeypatch.setattr(studio.video_generation, "load_metadata", lambda _: {"_owner": "alice", "status": "completed"})
    monkeypatch.setattr(studio.video_generation, "asset_path", lambda _id, asset: source if asset == "video" else reference)
    payload = {"video_task_id": "a" * 32}
    response = client.post("/api/studio/postprocess", data=payload)
    assert response.status_code == 202
    assert Path(tasks.load(response.json()["id"])["_reference"]).read_bytes() == png("red")
    replacement = png("green")
    overridden = client.post("/api/studio/postprocess", data=payload,
                            files={"reference_image": ("new.png", replacement, "image/png")})
    assert overridden.status_code == 202
    assert Path(tasks.load(overridden.json()["id"])["_reference"]).read_bytes() == replacement


def test_missing_reference_remains_optional_and_invalid_image_is_rejected(client):
    response = upload(client)
    assert response.status_code == 202
    assert tasks.load(response.json()["id"])["_reference"] is None
    assert "reference_image" not in response.json()["assets"]
    invalid = upload(client, b"not-an-image")
    assert invalid.status_code == 400
    failed = [task for task in tasks.ROOT.glob("*/metadata.json")
              if tasks.load(task.parent.name)["status"] == "failed"]
    assert len(failed) == 1
    bad_task = tasks.load(failed[0].parent.name)
    assert not bad_task.get("_ready")
    assert not (failed[0].parent / "reference.png").exists()


@pytest.mark.parametrize("preset", ["redgraft_msr"])
def test_one_click_msr_payload_inherits_source_reference_and_exact_seed(client, tmp_path, monkeypatch, preset):
    source, reference = tmp_path / "source.mp4", tmp_path / "source.png"
    source.write_bytes(b"original-video")
    reference.write_bytes(png("red"))
    monkeypatch.setattr(studio.video_generation, "load_metadata", lambda _: {"_owner": "alice", "status": "completed"})
    monkeypatch.setattr(studio.video_generation, "asset_path", lambda _id, asset: source if asset == "video" else reference)
    payload = {"video_task_id": "b" * 32, "preset": preset, "target_resolution": "1440p",
               "prompt": "Keep the original animation and character.", "seed": "9223372036854775807",
               "interpolation": "none", "cadence_stride": "1", "loop": "false", "duration": "8"}
    response = client.post("/api/studio/postprocess", data=payload)
    assert response.status_code == 202
    public = response.json()
    saved = tasks.load(public["id"])
    assert public["seed_text"] == payload["seed"]
    assert saved["video_task_id"] == payload["video_task_id"]
    assert saved["preset"] == preset
    assert saved["target_resolution"] == "1440p"
    assert saved["prompt"] == payload["prompt"]
    assert (saved["target_fps"], saved["cadence_stride"], saved["loop"]) == (24, 1, False)
    assert not saved["title_enabled"]
    assert Path(saved["_source"]).read_bytes() == source.read_bytes()
    assert Path(saved["_reference"]).read_bytes() == reference.read_bytes()
