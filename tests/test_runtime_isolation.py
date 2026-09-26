import json
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

import config
import main
from services import access_control, storage_maintenance, studio_tasks, video_generation


def test_lifespan_recovers_only_the_isolated_studio_record(isolated_runtime, tmp_path):
    # A neighboring record stands in for production storage. The real lifespan
    # must only scan its configured task root, including repeated TestClients.
    outside = tmp_path / "unrelated_live_job" / "metadata.json"
    outside.parent.mkdir()
    outside.write_text(json.dumps({"id": "external", "status": "running"}), encoding="utf-8")
    original = outside.read_bytes()
    task = studio_tasks.create("postprocess", "test-user", prompt="A moonlit castle")
    studio_tasks.update(task["id"], status="running")
    for _ in range(2):
        with TestClient(main.app) as client:
            assert client.get("/api/health").status_code == 200
            assert studio_tasks.load(task["id"])["status"] == "failed"
        assert outside.read_bytes() == original
    assert studio_tasks.JOBS == {}
    assert video_generation._background_tasks == set()


def test_all_persistence_and_cleanup_roots_are_isolated(isolated_runtime):
    for path in (config.VIDEO_DATA_FOLDER, config.VIDEO_WORKFLOW_FOLDER,
                 config.ACCESS_DATA_FOLDER, config.COMFYUI_INPUT_DIR,
                 video_generation.VIDEO_DATA_FOLDER, video_generation.VIDEO_WORKFLOW_FOLDER,
                 video_generation.COMFYUI_INPUT_DIR, storage_maintenance.VIDEO_DATA_FOLDER,
                 access_control.KEYS_PATH, access_control.USERS_PATH, access_control.STATS_PATH,
                 studio_tasks.ROOT):
        assert Path(path).resolve().is_relative_to(isolated_runtime.resolve())
    config.ensure_directories()
    assert storage_maintenance.cleanup_expired_history() == {"video_tasks": 0}
    assert storage_maintenance.emergency_cleanup_if_needed(threshold_percent=0)["deleted"] == 0


def test_unstubbed_network_is_rejected_before_reaching_comfy():
    with pytest.raises(pytest.fail.Exception, match="External network/restart"):
        requests.post("http://127.0.0.1:8188/prompt", json={"prompt": {}})
