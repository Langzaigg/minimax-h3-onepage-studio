"""Keep application lifespans and workers inside disposable test storage.

The environment fence is installed before test collection imports ``main``.
Per-test overrides then restore to that safe fence, never to a live job folder.
Tests still run the actual startup recovery, authentication and cleanup code.
"""

import importlib
import shutil
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_session_environment = None
_session_storage = None


def pytest_configure(config):
    global _session_environment, _session_storage
    _session_storage = tempfile.TemporaryDirectory(prefix="h3-studio-pytest-")
    root = Path(_session_storage.name)
    _session_environment = pytest.MonkeyPatch()
    # Set these before config.py or any from-config aliases are imported.
    for name, value in {
        "VIDEO_DATA_FOLDER": str(root / "video_data"),
        "VIDEO_WORKFLOW_FOLDER": str(root / "video_workflows"),
        "ACCESS_DATA_FOLDER": str(root / "access_data"),
        "COMFYUI_INPUT_DIR": str(root / "ComfyUI" / "input"),
        "COMFYUI_BASE_URL": "http://127.0.0.1:1",
        "SERVICE_ADMIN_KEY": "test-admin-secret",
        "LLM_API_KEY": "test-provider-key",
        "LLM_BASE_URL": "http://127.0.0.1:1/v1",
    }.items():
        _session_environment.setenv(name, value)
    shutil.copytree(PROJECT_ROOT / "video_workflows", root / "video_workflows")


def pytest_unconfigure(config):
    if _session_environment is not None:
        _session_environment.undo()
    if _session_storage is not None:
        _session_storage.cleanup()


def _external_effect_disabled(*args, **kwargs):
    pytest.fail("External network/restart effects are disabled in unit tests; stub the boundary explicitly", pytrace=False)


async def _async_external_effect_disabled(*args, **kwargs):
    _external_effect_disabled()


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """A fresh filesystem and job registry; individual tests may narrow either."""
    import config
    import main
    import requests
    import websockets
    from routers import admin, studio
    from services import access_control, image_generation, post_processing
    from services import service_update, storage_maintenance, studio_tasks, video_generation, workspaces

    root = tmp_path / "isolated_runtime"
    video_root = root / "video_data"
    workflow_root = root / "video_workflows"
    access_root = root / "access_data"
    comfy_root = root / "ComfyUI"
    shutil.copytree(PROJECT_ROOT / "video_workflows", workflow_root)
    for folder in (video_root, access_root, comfy_root / "input", comfy_root / "models"):
        folder.mkdir(parents=True, exist_ok=True)

    # Keep the optional integration's Python-only cleanup test meaningful while
    # withholding the real runtime, DLLs, model files and runtime configuration.
    integration = PROJECT_ROOT / "integrations" / "comfyui" / "ComfyUI-DLSS5" / "studio_integration.py"
    if integration.is_file():
        copy = comfy_root / "custom_nodes" / "ComfyUI-DLSS5" / integration.name
        copy.parent.mkdir(parents=True)
        shutil.copyfile(integration, copy)

    values = {
        "VIDEO_DATA_FOLDER": str(video_root),
        "VIDEO_WORKFLOW_FOLDER": str(workflow_root),
        "ACCESS_DATA_FOLDER": str(access_root),
        "COMFYUI_INPUT_DIR": str(comfy_root / "input"),
    }
    modules = (config, main, access_control, image_generation, post_processing,
               storage_maintenance, studio_tasks, video_generation, studio, admin, workspaces)
    for module in modules:
        for name, value in values.items():
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
    for name, filename in (("KEYS_PATH", "api_keys.json"), ("USERS_PATH", "users.json"),
                           ("STATS_PATH", "usage_stats.json")):
        monkeypatch.setattr(access_control, name, access_root / filename)
    monkeypatch.setattr(studio_tasks, "ROOT", video_root / "studio")
    monkeypatch.setattr(studio_tasks, "JOBS", {})
    monkeypatch.setattr(workspaces, "ROOT", video_root / "workspaces")
    monkeypatch.setattr(video_generation, "_background_tasks", set())
    monkeypatch.setattr(post_processing, "ROOT", root)
    monkeypatch.setattr(post_processing, "COMFY_ROOT", comfy_root)
    monkeypatch.setattr(post_processing, "MODEL_ROOT", comfy_root / "models")
    monkeypatch.setattr(studio, "_capability_cache", (0, None))
    monkeypatch.setattr(main.app, "dependency_overrides", {})

    # In-process TestClient HTTP still works. Fake clients/transports in a test
    # override these guards normally; real Comfy and provider calls cannot escape.
    monkeypatch.setattr(requests.sessions.Session, "request", _external_effect_disabled)
    for name in ("httpx", "httpx2"):
        try:
            transport = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        monkeypatch.setattr(transport.HTTPTransport, "handle_request", _external_effect_disabled)
        monkeypatch.setattr(transport.AsyncHTTPTransport, "handle_async_request", _async_external_effect_disabled)
    monkeypatch.setattr(websockets, "connect", _external_effect_disabled)
    monkeypatch.setattr(image_generation, "connect", _external_effect_disabled)
    monkeypatch.setattr(service_update, "run_git", _external_effect_disabled)
    monkeypatch.setattr(service_update, "schedule_service_restart", _external_effect_disabled)
    monkeypatch.setattr(service_update, "terminate_current_process", _external_effect_disabled)
    monkeypatch.setattr(admin, "schedule_service_restart", _external_effect_disabled)
    yield root
