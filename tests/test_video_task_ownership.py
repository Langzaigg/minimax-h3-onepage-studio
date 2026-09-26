import pytest
from fastapi.testclient import TestClient

import main
from routers.access import require_video_access
from services import video_generation as video


@pytest.fixture
def owned_video():
    task = video.create_task_record(
        mode="first_frame", prompt="Moonlit castle", duration=5,
        width=864, height=480, resolution_megapixels=.4, aspect_ratio="16:9",
        seed=9223372036854775783, warnings=[], owner="alice",
    )
    video.save_input_asset(task["id"], "first_frame", "frame.png", b"test image")
    task = video.load_metadata(task["id"])
    task["status"] = "completed"
    video._save_metadata(task)
    return task


@pytest.mark.parametrize("method,suffix", [
    ("get", ""), ("get", "/assets/first_frame"),
    ("get", "/gif"), ("delete", ""),
])
def test_other_owner_cannot_read_convert_or_delete(owned_video, method, suffix, monkeypatch):
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "bob"}
    def forbidden_side_effect(*args):
        pytest.fail("Unauthorized task must not reach conversion or deletion")
    monkeypatch.setattr(video, "ensure_video_gif", forbidden_side_effect)
    monkeypatch.setattr(video, "delete_task_record", forbidden_side_effect)
    with TestClient(main.app) as client:
        response = getattr(client, method)(f"/api/video/tasks/{owned_video['id']}{suffix}")
    assert response.status_code == 404
    assert video.load_metadata(owned_video["id"])["_owner"] == "alice"


@pytest.mark.parametrize("identity", [
    {"role": "user", "key_id": "alice"}, {"role": "admin", "key_id": "admin"},
])
def test_owner_and_admin_can_reuse_exact_seed_and_assets(owned_video, identity):
    main.app.dependency_overrides[require_video_access] = lambda: identity
    with TestClient(main.app) as client:
        response = client.get(f"/api/video/tasks/{owned_video['id']}")
        assert response.status_code == 200
        data = response.json()
        assert data["seed_text"] == "9223372036854775783"
        asset = client.get(data["assets"]["first_frame"])
        assert asset.status_code == 200
        assert asset.content == b"test image"


def test_owner_can_delete_completed_task(owned_video):
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "alice"}
    with TestClient(main.app) as client:
        assert client.delete(f"/api/video/tasks/{owned_video['id']}").status_code == 204
        assert client.get(f"/api/video/tasks/{owned_video['id']}").status_code == 404
