import asyncio
import json
from pathlib import Path
import shutil

from PIL import Image
import pytest

from services import auto_postprocess as auto, studio_tasks as tasks, video_generation as video
from services.post_processing import _run_ffmpeg


def parent_record(*, enabled=True, owner="owner-a", seed=9007199254740993123):
    data = video.create_task_record(mode="first_frame", prompt="Final polished camera movement",
        duration=3, width=64, height=48, resolution_megapixels=.4, aspect_ratio="4:3", seed=seed,
        warnings=[], owner=owner, extras={"auto_postprocess": enabled, "target_resolution": "1080p",
                                        "original_prompt": "unpolished"})
    source = video.task_dir(data["id"]) / "outputs" / "fixture.mp4"
    source.parent.mkdir(parents=True)
    _run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:size=64x48:rate=24", "-t", .5,
                 "-c:v", "libx264", source])
    reference = video.task_dir(data["id"]) / "inputs" / "first.png"
    reference.parent.mkdir(parents=True)
    Image.new("RGB", (24, 16), "red").save(reference)
    data.update(status="completed", progress=100, _assets={"video": str(source), "first_frame": str(reference)})
    video._save_metadata(data)
    return data


@pytest.fixture
def starts(monkeypatch):
    result = []
    monkeypatch.setattr(tasks, "start", result.append)
    return result


def test_handoff_copies_private_media_preserves_owner_seed_and_real_source_geometry(starts):
    parent = parent_record()
    child_id = asyncio.run(auto.ensure_auto_postprocess(parent["id"]))
    child = tasks.load(child_id)
    assert child_id == auto.child_task_id(parent["id"])
    assert starts == [child_id]
    assert child["_owner"] == "owner-a" and child["seed"] == 9007199254740993123
    assert tasks.public(child)["seed_text"] == "9007199254740993123"
    assert child["prompt"] == parent["prompt"]
    assert child["source_info"]["width"] == 64 and child["source_info"]["height"] == 48
    assert child["duration"] == child["requested_duration"] == .5
    assert child["source_info"]["frames"] == 12
    assert child["interpolation"] == "none" and child["target_fps"] == 24
    assert child["preset"] == "redgraft_msr" and child["pipeline_contract"] == 2
    assert child["_ready"] and child["status"] == "queued" and not child["loop"]
    assert child["target_resolution"] == "1080p" and child["overlay_enabled"] is False
    for field, asset in [("_source", "video"), ("_reference", "first_frame")]:
        copy = Path(child[field]); original = video.asset_path(parent["id"], asset)
        assert copy.parent == tasks.directory(child_id)
        assert copy.resolve() != original.resolve() and copy.read_bytes() == original.read_bytes()
    summary = video.public_task(video.load_metadata(parent["id"]))
    assert summary["status"] == "completed" and summary["auto_postprocess_task_id"] == child_id


def test_concurrent_completion_hooks_create_and_start_exactly_one_child(starts):
    parent = parent_record()
    async def twice():
        return await asyncio.gather(auto.ensure_auto_postprocess(parent["id"]), auto.ensure_auto_postprocess(parent["id"]))
    ids = asyncio.run(twice())
    assert ids[0] == ids[1] and starts == ids[:1]
    assert len(list(tasks.ROOT.glob("*/metadata.json"))) == 1


def test_off_and_unfinished_parent_never_creates_child(starts):
    parent = parent_record(enabled=False)
    assert asyncio.run(auto.ensure_auto_postprocess(parent["id"])) is None
    parent.update(auto_postprocess=True, status="running"); video._save_metadata(parent)
    assert asyncio.run(auto.ensure_auto_postprocess(parent["id"])) is None
    assert starts == [] and not tasks.ROOT.exists()


def test_resume_rebuilds_interrupted_preparation_after_studio_resume(starts):
    parent = parent_record(); child_id = auto.child_task_id(parent["id"])
    tasks.save({"id": child_id, "kind": "postprocess", "status": "preparing", "_ready": False,
                "created_at": tasks.now(), "_owner": "owner-a", "auto_postprocess_parent_id": parent["id"]})
    async def restart():
        await tasks.resume()
        assert tasks.load(child_id)["status"] == "failed"
        await auto.resume_completed_tasks()
    asyncio.run(restart())
    assert starts == [child_id] and tasks.load(child_id)["_ready"]
    assert tasks.load(child_id)["status"] == "queued"
    assert len(list(tasks.ROOT.glob("*/metadata.json"))) == 1


def test_resume_never_restarts_ready_child_and_failure_is_available_for_manual_retry(starts):
    parent = parent_record()
    child_id = asyncio.run(auto.ensure_auto_postprocess(parent["id"]))
    async def restart():
        await tasks.resume()
        await auto.resume_completed_tasks()
        await auto.resume_completed_tasks()
    asyncio.run(restart())
    assert starts == [child_id]
    assert tasks.load(child_id)["status"] == "failed"
    assert video.load_metadata(parent["id"])["status"] == "completed"
    assert video.public_task(video.load_metadata(parent["id"]))["auto_postprocess_error"]
    tasks.claim_retry(child_id)
    assert video.public_task(video.load_metadata(parent["id"]))["auto_postprocess_error"] is None


def test_preparation_failure_preserves_h3_and_does_not_retry_on_every_restart(starts, monkeypatch):
    parent = parent_record(); attempts = []
    def fail(*args):
        attempts.append(True)
        raise OSError("fixture copy failure")
    monkeypatch.setattr(auto.shutil, "copy2", fail)
    assert asyncio.run(auto.ensure_auto_postprocess(parent["id"])) is None
    asyncio.run(auto.resume_completed_tasks())
    asyncio.run(auto.ensure_auto_postprocess(parent["id"]))
    saved = video.load_metadata(parent["id"])
    assert saved["status"] == "completed" and saved["error"] is None
    assert "fixture copy failure" in saved["auto_postprocess_error"]
    assert len(attempts) == 1 and starts == []


def test_reference_fallback_and_asset_root_validation(starts, tmp_path):
    parent = parent_record()
    outside = tmp_path / "unowned.png"; Image.new("RGB", (8, 8), "green").save(outside)
    good_reference = parent["_assets"]["first_frame"]
    parent["_assets"].update(first_frame=str(outside), reference_image_1=good_reference)
    video._save_metadata(parent)
    child_id = asyncio.run(auto.ensure_auto_postprocess(parent["id"]))
    assert Path(tasks.load(child_id)["_reference"]).read_bytes() == Path(good_reference).read_bytes()
    no_reference = parent_record()
    no_reference["_assets"].pop("first_frame"); video._save_metadata(no_reference)
    child2 = asyncio.run(auto.ensure_auto_postprocess(no_reference["id"]))
    assert tasks.load(child2)["_reference"] is None


def test_child_owner_mismatch_does_not_mutate_foreign_record(starts):
    parent = parent_record(); child_id = auto.child_task_id(parent["id"])
    other = {"id": child_id, "kind": "postprocess", "status": "completed", "_ready": True,
             "_owner": "other-owner", "auto_postprocess_parent_id": "other-parent"}
    tasks.save(other)
    assert asyncio.run(auto.ensure_auto_postprocess(parent["id"])) is None
    assert tasks.load(child_id) == other
    assert video.load_metadata(parent["id"])["status"] == "completed" and starts == []


@pytest.mark.parametrize("handoff_failure", [False, True])
def test_real_completion_hook_only_starts_finishing_after_h3_saved(starts, monkeypatch, handoff_failure):
    parent = parent_record(); original = Path(parent["_assets"]["video"])
    parent.update(status="running", _assets={}); video._save_metadata(parent)
    class Client:
        def download(self, descriptor, destination):
            destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(original, destination)
    monkeypatch.setattr(video, "_find_video_descriptor", lambda _: {"filename": "returned.mp4"})
    if handoff_failure:
        monkeypatch.setattr(auto, "_prepare", lambda _: (_ for _ in ()).throw(OSError("handoff fixture failure")))
    asyncio.run(video._complete_from_history(parent["id"], {"status": {"completed": True}}, Client()))
    saved = video.load_metadata(parent["id"])
    assert saved["status"] == "completed" and saved["error"] is None
    if handoff_failure:
        assert starts == [] and "handoff fixture failure" in saved["auto_postprocess_error"]
    else:
        assert starts == [saved["auto_postprocess_task_id"]]


def test_start_failure_keeps_h3_completed_and_child_ready_for_manual_retry(monkeypatch):
    parent = parent_record()
    monkeypatch.setattr(tasks, "start", lambda _: (_ for _ in ()).throw(RuntimeError("start fixture failure")))
    assert asyncio.run(auto.ensure_auto_postprocess(parent["id"])) is None
    saved = video.load_metadata(parent["id"]); child = tasks.load(saved["auto_postprocess_task_id"])
    assert saved["status"] == "completed" and "start fixture failure" in saved["auto_postprocess_error"]
    assert child["_ready"] and child["status"] == "failed"
    assert tasks.claim_retry(child["id"])["status"] == "queued"


def test_worker_failure_callback_updates_chain_error_without_failing_h3(monkeypatch):
    parent = parent_record()
    monkeypatch.setattr(tasks, "_run", lambda child_id: tasks.update(child_id, status="failed", error="worker fixture failure"))
    async def run():
        child_id = await auto.ensure_auto_postprocess(parent["id"])
        await tasks.JOBS[child_id]
        await asyncio.sleep(0)
        return child_id
    child_id = asyncio.run(run())
    saved = video.load_metadata(parent["id"])
    assert saved["status"] == "completed" and saved["error"] is None
    assert saved["auto_postprocess_error"] == "worker fixture failure"
    assert saved["auto_postprocess_task_id"] == child_id


def test_startup_handoff_recovery_runs_after_both_normal_recoveries(monkeypatch):
    import main
    order = []
    async def video_recovery():order.append("video")
    async def studio_recovery():order.append("studio")
    async def auto_recovery():order.append("auto")
    async def no_op():pass
    async def forever():await asyncio.Event().wait()
    monkeypatch.setattr(main, "ensure_directories", lambda: None)
    monkeypatch.setattr(main, "resume_incomplete_tasks", video_recovery)
    monkeypatch.setattr(tasks, "resume", studio_recovery)
    monkeypatch.setattr(auto, "resume_completed_tasks", auto_recovery)
    monkeypatch.setattr(main, "weekly_history_cleanup", forever)
    monkeypatch.setattr(main, "daily_storage_check", forever)
    monkeypatch.setattr(main, "shutdown_background_tasks", no_op)
    monkeypatch.setattr(tasks, "shutdown", no_op)
    async def run():
        async with main.lifespan(None):
            assert order == ["video", "studio", "auto"]
    asyncio.run(run())
