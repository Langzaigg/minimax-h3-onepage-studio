from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from routers import studio
from services import studio_tasks as tasks, video_generation as videos


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(studio.router)
    app.dependency_overrides[studio.require_video_access] = lambda: {"role": "user", "key_id": "alice"}
    monkeypatch.setattr(tasks, "start", lambda _: None)
    monkeypatch.setattr(studio, "_capabilities", lambda: {"postprocess": {"presets": [
        {"id": studio.post_processing.DEFAULT_PRESET, "available": True}]}})
    # These ownership/copy tests use byte fixtures; decode is tested separately.
    monkeypatch.setattr(studio.post_processing, "probe_video", lambda _: {
        "width": 1280, "height": 720, "frames": 288, "fps": 24.0, "duration": 12., "has_audio": True})
    return TestClient(app)


def png():
    file = BytesIO()
    Image.new("RGBA", (32, 32), (40, 80, 120, 200)).save(file, format="PNG")
    return file.getvalue()


def video_record(owner="alice", prompt="Video", created=None):
    record = videos.create_task_record(mode="hybrid_to_video", prompt=prompt, duration=8,
        width=1024, height=576, resolution_megapixels=.5, aspect_ratio="16:9",
        seed=2**63-1, warnings=[], owner=owner,
        extras={"hybrid_preset": "anime", "hybrid_model": "DasiwaMinimaxH3_dasiwaHybridV2_int8.safetensors"})
    record["status"] = "completed"
    if created:
        record["created_at"] = created
    videos._save_metadata(record)
    return record


def post_record(owner="alice", status="completed"):
    task = tasks.create("postprocess", owner, prompt="Keep the character", seed=2**63-1,
        preset=studio.post_processing.DEFAULT_PRESET, loop=True, duration=15, requested_duration=12,
        target_fps=30, crossfade=.5, title_enabled=True, title_fade=False)
    folder = tasks.directory(task["id"])
    for name, content in (("source.mp4", b"original source"), ("finished.mp4", b"finished output"),
                          ("reference.png", png()), ("title.png", png())):
        (folder / name).write_bytes(content)
    return tasks.update(task["id"], status=status, _ready=True, _source=str(folder / "source.mp4"),
        _reference=str(folder / "reference.png"), _title_image=str(folder / "title.png"),
        _assets={"video": str(folder / "finished.mp4")})


def test_image_inputs_are_retrievable_private_and_seed_is_exact(client):
    response = client.post("/api/studio/images", data={"prompt": "Keep the character", "seed": str(2**63-1)},
        files=[("images", ("character.png", png(), "image/png"))])
    assert response.status_code == 202
    public = response.json()
    assert public["seed_text"] == "9223372036854775807"
    assert (public["expected_input_count"], public["missing_input_count"]) == (1, 0)
    assert public["input_images"] == [{
        "filename": "character.png", "url": f"/api/studio/tasks/{public['id']}/assets/input_image_0"}]
    url = public["input_images"][0]["url"]
    assert client.get(url).content == png()
    assert not any(key.startswith("_") for key in public)
    assert client.get(url.replace("input_image_0", "input_image_8")).status_code == 404
    client.app.dependency_overrides[studio.require_video_access] = lambda: {"role": "user", "key_id": "bob"}
    assert client.get(url).status_code == 404


def test_missing_original_images_are_counted_without_exposing_paths_or_mutating_metadata(client):
    response = client.post("/api/studio/images", data={"prompt": "Keep both references"}, files=[
        ("images", ("character.png", png(), "image/png")),
        ("images", ("background.png", png(), "image/png")),
    ])
    assert response.status_code == 202
    task = tasks.load(response.json()["id"])
    folder = tasks.directory(task["id"])
    output = folder / "result.png"
    output.write_bytes(png())
    tasks.update(task["id"], status="completed", _assets={"image": str(output)})
    metadata = folder / "metadata.json"
    original = metadata.read_bytes()
    for missing in (1, 2):
        Path(task["_inputs"][missing - 1]).unlink()
        result = client.get(f"/api/studio/tasks/{task['id']}")
        public = result.json()
        assert (public["expected_input_count"], public["missing_input_count"]) == (2, missing)
        assert len(public["input_images"]) == 2 - missing
        assert str(folder) not in result.text
        assert metadata.read_bytes() == original
        assert client.get(public["assets"]["image"]).content == png()
        listed = client.get("/api/studio/library", params={"kind": "image"}).json()["tasks"][0]
        assert listed["missing_input_count"] == missing
    empty = tasks.create("image", "alice", prompt="No references", preset="landscape-hd")
    public = tasks.public(empty)
    assert (public["expected_input_count"], public["missing_input_count"]) == (0, 0)


def test_private_inputs_reject_paths_outside_task_and_symlinks(client, tmp_path):
    task = post_record()
    external = tmp_path / "outside.mp4"
    external.write_bytes(b"private")
    tasks.update(task["id"], _source=str(external), _title_image=str(external), _inputs=[str(external)])
    for name in ("source_video", "title_image", "input_image_0"):
        assert client.get(f"/api/studio/tasks/{task['id']}/assets/{name}").status_code == 404
    public = client.get(f"/api/studio/tasks/{task['id']}").json()
    assert "source_video" not in public["assets"] and public["input_images"] == []
    link = tasks.directory(task["id"]) / "linked.mp4"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links")
    tasks.update(task["id"], _source=str(link))
    assert client.get(f"/api/studio/tasks/{task['id']}/assets/source_video").status_code == 404
    assert client.post("/api/studio/postprocess", data={"source_studio_task_id": task["id"]}).status_code == 404


def test_library_searches_all_owned_history_before_pagination_and_normalizes_timezones(client):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    images = []
    for index in range(27):
        task = tasks.create("image", "alice", prompt="Moonlit castle", preset="landscape-hd")
        task = tasks.update(task["id"], status="completed", created_at=(base + timedelta(minutes=index)).isoformat(),
                            original_prompt="deep archive original" if index == 0 else "Moon")
        images.append(task)
    old_video = video_record(created=(base + timedelta(minutes=2, seconds=30)).astimezone(timezone(timedelta(hours=8))).isoformat())
    new_video = video_record(created=(base + timedelta(minutes=40)).isoformat())
    tasks.create("image", "bob", prompt="deep archive private", preset="landscape-hd")
    video_record("bob", "private video")
    page = client.get("/api/studio/library", params={"limit": 5, "offset": 25}).json()
    assert (page["total"], page["limit"], page["offset"]) == (29, 5, 25)
    assert [task["id"] for task in page["tasks"]] == [old_video["id"], images[2]["id"], images[1]["id"], images[0]["id"]]
    first = client.get("/api/studio/library", params={"limit": 1}).json()["tasks"][0]
    assert first["id"] == new_video["id"] and first["kind"] == "video"
    assert first["seed_text"] == "9223372036854775807"
    found = client.get("/api/studio/library", params={"q": "DEEP ARCHIVE"}).json()
    assert found["total"] == 1 and found["tasks"][0]["id"] == images[0]["id"]
    assert client.get("/api/studio/library", params={"kind": "video", "q": "Dasiwa"}).json()["total"] == 2
    assert client.get("/api/studio/library", params={"q": "Qwen Image 2.1"}).json()["total"] == 27
    assert client.get("/api/studio/library", params={"kind": "postprocess"}).json()["total"] == 0
    client.app.dependency_overrides[studio.require_video_access] = lambda: {"role": "admin", "key_id": "admin"}
    assert client.get("/api/studio/library").json()["total"] == 31


def test_library_does_not_migrate_or_modify_original_video_assets(client):
    record = video_record()
    source = videos.task_dir(record["id"]) / "result.mp4"
    source.write_bytes(b"legacy output")
    record["_assets"]["video"] = str(source)
    videos._save_metadata(record)
    metadata = videos.metadata_path(record["id"])
    before = metadata.read_bytes()
    response = client.get("/api/studio/library", params={"kind": "video"})
    assert response.status_code == 200 and response.json()["tasks"][0]["assets"]["video"]
    assert source.read_bytes() == b"legacy output" and metadata.read_bytes() == before


@pytest.mark.parametrize("asset_name,expected", [("source_video", b"original source"), ("video", b"finished output")])
def test_postprocess_server_side_reuse_copies_inputs_and_all_parameters(client, monkeypatch, asset_name, expected):
    original = post_record()
    old_folder = tasks.directory(original["id"])
    before = (old_folder / "metadata.json").read_bytes()
    payload = {"source_studio_task_id": original["id"], "source_studio_asset": asset_name,
               "preset": original["preset"], "prompt": "Sharpen the existing lines", "seed": str(2**63-1),
               "loop": "true", "duration": "11.5", "target_fps": "24", "crossfade": "0.25", "title_fade": "true",
               "cadence_stride": "1", "interpolation": "none", "target_resolution": "1440p"}
    response = client.post("/api/studio/postprocess", data=payload,
        files={"title_image": ("logo.png", png(), "image/png")})
    assert response.status_code == 202, response.text
    public = response.json()
    assert public["id"] != original["id"] and public["seed_text"] == "9223372036854775807"
    assert public["requested_duration"] == 11.5 and public["title_enabled"] and public["title_fade"]
    assert public["source_studio_asset"] == asset_name and public["source_studio_task_id"] == original["id"]
    saved = tasks.load(public["id"])
    assert Path(saved["_source"]).read_bytes() == expected
    assert Path(saved["_source"]).parent == tasks.directory(saved["id"])
    assert Path(saved["_reference"]).read_bytes() == (old_folder / "reference.png").read_bytes()
    assert client.get(public["assets"]["source_video"]).content == expected
    assert client.get(public["assets"]["title_image"]).content == png()
    received = {}
    def render(source, output, **kwargs):
        received.update(kwargs)
        output.write_bytes(b"new rendered output")
        return {"width": 1920, "height": 1080, "duration": 11.49, "resampling": True, "segments": 3}
    from services import title_overlay
    monkeypatch.setattr(tasks.post_processing, "run_postprocess", render)
    monkeypatch.setattr(title_overlay, "apply_title", lambda *args, **kwargs: {"bbox": [200, 110, 300, 140]})
    tasks._run(saved["id"])
    completed = tasks.public(tasks.load(saved["id"]))
    assert completed["duration"] == 11.49 and completed["requested_duration"] == 11.5
    assert received["seed"] == 2**63-1 and received["loop_seconds"] == 11.5
    assert received["fps"] == 24 and received["crossfade"] == .25
    assert received["cadence_stride"] == public["cadence_stride"] == 1
    assert received["target_resolution"] == public["target_resolution"] == "1440p"
    assert received["prompt"] == payload["prompt"]
    assert tasks.load(saved["id"])["prompt"] == payload["prompt"]
    assert (old_folder / "metadata.json").read_bytes() == before
    assert (old_folder / "source.mp4").read_bytes() == b"original source"


def test_postprocess_reuse_enforces_owner_kind_completion_and_exclusive_source(client):
    other = post_record("bob")
    assert client.post("/api/studio/postprocess", data={"source_studio_task_id": other["id"]}).status_code == 404
    assert client.get(f"/api/studio/tasks/{other['id']}/assets/source_video").status_code == 404
    image = tasks.create("image", "alice", prompt="Moon")
    assert client.post("/api/studio/postprocess", data={"source_studio_task_id": image["id"]}).status_code == 400
    original = post_record(status="running")
    assert client.post("/api/studio/postprocess", data={"source_studio_task_id": original["id"], "source_studio_asset": "video"}).status_code == 400
    for data, files in [({}, None), ({"source_studio_task_id": original["id"], "video_task_id": "a"*32}, None),
                        ({"source_studio_task_id": original["id"]}, {"video": ("in.mp4", b"video", "video/mp4")}),
                        ({"source_studio_task_id": original["id"], "source_studio_asset": "../../metadata"}, None)]:
        assert client.post("/api/studio/postprocess", data=data, files=files).status_code == 400
    Path(original["_source"]).unlink()
    assert client.post("/api/studio/postprocess", data={"source_studio_task_id": original["id"]}).status_code == 404


def test_old_postprocess_metadata_gets_duration_fallback_without_migration(client):
    original = post_record()
    original.pop("requested_duration")
    tasks.save(original)
    before = (tasks.directory(original["id"]) / "metadata.json").read_bytes()
    response = client.get(f"/api/studio/tasks/{original['id']}").json()
    assert response["requested_duration"] == original["duration"]
    assert (tasks.directory(original["id"]) / "metadata.json").read_bytes() == before
