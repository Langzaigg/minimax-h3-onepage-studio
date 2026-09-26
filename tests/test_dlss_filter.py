from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import av
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from routers import studio
from services import dlss_filter as df, post_processing as pp, studio_tasks as tasks


def png(size=(321, 257), alpha=101):
    stream = BytesIO()
    Image.new("RGBA", size, (30, 60, 90, alpha)).save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("strength", [0, .25, 1])
def test_nr_filter_graph_has_no_scale_or_interpolation_and_crops_only_padding(kind, strength):
    graph = df.build_filter_workflow("source", media_kind=kind, source_width=321, source_height=257,
                                     options={"look": "detail", "strength": strength})
    assert graph["4"]["inputs"]["operation"] == "Neural rendering only"
    assert graph["4"]["inputs"]["effect_strength"] == strength
    assert graph["3"]["inputs"]["right"] == graph["3"]["inputs"]["bottom"] == 31
    assert graph["5"]["inputs"] == {"image": ["4", 0], "x": 0, "y": 0,
                                    "width": 322 if kind == "video" else 321,
                                    "height": 258 if kind == "video" else 257}
    assert not any(n["class_type"] in {"ImageScale", "RIFE VFI", "StudioFrameInterpolateSampled", "RTXVideoSuperResolution"}
                   for n in graph.values())
    if kind == "video":
        assert graph["90"]["inputs"]["fps"] == ["2", 2]
    assert pp.required_models("dlss_nr") == []


@pytest.mark.parametrize("options", [{"strength": float("nan")}, {"strength": float("inf")},
                                     {"strength": -1}, {"strength": 1.1}, {"look": "shell-command"}])
def test_filter_parameters_are_strict(options):
    with pytest.raises(ValueError):
        df.filter_options(**options)


def test_image_filter_worker_keeps_odd_size_and_original_alpha(tmp_path, monkeypatch):
    source, output = tmp_path/"source.png", tmp_path/"finished.png"
    source.write_bytes(png())
    graphs = []
    class Client:
        base_url = "http://test.invalid"
        session = SimpleNamespace(trust_env=True, get=lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {n["class_type"]: {} for n in
                df.build_filter_workflow("source.png", media_kind="image").values()}))
        def __init__(self, *a, **k): pass
        def upload(self, path): return Path(path).name
        def render(self, graph, destination, **kwargs):
            graphs.append(graph)
            Image.new("RGB", (321, 257), (80, 90, 100)).save(destination)
            return {"prompt_id": "fake-test-only", "path": str(destination)}
    monkeypatch.setattr(pp, "PostprocessComfyClient", Client)
    monkeypatch.setattr(pp, "_dlss_runtime_status", lambda _: ([], False))
    result = df.run_filter(source, output, media_kind="image", preview=True, options={"strength": 0})
    assert result["alpha_preserved"] and not result["effect_enabled"]
    with Image.open(output) as filtered, Image.open(source) as original:
        assert filtered.size == original.size == (321, 257)
        assert filtered.getchannel("A").tobytes() == original.getchannel("A").tobytes()
    assert Path(result["preview_source"]).is_file()
    assert graphs[0]["4"]["inputs"]["operation"] == "Neural rendering only"


def video_file(path, *, fps=12):
    pp._run_ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size=64x48:rate={fps}:duration=0.75",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000:duration=0.75",
        "-t", "0.75", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])
    return path


def test_preview_time_uses_actual_display_timestamp_and_extracts_exact_frame(tmp_path):
    source = video_file(tmp_path/"source.mp4")
    index, timestamp = df.preview_position(source, time_seconds=.21)
    assert index == 2 and timestamp == pytest.approx(2/12)
    destination = tmp_path/"preview.png"
    df.extract_preview(source, destination, "video", index)
    with av.open(str(source)) as container:
        frame = list(container.decode(video=0))[index].to_image()
    with Image.open(destination) as actual:
        assert actual.tobytes() == frame.tobytes()
    assert df.preview_position(source, frame_index=4) == (4, pytest.approx(4/12))


@pytest.mark.parametrize("variable_rate,offset", [(False, 0), (True, 0), (True, 2)])
def test_filter_mux_preserves_source_pts_durations_and_audio_packets(tmp_path, variable_rate, offset):
    from services.media_overlay import _video_signature
    source, rendered, output = (tmp_path/name for name in ("source.mp4", "rendered.mp4", "finished.mp4"))
    args = ["-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000:duration=1"]
    if variable_rate:
        args += ["-vf", "select='not(eq(n,3)+eq(n,4)+eq(n,7))'", "-fps_mode", "vfr"]
    pp._run_ffmpeg([*args, "-output_ts_offset", str(offset), "-c:v", "libx264", "-c:a", "aac", str(source)])
    count = len(df.video_timing(source)["timestamps"])
    pp._run_ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size=64x48:rate=10:duration={count/10}",
                    "-frames:v", str(count), "-an", "-c:v", "libx264", str(rendered)])
    result = df.restore_video_timing(rendered, source, output, has_audio=True)
    before, after = _video_signature(source), _video_signature(output)
    assert before["timestamps"] == after["timestamps"]
    assert before["audio"] == after["audio"]
    assert df.video_timing(source)["durations"] == df.video_timing(output)["durations"]
    assert result == {"timestamps_preserved": True, "source_timing_restored": variable_rate or bool(offset),
                      "variable_frame_rate": variable_rate, "encoding_padding_removed": False}
    if not variable_rate:
        assert pp.probe_video(source)["fps"] == pytest.approx(pp.probe_video(output)["fps"])
    if variable_rate:
        assert df.preview_position(source, time_seconds=.42) == (2, pytest.approx(.2))


def test_filter_video_encoder_padding_is_cropped_without_resizing_odd_source(tmp_path):
    source, rendered, output = (tmp_path/name for name in ("odd.mp4", "even.mp4", "finished.mp4"))
    pp._run_ffmpeg(["-f", "lavfi", "-i", "testsrc=size=65x49:rate=10:duration=0.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv444p", str(source)])
    pp._run_ffmpeg(["-i", str(source), "-vf", "pad=66:50:0:0", "-c:v", "libx264", str(rendered)])
    result = df.restore_video_timing(rendered, source, output, has_audio=False)
    assert result["encoding_padding_removed"]
    assert pp.probe_video(output) == pp.probe_video(source)
    assert df.video_timing(output)["timestamps"] == df.video_timing(source)["timestamps"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "ROOT", tmp_path/"jobs")
    monkeypatch.setattr(tasks, "start", lambda _: None)
    monkeypatch.setattr(studio, "_capabilities", lambda: {"postprocess": {"presets": [
        {"id": key, "available": True} for key in pp.ACTIVE_PRESETS]}})
    app = FastAPI()
    app.include_router(studio.router)
    app.dependency_overrides[studio.require_video_access] = lambda: {"role": "user", "key_id": "alice"}
    return TestClient(app)


def test_filter_image_upload_and_qwen_history_reuse_are_private(client):
    response = client.post("/api/studio/postprocess", data={"preset": "dlss_nr", "preview": "true"},
                           files={"image": ("../../source.png", png(), "image/png")})
    assert response.status_code == 202, response.text
    task = response.json()
    assert task["kind"] == "postprocess" and task["media_kind"] == task["output_kind"] == "image"
    assert task["target_fps"] is None and client.get(task["assets"]["source_image"]).status_code == 200
    qwen = tasks.create("image", "alice", prompt="x")
    path = tasks.directory(qwen["id"])/"result.png"
    path.write_bytes(png((77, 91)))
    tasks.update(qwen["id"], status="completed", _assets={"image": str(path)})
    reused = client.post("/api/studio/postprocess", data={"preset": "dlss_nr",
        "source_studio_task_id": qwen["id"], "source_studio_asset": "image"})
    assert reused.status_code == 202, reused.text
    assert reused.json()["source_info"]["width"] == 77
    tasks.update(qwen["id"], _owner="bob")
    assert client.post("/api/studio/postprocess", data={"preset": "dlss_nr",
        "source_studio_task_id": qwen["id"], "source_studio_asset": "image"}).status_code == 404


def test_new_routes_reject_retired_modes_and_hidden_timing_changes(client):
    for preset in pp.PRESETS.keys() - pp.ACTIVE_PRESETS:
        assert client.post("/api/studio/postprocess", data={"preset": preset}).status_code == 400
    for options in ({"interpolation": "2x"}, {"loop": "true"}, {"dlss_strength": "NaN"},
                    {"dlss_look": "invalid"}):
        response = client.post("/api/studio/postprocess", data={"preset": "dlss_nr", **options},
            files={"image": ("source.png", png(), "image/png")})
        assert response.status_code in {400, 422}


def test_video_preview_accepts_seconds_without_reupload_and_keeps_real_frame_time(client, tmp_path):
    original = tasks.create("postprocess", "alice", preset="dlss_nr", media_kind="video")
    source = video_file(tasks.directory(original["id"])/"source.mp4")
    tasks.update(original["id"], _source=str(source), status="completed")
    response = client.post("/api/studio/postprocess", data={"preset": "dlss_nr", "preview": "true",
        "preview_time": ".21", "preview_frame": "9999", "source_studio_task_id": original["id"]})
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["preview_frame"] == 2 and result["preview_timestamp"] == pytest.approx(2/12)
    assert result["target_fps"] == 12 and result["output_kind"] == "image"
    assert client.post("/api/studio/postprocess", data={"preset": "dlss_nr", "preview": "true",
        "preview_time": "99", "source_studio_task_id": original["id"]}).status_code == 400


@pytest.mark.parametrize("interpolation,fps", [("none", 24), ("2x", 48)])
def test_ltx_api_derives_fps_from_source(client, monkeypatch, interpolation, fps):
    monkeypatch.setattr(pp, "probe_video", lambda _: {"width": 960, "height": 540, "frames": 48,
        "fps": 24., "duration": 2., "has_audio": False})
    response = client.post("/api/studio/postprocess", data={"preset": "redgraft_msr", "interpolation": interpolation},
        files={"video": ("source.mp4", b"fixture validated through mocked media probe", "video/mp4")})
    assert response.status_code == 202 and response.json()["target_fps"] == fps
    mismatch = client.post("/api/studio/postprocess", data={"preset": "redgraft_msr", "target_fps": "30"},
        files={"video": ("source.mp4", b"fixture", "video/mp4")})
    assert mismatch.status_code == 400


def test_overlay_options_and_ownership_checked_before_queue(client):
    foreign = tasks.create("image", "bob", prompt="overlay")
    overlay = tasks.directory(foreign["id"])/"overlay.png"
    overlay.write_bytes(png((10, 10)))
    tasks.update(foreign["id"], status="completed", _assets={"image": str(overlay)})
    response = client.post("/api/studio/postprocess", data={"preset": "dlss_nr",
        "overlay_studio_task_id": foreign["id"], "overlay_studio_asset": "image"},
        files={"image": ("source.png", png(), "image/png")})
    assert response.status_code == 404
    invalid = client.post("/api/studio/postprocess", data={"preset": "dlss_nr", "overlay_x": "1"},
        files={"image": ("source.png", png(), "image/png"), "overlay_media": ("overlay.png", png(), "image/png")})
    assert invalid.status_code == 400
    valid = client.post("/api/studio/postprocess", data={"preset": "dlss_nr", "overlay_anchor": "bottom-right", "overlay_width_percent": "30"},
        files={"image": ("source.png", png(), "image/png"), "overlay_media": ("overlay.png", png((10, 20)), "image/png")})
    assert valid.status_code == 202, valid.text
    task = valid.json()
    assert task["overlay_media_kind"] == "image" and task["overlay_options"]["width_percent"] == 30
    assert client.get(task["assets"]["overlay_media"]).status_code == 200


def test_worker_preview_composites_at_selected_frame_timestamp_and_exposes_reusable_image(client, tmp_path, monkeypatch):
    source = video_file(tmp_path/"source.mp4")
    response = client.post("/api/studio/postprocess", data={"preset": "dlss_nr", "preview": "true",
        "preview_time": ".21", "overlay_start": ".15", "overlay_end": ".20"},
        files={"video": ("source.mp4", source.read_bytes(), "video/mp4"),
               "overlay_media": ("layer.png", png((10, 10), alpha=255), "image/png")})
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    def render_mock(source, output, **options):
        assert options["preview_time"] == .21 and options["media_kind"] == "video"
        Image.new("RGB", (64, 48), "black").save(output)
        preview = output.parent/"preview_source.png"
        Image.new("RGB", (64, 48), "black").save(preview)
        return {"width": 64, "height": 48, "duration": 0, "resampling": False, "segments": 1,
                "fps": None, "frames": 1, "preview_timestamp": 2/12, "preview_source": str(preview)}
    monkeypatch.setattr(pp, "run_postprocess", render_mock)
    tasks._run(task_id)
    saved = tasks.load(task_id)
    assert saved["status"] == "completed" and saved["current_stage"] == "已完成"
    assert saved["_result"]["overlay"]["overlay"]["applied"]
    assert saved["_result"]["overlay"]["overlay"]["options"]["preview_time"] == pytest.approx(2/12)
    public = tasks.public(saved)
    with Image.open(BytesIO(client.get(public["assets"]["image"]).content)) as actual:
        assert actual.getpixel((2, 2))[:3] == (30, 60, 90)
        assert actual.getpixel((20, 20))[:3] == (0, 0, 0)
    assert client.get(public["assets"]["preview_source"]).status_code == 200
    reused = client.post("/api/studio/postprocess", data={"preset": "dlss_nr",
        "source_studio_task_id": task_id, "source_studio_asset": "image"})
    assert reused.status_code == 202 and reused.json()["media_kind"] == "image"


def test_looping_video_overlay_frame_offset_uses_modulo_contract(client, tmp_path):
    source = video_file(tmp_path/"overlay.mp4")
    response = client.post("/api/studio/postprocess", data={"preset": "dlss_nr",
        "overlay_loop": "true", "overlay_frame_time": "2"},
        files={"image": ("source.png", png(), "image/png"),
               "overlay_media": ("overlay.mp4", source.read_bytes(), "video/mp4")})
    assert response.status_code == 202, response.text
    assert response.json()["overlay_media_kind"] == "video"
