from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

import main
from routers.access import require_video_access
from routers import studio
from services import studio_tasks as tasks


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "ROOT", tmp_path / "jobs")
    monkeypatch.setattr(tasks, "start", lambda task_id: None)
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "alice"}
    with TestClient(main.app, raise_server_exceptions=True) as client:
        yield client
    main.app.dependency_overrides.clear()


def test_upload_private_assets_and_owner_isolation(client, tmp_path):
    image = BytesIO()
    Image.new("RGB", (64, 64), "blue").save(image, format="PNG")
    response = client.post("/api/studio/images", data={"prompt": "自然延展背景"},
                           files={"images": ("../../source.png", image.getvalue(), "image/png")})
    assert response.status_code == 202
    public = response.json()
    assert not any(key.startswith("_") for key in public)
    task_id = public["id"]
    saved = tasks.load(task_id)
    assert Path(saved["_inputs"][0]).is_relative_to(tasks.directory(task_id))
    output = tasks.directory(task_id) / "result.png"
    output.write_bytes(image.getvalue())
    tasks.update(task_id, _assets={"image": str(output)}, status="completed")
    assert client.get(f"/api/studio/tasks/{task_id}/assets/image").status_code == 200
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "bob"}
    assert client.get(f"/api/studio/tasks/{task_id}").status_code == 404
    assert client.get(f"/api/studio/tasks/{task_id}/assets/image").status_code == 404
    assert client.get("/api/studio/tasks").json()["tasks"] == []


def test_invalid_image_and_path_escape_rejected(client, tmp_path):
    assert client.post("/api/studio/images", data={"prompt": "x", "preset": "bad"}).status_code == 400
    assert client.post("/api/studio/images", data={"prompt": "x"},
                       files={"images": ("bad.png", b"not an image", "image/png")}).status_code == 400
    data = tasks.create("image", "alice", prompt="x")
    external = tmp_path / "secret.txt"
    external.write_text("secret")
    tasks.update(data["id"], _assets={"image": str(external)})
    assert client.get(f"/api/studio/tasks/{data['id']}/assets/image").status_code == 404


def test_postprocess_cannot_reuse_another_users_video(client, monkeypatch):
    monkeypatch.setattr(studio, "_capabilities", lambda: {"postprocess": {"presets": [
        {"id": studio.post_processing.DEFAULT_PRESET, "available": True}]}})
    monkeypatch.setattr(studio.video_generation, "load_metadata", lambda _: {"_owner": "bob", "status": "completed"})
    response = client.post("/api/studio/postprocess", data={"video_task_id": "a"*32})
    assert response.status_code == 404


def test_image_worker_records_results_and_errors(client, monkeypatch):
    data = tasks.create("image", "alice", prompt="moon", preset="landscape-hd", polish_prompt_first=False)
    def worker(prompt, folder, **kwargs):
        kwargs["on_submitted"]("gpu-id")
        kwargs["progress_callback"](50, "采样")
        return {"prompt": prompt, "width": 2048, "height": 1152, "image_path": str(folder / "reference.png")}
    monkeypatch.setattr(tasks.image_generation, "run_image_generation", worker)
    tasks._run(data["id"])
    result = tasks.load(data["id"])
    assert (result["status"], result["progress"], result["_comfy_prompt_id"]) == ("completed", 100, "gpu-id")
    def broken(*args, **kwargs):
        raise RuntimeError("sampler failed")
    monkeypatch.setattr(tasks.image_generation, "run_image_generation", broken)
    tasks._run(data["id"])
    assert tasks.load(data["id"])["error"] == "sampler failed"
