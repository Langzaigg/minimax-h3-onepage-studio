from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import requests

from routers import studio
from services import studio_tasks as tasks


LEGACY = sorted(studio.ARCHIVED_POSTPROCESS_PRESETS)


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.include_router(studio.router)
    app.dependency_overrides[studio.require_video_access] = lambda: {"role": "user", "key_id": "alice"}
    started = []
    monkeypatch.setattr(tasks, "start", started.append)
    return TestClient(app), started


@pytest.mark.parametrize("preset", LEGACY)
def test_archived_post_requests_reject_before_model_probing_or_task_creation(api, monkeypatch, preset):
    client, started = api
    monkeypatch.setattr(studio, "_capabilities", lambda: pytest.fail("Archived request must not probe or queue GPU work"))
    response = client.post("/api/studio/postprocess", data={"preset": preset},
                           files={"video": ("input.mp4", b"placeholder", "video/mp4")})
    assert response.status_code == 400
    assert response.json()["detail"] == studio.ARCHIVED_POSTPROCESS_MESSAGE
    assert started == []
    assert not list(tasks.ROOT.glob("*/metadata.json"))


@pytest.mark.parametrize("preset", LEGACY)
def test_archived_history_is_readable_but_cannot_bypass_archive_through_retry(api, preset):
    client, started = api
    data = tasks.create("postprocess", "alice", preset=preset, prompt="Historical parameters", seed=42)
    folder = tasks.directory(data["id"])
    source = folder / "source.mp4"
    source.write_bytes(b"original-video")
    tasks.update(data["id"], status="failed", _source=str(source), _ready=True)
    response = client.get(f"/api/studio/tasks/{data['id']}")
    assert response.status_code == 200
    assert response.json()["preset"] == preset
    assert client.get(response.json()["assets"]["source_video"]).content == b"original-video"
    retry = client.post(f"/api/studio/tasks/{data['id']}/retry")
    assert retry.status_code == 409
    assert retry.json()["detail"] == studio.ARCHIVED_POSTPROCESS_MESSAGE
    assert tasks.load(data["id"])["status"] == "failed"
    assert started == []
    client.app.dependency_overrides[studio.require_video_access] = lambda: {"role": "user", "key_id": "bob"}
    assert client.get(f"/api/studio/tasks/{data['id']}").status_code == 404
    assert client.post(f"/api/studio/tasks/{data['id']}/retry").status_code == 404


def test_capabilities_mark_archived_presets_unavailable_even_when_models_exist(monkeypatch):
    class OfflineSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            raise requests.ConnectionError("CPU test has no Comfy server")

    monkeypatch.setattr(studio.requests, "Session", OfflineSession)
    monkeypatch.setattr(studio.post_processing, "capabilities", lambda _: {"presets": [
        {"id": name, "available": True} for name in [*LEGACY, *studio.post_processing.ACTIVE_PRESETS]]})
    caps = studio._capabilities()
    by_id = {item["id"]: item for item in caps["postprocess"]["presets"]}
    for name in LEGACY:
        assert by_id[name]["available"] is False
        assert by_id[name]["archived"] is True
        assert by_id[name]["hide_when_unavailable"] is True
        assert by_id[name]["reason"] == studio.ARCHIVED_POSTPROCESS_MESSAGE
    assert set(studio.post_processing.ACTIVE_PRESETS) == {"redgraft_msr", "dlss_nr"}
    assert all(by_id[name]["available"] for name in studio.post_processing.ACTIVE_PRESETS)
