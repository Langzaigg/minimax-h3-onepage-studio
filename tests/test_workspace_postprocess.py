from io import BytesIO
from pathlib import Path

import av
from fastapi.testclient import TestClient
from PIL import Image
import pytest

import main
from routers import studio
from routers.access import require_video_access
from services import studio_tasks


@pytest.fixture
def client(monkeypatch):
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "alice"}
    monkeypatch.setattr(studio, "_capabilities", lambda: {"postprocess": {"presets": [
        {"id": key, "available": True} for key in studio.post_processing.PRESETS]}})
    monkeypatch.setattr(studio_tasks, "start", lambda *args: None)
    with TestClient(main.app) as client:
        yield client


def small_video():
    data = BytesIO()
    with av.open(data, mode="w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=24)
        stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
        for index in range(3):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 64), (index * 40, 100, 160)))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return data.getvalue()


def uploaded_workspace(client, *, image=False):
    response = client.post("/api/studio/workspaces", json={"name": "Source selection test"})
    assert response.status_code in (200, 201), response.text
    workspace = response.json()
    if image:
        output = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(output, "PNG")
        content, filename, mime = output.getvalue(), "image.png", "image/png"
    else:
        content, filename, mime = small_video(), "source.mp4", "video/mp4"
    response = client.post(f"/api/studio/workspaces/{workspace['id']}/assets",
                           files=[("files", (filename, content, mime))])
    assert response.status_code in (200, 201), response.text
    detail = client.get(f"/api/studio/workspaces/{workspace['id']}").json()
    return detail, detail["assets"][0], content


def test_postprocess_copies_owned_workspace_video_and_survives_workspace_deletion(client):
    workspace, asset, content = uploaded_workspace(client)
    response = client.post("/api/studio/postprocess", data={
        "source_workspace_id": workspace["id"], "source_workspace_asset_id": asset["id"],
        "preset": "redgraft_msr", "seed": "9223372036854775783", "loop": "true", "duration": "15",
    })
    assert response.status_code == 202, response.text
    task = response.json()
    stored = studio_tasks.load(task["id"])
    assert stored["source_workspace_id"] == workspace["id"]
    assert stored["source_workspace_asset_id"] == asset["id"]
    assert task["seed_text"] == "9223372036854775783"
    assert task["cadence_stride"] == stored["cadence_stride"] == 1
    assert task["target_resolution"] == stored["target_resolution"] == "1080p"
    assert Path(stored["_source"]).read_bytes() == content
    assert client.delete(f"/api/studio/workspaces/{workspace['id']}").status_code in (200, 204)
    assert Path(stored["_source"]).read_bytes() == content


def test_workspace_source_rejects_other_owner_image_and_mixed_sources(client):
    workspace, asset, _ = uploaded_workspace(client)
    payload = {"source_workspace_id": workspace["id"], "source_workspace_asset_id": asset["id"]}
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "bob"}
    assert client.post("/api/studio/postprocess", data=payload).status_code == 404
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "alice"}
    assert client.post("/api/studio/postprocess", data={**payload, "video_task_id": "a" * 32}).status_code == 400
    assert client.post("/api/studio/postprocess", data={"source_workspace_id": workspace["id"]}).status_code == 400
    assert client.post("/api/studio/postprocess", data={"source_workspace_asset_id": asset["id"]}).status_code == 400
    image_workspace, image_asset, _ = uploaded_workspace(client, image=True)
    assert client.post("/api/studio/postprocess", data={
        "source_workspace_id": image_workspace["id"], "source_workspace_asset_id": image_asset["id"],
    }).status_code == 400


def test_invalid_cadence_is_rejected_before_task_creation(client):
    before = len(list(studio_tasks.records()))
    for invalid in (0, 3, "1.5", "auto"):
        response = client.post("/api/studio/postprocess", data={"cadence_stride": invalid})
        assert response.status_code == 422
    assert len(list(studio_tasks.records())) == before


@pytest.mark.parametrize("target", ["720p", "1080p", "1440p"])
def test_postprocess_persists_selected_exact_delivery_target(client, target):
    response = client.post("/api/studio/postprocess", data={"target_resolution": target},
                           files={"video": ("source.mp4", small_video(), "video/mp4")})
    assert response.status_code == 202, response.text
    assert response.json()["target_resolution"] == target
    assert studio_tasks.load(response.json()["id"])["target_resolution"] == target


def test_invalid_delivery_target_does_not_create_task(client):
    before = len(list(studio_tasks.records()))
    response = client.post("/api/studio/postprocess", data={"target_resolution": "4k"})
    assert response.status_code == 400
    assert len(list(studio_tasks.records())) == before
