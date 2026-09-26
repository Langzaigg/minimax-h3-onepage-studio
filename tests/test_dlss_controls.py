import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from io import BytesIO
import pytest

from routers import studio
from services import dlss_filter as df, post_processing as pp, studio_tasks as tasks

NODE_PATH = Path(__file__).resolve().parents[1]/"integrations/comfyui/ComfyUI-DLSS5/studio_filter.py"
spec = importlib.util.spec_from_file_location("studio_filter_under_test", NODE_PATH)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


@pytest.mark.parametrize("style,number", [("default", 0), ("natural", 1), ("cinematic", 2)])
def test_four_distinct_parameters_reach_native_bindings_and_style_selection(style, number):
    settings = df.filter_options(controls_version=2, style=style)
    graph = df.build_filter_workflow("input.png", media_kind="image", options=settings)
    assert graph["4"]["class_type"] == "StudioDLSS5Filter"
    values = dict(graph["4"]["inputs"]); values.pop("image")
    actual = native.native_settings(**values)
    assert actual == df.native_parameters(settings)
    assert actual == {"style": number, "intensity": 1, "style_strength": .4,
        "local_structure": .5, "skin_structure": .3, "auto_mask": True, "depth_inverted": True,
        "feature_id": 18, "preset": 0}
    assert "style_strength" not in df.CONTROLS  # It is local tone, not a fifth independent effect.
    assert not any(node["class_type"] in {"ImageScale", "RIFE VFI", "DLSSSuperResolution"} for node in graph.values())


def test_unchecked_restores_explicit_public_wrapper_defaults_and_keeps_saved_values():
    controls = {name + "_enabled": False for name in df.CONTROLS}
    controls.update(local_tone=.2, output_blend=.8, local_structure=.7, skin_mask=.1)
    settings = df.filter_options(controls_version=2, **controls)
    assert settings["local_tone"] == .2
    actual = df.native_parameters(settings)
    assert actual == native.native_settings(**{k: v for k, v in settings.items() if k != "controls_version"})
    assert [actual[k] for k in ("intensity", "style_strength", "local_structure", "skin_structure")] == [1, 1, 1, -1]


@pytest.mark.parametrize("field", list(df.CONTROLS))
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01, 1.01, 5])
def test_standard_range_fails_in_both_api_normalization_and_node(field, bad):
    with pytest.raises(ValueError):
        df.filter_options(controls_version=2, **{field: bad})
    with pytest.raises(ValueError):
        native.native_settings(**{field: bad})


def test_experimental_range_is_real_and_only_opt_in():
    settings = df.filter_options(controls_version=2, allow_experimental=True,
                                 **{name: 5 for name in df.CONTROLS})
    actual = native.native_settings(**{k: v for k, v in settings.items() if k != "controls_version"})
    assert all(actual[key] == 5 for key in ("intensity", "style_strength", "local_structure", "skin_structure"))
    with pytest.raises(ValueError):
        df.filter_options(controls_version=2, allow_experimental=True, local_tone=5.01)
    with pytest.raises(ValueError):
        df.filter_options(controls_version=2, skin_mask_enabled="false")


@pytest.mark.parametrize("frames", [1, 9])
def test_adapter_preserves_easy_depth_motion_and_temporal_guides_and_releases_caches(monkeypatch, tmp_path, frames):
    events = []
    class Tensor:
        shape = (frames, 96, 128, 3)
        device = "cpu"
        def detach(self): return self
        def to(self, *a, **k): return self
        def numpy(self): return "source-pixels"
    tensor = Tensor()
    monkeypatch.setitem(sys.modules, "comfy", SimpleNamespace(model_management=SimpleNamespace(
        unload_all_models=lambda: events.append("unload"), soft_empty_cache=lambda: events.append("empty"))))
    def guide(name, *expected):
        def invoke(*args):
            events.append(name)
            if expected: assert args[1:] == expected
            return (name,)
        return invoke
    def run_process(command, **kwargs):
        assert Path(command[1]).name == "bridge_runner.py"
        settings = json.loads(command[command.index("--settings") + 1])
        assert settings["style_strength"] == .9 and settings["intensity"] == 0
        assert "--depth" in command and "--mvec" in command
        events.append("native")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    implementation = SimpleNamespace(
        _bridge_diagnostics=lambda _: (True, ""),
        _easy_preset=lambda scenario, _: {"flow_model": "Optical Flow (fastest)" if frames == 1 else "RAFT Large (best)", "flow_chunk": 2},
        DLSS5DepthAnythingV2=lambda: SimpleNamespace(estimate=guide("depth", "Small (recommended)", True, 4)),
        DLSS5OpticalFlow=lambda: SimpleNamespace(estimate=guide("optical", .5, 5, 21)),
        DLSS5RAFTFlow=lambda: SimpleNamespace(estimate=guide("raft", "RAFT Large (best)", 2)),
        DLSS5TemporalDepthStabilize=lambda: SimpleNamespace(stabilize=guide("temporal", "raft", .7, .08)),
        _runtime_paths=lambda: ("python", "plugin", "dll"), _runtime_temp_dir=lambda: tmp_path,
        torch=SimpleNamespace(float32="float32", from_numpy=lambda _: tensor),
        np=SimpleNamespace(save=lambda *a: None, load=lambda *a: tensor),
        _save_guides_chunked=lambda *a: events.append("guide-files"), _run_process=run_process,
        _runtime_timeout=lambda: 900, PACKAGE=tmp_path, _DEPTH_CACHE={"model": 1}, _RAFT_CACHE={"model": 1})
    output = native.make_filter_node(implementation)().run(tensor, local_tone=.9, output_blend=0)
    assert output["result"][0] is tensor
    assert json.loads(output["ui"]["text"][0])["style_strength"] == .9
    assert events == (["unload", "empty", "depth", "optical", "guide-files", "native", "empty"] if frames == 1 else
                      ["unload", "empty", "depth", "raft", "temporal", "guide-files", "native", "empty"])
    assert not implementation._DEPTH_CACHE and not implementation._RAFT_CACHE


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "ROOT", tmp_path/"jobs")
    monkeypatch.setattr(tasks, "start", lambda _: None)
    monkeypatch.setattr(studio, "_capabilities", lambda: {"postprocess": {"presets": [
        {"id": "dlss_nr", "available": True, "controls_contract_version": 2}]}})
    app = FastAPI(); app.include_router(studio.router)
    app.dependency_overrides[studio.require_video_access] = lambda: {"role": "user", "key_id": "owner"}
    return TestClient(app)


def image_file():
    file = BytesIO(); Image.new("RGB", (32, 32), "red").save(file, format="PNG")
    return {"image": ("input.png", file.getvalue(), "image/png")}


def test_api_stores_controls_for_history_and_blocks_unloaded_contract(client, monkeypatch):
    fields = {"preset": "dlss_nr", "dlss_controls_version": "2", "dlss_style": "cinematic",
              "dlss_local_tone": ".8", "dlss_skin_mask_enabled": "false", "dlss_allow_experimental": "true",
              "dlss_output_blend": "5"}
    response = client.post("/api/studio/postprocess", data=fields, files=image_file())
    assert response.status_code == 202, response.text
    public = response.json(); saved = tasks.load(public["id"])
    assert public["dlss_controls_version"] == 2 and public["dlss_local_tone"] == .8
    assert saved["filter_options"]["output_blend"] == 5 and not saved["filter_options"]["skin_mask_enabled"]
    monkeypatch.setattr(studio, "_capabilities", lambda: {"postprocess": {"presets": [
        {"id": "dlss_nr", "available": True, "controls_contract_version": 1}]}})
    assert client.post("/api/studio/postprocess", data=fields, files=image_file()).status_code == 409
    legacy = client.post("/api/studio/postprocess", data={"preset": "dlss_nr", "dlss_strength": ".25"}, files=image_file())
    assert legacy.status_code == 202 and legacy.json()["filter_options"] == {"look": "neutral", "strength": .25}


@pytest.mark.parametrize("fields", [{"dlss_local_tone": "NaN"}, {"dlss_output_blend": "1.1"},
    {"dlss_skin_mask": "5.1", "dlss_allow_experimental": "true"}, {"dlss_style": "unrecognized"},
    {"dlss_controls_version": "3"}])
def test_api_rejects_invalid_controls_before_queue(client, fields):
    response = client.post("/api/studio/postprocess", data={"preset": "dlss_nr", "dlss_controls_version": "2", **fields}, files=image_file())
    assert response.status_code in {400, 422}
    assert not list(tasks.ROOT.glob("*/metadata.json"))


def test_comfy_receipt_contains_executed_parameters_not_only_requested_controls(tmp_path):
    parameters = df.native_parameters(df.filter_options(controls_version=2, local_tone=.7))
    class Response:
        ok = True
        def __init__(self, value): self.value = value
        def json(self): return self.value
        def raise_for_status(self): pass
        def iter_content(self, *a): return [b"rendered PNG fixture"]
    class Session:
        def post(self, *a, **k): return Response({"prompt_id": "validated-node-run"})
        def get(self, url, **kwargs):
            if "/history/" in url:
                return Response({"validated-node-run": {"status": {"completed": True}, "outputs": {
                    "4": {"text": [json.dumps(parameters)]},
                    "90": {"images": [{"filename": "result.png", "type": "output"}]}}}})
            return Response({})
    client = pp.PostprocessComfyClient(); client.session = Session()
    graph = df.build_filter_workflow("source.png", media_kind="image", options=df.filter_options(controls_version=2))
    result = client.render(graph, tmp_path/"result.png")
    assert result["native_parameters"] == parameters
    assert result["native_parameters"]["style_strength"] == .7  # actual report differs from request .4


def test_worker_rejects_a_node_that_reports_different_native_controls(tmp_path, monkeypatch):
    source, output = tmp_path/"source.png", tmp_path/"finished.png"
    Image.new("RGB", (32, 32), "red").save(source)
    class Client:
        base_url = "http://test.invalid"
        session = SimpleNamespace(trust_env=True, get=lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {n["class_type"]: {} for n in
                df.build_filter_workflow("source.png", media_kind="image", options=df.filter_options(controls_version=2)).values()}))
        def __init__(self, *a, **k): pass
        def upload(self, path): return Path(path).name
        def render(self, graph, destination, **kwargs):
            Image.new("RGB", (32, 32), "red").save(destination)
            return {"native_parameters": {"style": 999}}
    monkeypatch.setattr(pp, "PostprocessComfyClient", Client)
    monkeypatch.setattr(pp, "_dlss_runtime_status", lambda _: ([], False))
    with pytest.raises(RuntimeError, match="原生参数"):
        df.run_filter(source, output, media_kind="image", options=df.filter_options(controls_version=2))
