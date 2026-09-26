from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest
import requests

from routers import studio
from services import studio_tasks as tasks


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "ROOT", tmp_path / "jobs")
    monkeypatch.setattr(tasks, "JOBS", {})


@pytest.fixture
def client(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "start", lambda task_id: None)
    app = FastAPI()
    app.include_router(studio.router)
    app.dependency_overrides[studio.require_video_access] = lambda: {"role": "user", "key_id": "alice"}
    monkeypatch.setattr(studio, "_capabilities", lambda: {"postprocess": {"presets": [
        {"id": studio.post_processing.DEFAULT_PRESET, "available": True}]}})
    # Recovery tests isolate task/asset behavior from media decoding.
    monkeypatch.setattr(studio.post_processing, "probe_video", lambda _: {
        "width": 1280, "height": 720, "frames": 288, "fps": 24.0, "duration": 12., "has_audio": True})
    return TestClient(app)


def _failed(kind="image", **options):
    task = tasks.create(kind, "alice", prompt="Moonlit castle", preset="landscape-hd", **options)
    return tasks.update(task["id"], status="failed", _ready=True)


def test_retry_claim_is_atomic_and_does_not_overlap_a_finishing_worker(isolated):
    task = _failed()
    tasks.JOBS[task["id"]] = SimpleNamespace(done=lambda: False)
    with pytest.raises(ValueError, match="正在结束"):
        tasks.claim_retry(task["id"])
    tasks.JOBS.clear()
    retried = tasks.claim_retry(task["id"], resume_prompt_id="same-comfy-id")
    assert retried["_comfy_prompt_id"] == "same-comfy-id"
    with pytest.raises(ValueError, match="仅失败"):
        tasks.claim_retry(task["id"])


def test_old_completion_callback_cannot_remove_new_retry(isolated, monkeypatch):
    created = []
    class FakeJob:
        finished = False
        def done(self):
            return self.finished
        def add_done_callback(self, callback):
            self.callback = callback
    def create_job(_):
        job = FakeJob()
        created.append(job)
        return job
    monkeypatch.setattr(tasks.asyncio, "to_thread", lambda *args: None)
    monkeypatch.setattr(tasks.asyncio, "create_task", create_job)
    task_id = "a" * 32
    tasks.start(task_id)
    created[0].finished = True
    tasks.start(task_id)
    created[0].callback(created[0])
    assert tasks.JOBS[task_id] is created[1]
    created[1].callback(created[1])
    assert task_id not in tasks.JOBS


def test_failed_image_upload_cannot_retry_without_requested_reference(client):
    response = client.post("/api/studio/images", data={"prompt": "Keep the reference character"},
        files={"images": ("broken.png", b"invalid", "image/png")})
    assert response.status_code == 400
    task = next(tasks.records())
    assert task["status"] == "failed" and task["_ready"] is False
    retry = client.post(f"/api/studio/tasks/{task['id']}/retry")
    assert retry.status_code == 409
    assert "重新上传" in retry.json()["detail"]


def _comfy_session(monkeypatch, *, history=None, running=True, unavailable=False):
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            if unavailable:
                raise requests.ConnectionError("ComfyUI unavailable")
            value = (history or {}) if "/history/" in url else {
                "queue_running": [[0, "comfy-id", {}]] if running else [], "queue_pending": []}
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
    monkeypatch.setattr(studio.requests, "Session", Session)


def test_image_retry_reattaches_running_comfy_prompt(client, monkeypatch):
    task = _failed()
    tasks.update(task["id"], _comfy_prompt_id="comfy-id")
    _comfy_session(monkeypatch)
    response = client.post(f"/api/studio/tasks/{task['id']}/retry")
    assert response.status_code == 202
    assert tasks.load(task["id"])["_comfy_prompt_id"] == "comfy-id"


def test_image_retry_downloads_successful_history_instead_of_resubmitting(client, monkeypatch):
    task = _failed()
    tasks.update(task["id"], _comfy_prompt_id="comfy-id")
    _comfy_session(monkeypatch, history={"comfy-id": {"status": {"status_str": "success"}}})
    assert client.post(f"/api/studio/tasks/{task['id']}/retry").status_code == 202
    assert tasks.load(task["id"])["_comfy_prompt_id"] == "comfy-id"


def test_postprocess_retry_does_not_duplicate_pending_gpu_segment(client, monkeypatch):
    task = _failed("postprocess")
    tasks.update(task["id"], _comfy_prompt_id="comfy-id")
    _comfy_session(monkeypatch)
    response = client.post(f"/api/studio/tasks/{task['id']}/retry")
    assert response.status_code == 409
    assert tasks.load(task["id"])["status"] == "failed"


def test_retry_preserves_receipt_when_comfy_cannot_be_reached(client, monkeypatch):
    task = _failed()
    tasks.update(task["id"], _comfy_prompt_id="comfy-id")
    _comfy_session(monkeypatch, unavailable=True)
    assert client.post(f"/api/studio/tasks/{task['id']}/retry").status_code == 503
    saved = tasks.load(task["id"])
    assert saved["status"] == "failed" and saved["_comfy_prompt_id"] == "comfy-id"


def test_title_upload_is_private_contained_and_reused_on_retry(client):
    title = BytesIO()
    Image.new("RGBA", (100, 40), (230, 220, 200, 120)).save(title, format="PNG")
    response = client.post("/api/studio/postprocess", data={"title_fade": "true"}, files={
        "video": ("source.mp4", b"video fixture", "video/mp4"),
        "title_image": ("../../outside.png", title.getvalue(), "image/png"),
    })
    assert response.status_code == 202
    public = response.json()
    assert public["title_enabled"] is True and public["title_fade"] is True
    assert not any(key.startswith("_") for key in public)
    saved = tasks.load(public["id"])
    title_path = Path(saved["_title_image"])
    assert title_path.name == "title.png" and title_path.is_relative_to(tasks.directory(saved["id"]))
    tasks.update(saved["id"], status="failed")
    assert client.post(f"/api/studio/tasks/{saved['id']}/retry").status_code == 202
    assert tasks.load(saved["id"])["_title_image"] == str(title_path)


def test_title_file_must_be_decodable_png_or_webp(client):
    for name, content in [("title.svg", b"<svg/>"), ("title.png", b"invalid")]:
        response = client.post("/api/studio/postprocess", files={
            "video": ("source.mp4", b"video fixture", "video/mp4"),
            "title_image": (name, content, "image/png"),
        })
        assert response.status_code == 400
    assert all(not task["_ready"] for task in tasks.records())


def test_postprocess_worker_applies_uploaded_title_after_render(isolated, monkeypatch):
    task = _failed("postprocess", loop=False, duration=15, target_fps=30, crossfade=.5,
                   title_enabled=True, title_fade=True)
    folder = tasks.directory(task["id"])
    title = folder / "title.png"
    title.write_bytes(b"title fixture")
    tasks.update(task["id"], _source=str(folder / "source.mp4"), _title_image=str(title))
    def render(source, output, **kwargs):
        output.write_bytes(b"finished video")
        return {"width": 1920, "height": 1080, "duration": 15, "resampling": True, "segments": 4}
    calls = []
    def apply_title(video, title_path, fade=False):
        assert video.read_bytes() == b"finished video"
        calls.append((video, title_path, fade))
        return {"bbox": [192, 108, 650, 250]}
    monkeypatch.setattr(tasks.post_processing, "run_postprocess", render)
    fake = SimpleNamespace(apply_title=apply_title)
    monkeypatch.setitem(sys.modules, "services.title_overlay", fake)
    import services
    monkeypatch.setattr(services, "title_overlay", fake, raising=False)
    tasks._run(task["id"])
    saved = tasks.load(task["id"])
    assert saved["status"] == "completed"
    assert saved["_title_result"]["bbox"] == [192, 108, 650, 250]
    assert calls == [(folder / "finished.mp4", title, True)]


def test_capabilities_require_qwen_weights_not_only_encoder_node(monkeypatch):
    graph = studio.image_generation.build_image_workflow("probe", 1536, 864, 0, ["probe.png"])
    info = {node["class_type"]: {} for node in graph.values()}
    info["EmptyLatentImage"] = {}
    for node_id, field in (("3", "unet_name"), ("4", "clip_name"), ("5", "vae_name")):
        node = graph[node_id]
        info[node["class_type"]] = {"input": {"required": {field: [[node["inputs"][field]]]}}}
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: info)
    monkeypatch.setattr(studio.requests, "Session", Session)
    monkeypatch.setattr(studio.post_processing, "capabilities", lambda info: {"presets": []})
    monkeypatch.setattr(studio, "_capability_cache", (0, None))
    assert studio._capabilities()["image"]["enabled"] is True
    info["UNETLoader"]["input"]["required"]["unet_name"] = [[]]
    monkeypatch.setattr(studio, "_capability_cache", (0, None))
    unavailable = studio._capabilities()["image"]
    assert unavailable["enabled"] is False
    assert unavailable["missing_models"] == ["qwen_image_2.1_bf16.safetensors"]
