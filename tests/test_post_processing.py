from pathlib import Path
import json
import importlib.util
import inspect
import shutil
import sys
from types import SimpleNamespace

import pytest

from services import post_processing as pp


def test_ltx_graph_reencodes_and_resamples_source_instead_of_resizing_only():
    graph = pp.build_workflow("clip.mp4", "Clean animation", "ltx23_refine")
    assert graph["20"]["class_type"] == "VAEEncode"
    assert graph["29"]["inputs"]["latent_image"] == ["24", 0]
    assert graph["24"]["inputs"]["video_latent"] == ["20", 0]
    assert graph["30"]["inputs"]["samples"] == ["54", 0]
    assert graph["30"]["inputs"]["temporal_size"] == 4096
    assert graph["90"]["inputs"]["audio"] == ["2", 1]
    assert graph["28"]["inputs"]["sigmas"].startswith("0.4,")
    assert graph["90"]["inputs"]["fps"] == 24


def test_h3_reference_and_models_are_independent_of_secret_config():
    graph = pp.build_workflow("source.mp4", "<Picture 1> remains identical", "h3_refine",
                              reference_image="avatar.png", model_overrides={"h3_model": "custom.safetensors"}, frames=107)
    assert graph["10"]["inputs"]["unet_name"] == "custom.safetensors"
    assert graph["16"]["inputs"]["ref_images.ref_image_1"] == ["15", 0]
    assert graph["16"]["inputs"]["length"] == 107
    assert graph["23"]["inputs"]["mask"] == ["22", 0]


def test_pixel_iclora_uses_low_resolution_guide_and_removes_it_before_decoding():
    graph = pp.build_workflow("low_res.mp4", "clean animation", "ltx25_pixel_2x", frames=25,
                              model_overrides={"ltx25_video_vae": "ltx-2.5-video-vae-bf16.safetensors"})
    assert all(node["class_type"] != "RTXVideoSuperResolution" for node in graph.values())
    assert (graph["3"]["inputs"]["width"], graph["3"]["inputs"]["height"]) == (960, 544)
    assert graph["21"]["inputs"]["iclora_parameters"] == ["20", 0]
    assert graph["25"]["inputs"]["video_latent"] == ["21", 2]
    assert graph["32"]["class_type"] == "LTXVCropGuides"
    assert graph["33"]["inputs"]["samples"] == ["32", 2]
    assert graph["33"]["inputs"]["temporal_size"] == 4096
    assert graph["13"]["inputs"]["vae_name"] == "ltx-2.5-video-vae-bf16.safetensors"
    assert graph["19"]["class_type"] == "EmptyLTXVLatentVideo"
    assert graph["29"]["inputs"]["sigmas"] == "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
    assert graph["90"]["inputs"]["audio"] == ["2", 1]
    with pytest.raises(ValueError, match="multiples of 64"):
        pp.build_workflow("low_res.mp4", preset="ltx25_pixel_2x", refine_height=1056)


def test_pixel_two_stage_refines_upscaled_latents_without_reapplying_iclora():
    graph = pp.build_workflow("low_res.mp4", preset="ltx25_pixel_2stage", frames=25)
    assert (graph["19"]["inputs"]["width"], graph["19"]["inputs"]["height"]) == (1024, 576)
    assert (graph["3"]["inputs"]["width"], graph["3"]["inputs"]["height"]) == (512, 288)
    assert graph["36"]["inputs"]["samples"] == ["32", 2]
    assert graph["38"]["inputs"]["model"] == ["10", 0]
    assert graph["38"]["inputs"]["positive"] == ["32", 0]
    assert graph["33"]["inputs"]["samples"] == ["42", 0]
    assert graph["39"]["inputs"]["sigmas"] == "0.909375, 0.725, 0.421875, 0.0"
    assert graph["41"]["inputs"]["latent_image"] == ["37", 0]
    assert graph["37"]["inputs"]["video_latent"] == ["36", 0]
    assert any(folder == "latent_upscale_models" for folder, _ in pp.required_models("ltx25_pixel_2stage"))


@pytest.mark.parametrize("target,source,canvas", [
    ("720p", (736, 414), (1472, 832)),
    ("1080p", (1280, 720), (2560, 1472)),
    ("1440p", (1344, 756), (2688, 1536)),
])
def test_msr_preserves_source_latent_and_reference_across_target_sizes(target, source, canvas):
    graph = pp.build_workflow("source.mp4", preset="ltx25_msr", reference_image="character.png",
                              frames=193, source_width=source[0], source_height=source[1], target_resolution=target)
    assert graph["4"] == {"class_type": "LoadImage", "inputs": {"image": "character.png"}}
    assert graph["3"] == {"class_type": "ImagePadKJ", "inputs": {
        "image": ["2", 0], "left": 0, "top": 0,
        "right": canvas[0]//2 - source[0], "bottom": canvas[1]//2 - source[1],
        "extra_padding": 0, "pad_mode": "edge_pixel", "color": "0, 0, 0"}}
    assert graph["20"]["class_type"] == "VAEEncode"
    assert graph["20"]["inputs"]["pixels"] == ["3", 0]
    assert graph["22"]["inputs"]["samples"] == ["20", 0]
    assert graph["23"]["inputs"]["latent"] == ["22", 0]
    assert graph["23"]["inputs"]["pic1"] == ["4", 0]
    assert graph["27"]["inputs"]["video_latent"] == ["23", 2]
    assert graph["32"]["inputs"]["latent_image"] == ["27", 0]
    assert graph["31"]["inputs"]["sigmas"] == "0.45, 0.25, 0.10, 0.0"
    assert graph["30"]["inputs"]["sampler_name"] == "euler"
    assert graph["34"]["inputs"]["positive"] == ["23", 0]
    assert graph["35"]["inputs"]["samples"] == ["34", 2]
    assert graph["35"]["inputs"]["temporal_size"] == 4096
    assert graph["37"] == {"class_type": "ImageCrop", "inputs": {
        "image": ["35", 0], "x": 0, "y": 0, "width": source[0]*2, "height": source[1]*2}}
    assert graph["36"]["inputs"]["image"] == ["37", 0]
    assert (graph["36"]["inputs"]["width"], graph["36"]["inputs"]["height"]) == pp.output_size(target)
    assert graph["90"]["inputs"]["audio"] == ["2", 1]
    assert graph["90"]["inputs"]["fps"] == ["2", 2]
    assert not any(n["class_type"] in ("EmptyLTXVLatentVideo", "LTXVImgToVideoInplace", "RTXVideoSuperResolution") for n in graph.values())
    redgraft = pp.build_workflow("source.mp4", preset="redgraft_msr", reference_image="character.png",
                                 frames=193, source_width=source[0], source_height=source[1], target_resolution=target)
    assert redgraft["10"]["inputs"]["unet_name"] == pp.MODEL_DEFAULTS["redgraft_model"]
    redgraft["10"] = graph["10"]
    assert redgraft == graph  # A/B varies only the base model, not the workflow.


@pytest.mark.parametrize("source,encode", [
    ((864, 480), (864, 480)), ((960, 540), (960, 544)),
    ((1888, 1062), (1888, 1088)), ((1920, 1080), (1920, 1088)),
    ((958, 538), (960, 544)),
])
def test_msr_native_geometry_is_independent_of_delivery_and_never_uses_direct_v2v(source, encode):
    graphs = []
    for target in ("720p", "1080p", "1440p"):
        plan = pp.msr_geometry(*source, target)
        assert plan["mode"] == "native_latent_2x"
        assert plan["source"] == list(source) and plan["encode"] == list(encode)
        assert plan["latent2x_canvas"] == [v*2 for v in encode]
        assert plan["content2x"] == [v*2 for v in source]
        assert plan["final"] == list(pp.output_size(target))
        assert plan["padding"]["left"] == plan["padding"]["top"] == 0
        graph = pp.build_workflow("source.mp4", preset="redgraft_msr", source_width=source[0],
                                  source_height=source[1], target_resolution=target)
        assert graph["22"]["class_type"] == "LTXVLatentUpsampler"
        assert [n["class_type"] for n in graph.values()].count("ImageScale") == 1  # Final delivery only.
        graph.pop("36")
        graphs.append(graph)
    assert graphs[0] == graphs[1] == graphs[2]


def test_msr_explicit_canvas_can_only_confirm_native_dimensions():
    options = dict(source_width=1280, source_height=720, preset="redgraft_msr")
    assert pp.build_workflow("source.mp4", **options) == pp.build_workflow(
        "source.mp4", refine_width=2560, refine_height=1472, **options)
    with pytest.raises(ValueError, match="fixed native latent 2x"):
        pp.build_workflow("source.mp4", refine_width=1920, refine_height=1088, **options)
    with pytest.raises(ValueError, match="positive integers"):
        pp.msr_geometry(0, 540)


def test_msr_first_frame_fallback_and_dependency_reporting(tmp_path):
    graph = pp.build_workflow("source.mp4", preset="ltx25_msr")
    assert graph["4"] == {"class_type": "ImageFromBatch", "inputs": {"image": ["2", 0], "batch_index": 0, "length": 1}}
    requirements = pp.required_models("ltx25_msr")
    assert ("loras", "LTX-2.5-Licon-MSR-V1.safetensors") in requirements
    assert ("latent_upscale_models", pp.MODEL_DEFAULTS["ltx25_latent_upscaler"]) in requirements
    assert ("frame_interpolation", pp.MODEL_DEFAULTS["rife_model"]) in requirements
    assert not any("pixel-spatial" in name for _, name in requirements)
    redgraft_models = pp.required_models("redgraft_msr")
    assert ("diffusion_models", pp.MODEL_DEFAULTS["redgraft_model"]) in redgraft_models
    assert ("diffusion_models", pp.MODEL_DEFAULTS["ltx25_model"]) not in redgraft_models
    assert ("loras", "LTX-2.5-Licon-MSR-V1.safetensors") in redgraft_models
    cap = next(c for c in pp.capabilities({}, model_root=tmp_path)["presets"] if c["id"] == "ltx25_msr")
    assert not cap["available"]
    assert "ComfyUILTX25MSRMultiReferenceGuide" in cap["missing_nodes"]
    assert "ImagePadKJ" in cap["missing_nodes"]
    assert "loras/LTX-2.5-Licon-MSR-V1.safetensors" in cap["missing_models"]
    assert pp.DEFAULT_PRESET == "redgraft_msr"
    with pytest.raises(ValueError, match="fixed native latent 2x"):
        pp.build_workflow("source.mp4", preset="ltx25_msr", refine_height=1056)


@pytest.mark.parametrize("preset", ["ltx25_msr", "redgraft_msr"])
@pytest.mark.parametrize("interpolation,expected_fps", [("none", 24), ("2x", 48)])
def test_msr_worker_uploads_reference_and_keeps_full_context(tmp_path, monkeypatch, preset, interpolation, expected_fps):
    source, reference, output = (tmp_path/name for name in ("source.mp4", "character.png", "result.mp4"))
    source.write_bytes(b"source video fixture")
    reference.write_bytes(b"reference fixture")
    info = dict(width=960, height=540, frames=48, fps=24.0, duration=2.0, has_audio=True)
    render_info = dict(width=1920, height=1080, frames=49, fps=24.0, duration=49/24, has_audio=False)
    final_info = dict(width=1920, height=1080, frames=2*expected_fps, fps=float(expected_fps), duration=2.0, has_audio=True)
    graphs, uploads = [], []
    class Client:
        base_url = "http://test.invalid"
        session = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}),
                                  post=lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None))
        def __init__(self, *a): pass
        def upload(self, path):
            uploads.append(Path(path))
            return "uploaded_"+Path(path).name
        def render(self, graph, destination, **kwargs):
            graphs.append(graph)
            destination.write_bytes(b"render fixture")
            return {"prompt_id": "fake-only-in-test"}
    def prepare(original, destination, **kwargs):
        assert original == source and kwargs["duration"] == 2.0
        destination.write_bytes(b"padded video fixture")
        return {"path": str(destination)}
    monkeypatch.setattr(pp, "PostprocessComfyClient", Client)
    monkeypatch.setattr(pp, "capabilities", lambda *a, **k: {"presets": [{"id": preset, "available": True}]})
    monkeypatch.setattr(pp, "prepare_input", prepare)
    monkeypatch.setattr(pp, "probe_video", lambda path: info if Path(path) == source else
                        render_info if Path(path).name == "whole_render.mp4" else final_info)
    monkeypatch.setattr(pp, "finalize_uniform_video", lambda *a, **k: {**final_info, "path": str(output)})
    assert pp.WORKFLOW_REVISIONS[preset] == 3
    monkeypatch.setitem(pp.WORKFLOW_REVISIONS, preset, 2)
    result = pp.run_postprocess(source, output, preset=preset, reference_image=reference, interpolation=interpolation)
    assert reference in uploads
    assert graphs[0]["4"]["inputs"]["image"] == "uploaded_character.png"
    assert graphs[0]["35"]["inputs"]["temporal_size"] == 4096
    assert graphs[0]["90"]["inputs"]["fps"] == 24
    assert len(graphs) == 2 and result["segments"] == 1 and result["frames"] == 2*expected_fps
    assert result["msr_geometry"] == pp.msr_geometry(960, 540)
    assert json.loads((output.parent/"result_stages"/"msr_geometry.json").read_text()) == result["msr_geometry"]
    # Native padding must not reuse receipts from the old pre-resized workflow.
    monkeypatch.setitem(pp.WORKFLOW_REVISIONS, preset, 3)
    pp.run_postprocess(source, output, preset=preset, reference_image=reference, interpolation=interpolation)
    assert len(graphs) == 4
    assert graphs[2]["31"]["inputs"]["sigmas"] == "0.45, 0.25, 0.10, 0.0"
    pp.run_postprocess(source, output, preset=preset, reference_image=reference, interpolation=interpolation)
    assert len(graphs) == 4  # Identical current settings can still resume safely.


def test_availability_never_claims_missing_models_or_unchecked_nodes_ready(tmp_path):
    nodes = {n["class_type"]: {} for n in pp.build_workflow("a.mp4").values()}
    nodes.update({n["class_type"]: {} for n in pp.build_workflow("a.mp4", preset="rtx_vsr").values()})
    caps = {c["id"]: c for c in pp.capabilities(nodes, model_root=tmp_path)["presets"]}
    assert not caps["rtx_vsr"]["available"]
    assert caps["rtx_vsr"]["missing_models"] == ["frame_interpolation/rife_v4.26.safetensors"]
    assert not caps["ltx23_refine"]["available"]
    assert len(caps["ltx23_refine"]["missing_models"]) == 3
    assert not caps["ltx25_refine"]["available"]
    assert all(not c["available"] for c in pp.capabilities(model_root=tmp_path)["presets"])


def test_segment_plan_covers_entire_source_without_duplicate_time():
    plans = pp.plan_segments(15, segment_seconds=4, overlap_seconds=.5)
    assert plans[0]["start"] == 0
    assert plans[-1]["start"] + plans[-1]["duration"] == 15
    assert sum(p["duration"] - p["overlap"] for p in plans) == 15
    for before, after in zip(plans, plans[1:]):
        assert before["start"] + before["duration"] - after["start"] == .5
    assert pp._grid_frames(96, "ltx") == 97
    assert pp._grid_frames(96, "h3") == 107


def test_unknown_backend_does_not_fall_back_silently():
    with pytest.raises(ValueError):
        pp.build_workflow("a.mp4", preset="dlss5_official")


def test_recommended_default_and_dependency_list_match_the_actual_graph():
    assert pp.DEFAULT_PRESET == "redgraft_msr"
    assert pp.capabilities()["default_preset"] == pp.DEFAULT_PRESET
    for function in (pp.build_workflow, pp.run_postprocess):
        assert inspect.signature(function).parameters["preset"].default == pp.DEFAULT_PRESET
    for preset in ("redgraft_refine", "redgraft_pixel_2stage"):
        graph = pp.build_workflow("sample.mp4", preset=preset)
        expected = pp.MODEL_DEFAULTS["redgraft_model"]
        assert graph["10"]["inputs"]["unet_name"] == expected
        assert ("diffusion_models", expected) in pp.required_models(preset)


def test_concurrent_clients_upload_isolated_names_with_original_extension(tmp_path):
    source = tmp_path / "input_000.mp4"
    source.write_bytes(b"test video")

    class Session:
        def post(self, url, **kwargs):
            name = kwargs["files"]["image"][0]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"name": name})

    clients = [pp.PostprocessComfyClient(), pp.PostprocessComfyClient()]
    for client in clients:
        client.session = Session()
    names = [client.upload(source) for client in clients]
    assert len(set(names)) == 2
    assert all(name.endswith("_input_000.mp4") for name in names)


def test_cyclic_rife_plan_spans_the_wrap_without_duplicate_output_frames():
    plan = pp.loop_frame_plan(source_frames=361, duration=15, fps=30)
    assert plan["anchors"] == [*range(0, 360, 2), 0]
    assert plan["multiplier"] == 5
    assert plan["selection"] == list(range(0, 900, 2))
    assert len(plan["selection"]) == 450
    # 898 and 899 lie in the last->first interpolation interval; the endpoint
    # itself (900) is excluded so playback never repeats the first frame twice.
    assert plan["selection"][-1] > 179 * plan["multiplier"]
    plan24 = pp.loop_frame_plan(source_frames=361, duration=15, fps=24)
    assert len(plan24["selection"]) == 360
    assert len(set(plan24["selection"])) == 360
    with pytest.raises(ValueError):
        pp.loop_frame_plan(source_frames=121, duration=15, fps=30)


def test_redgraft_loop_keeps_diffusion_one_full_low_resolution_pass_then_native_rife():
    graph = pp.build_workflow("full.mp4", preset="redgraft_loop", frames=361)
    assert graph["4"]["inputs"]["width"] == 960
    assert graph["4"]["inputs"]["height"] == 544
    assert graph["31"]["inputs"]["width"] == 960
    assert graph["28"]["inputs"]["sigmas"] == "0.25, 0.15, 0.05, 0.0"
    finish = pp.build_loop_finish_workflow("refined.mp4", source_frames=361, duration=15, fps=30)
    assert sum(n["class_type"] == "StudioFrameInterpolateSampled" for n in finish.values()) == 1
    assert sum(n["class_type"] == "RTXVideoSuperResolution" for n in finish.values()) == 1
    assert finish["6"]["inputs"]["images"] == ["9", 0]
    assert finish["9"]["inputs"]["images"] == ["4", 0]
    assert finish["9"]["inputs"]["indexes"].endswith(",179,0")
    assert ("frame_interpolation", "rife_v4.26.safetensors") in pp.required_models("redgraft_loop")


def test_dlss_sr_is_independent_of_missing_nr_and_requires_a_valid_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "COMFY_ROOT", tmp_path)
    runtime = tmp_path / "custom_nodes" / "ComfyUI-DLSS5" / "runtime"
    runtime.mkdir(parents=True)
    for name in ("vsdlsssr.dll", "nvngx_dlss.dll", "vsdlssnr.dll"):
        (runtime / name).write_bytes(b"test binary" * 150)
    (runtime / "nvngx_dlssnr.dll").write_text("version https://git-lfs.github.com/spec/v1")
    (runtime / "config.json").write_text(json.dumps({"python": str(runtime / "python.exe")}))
    nodes = {n["class_type"]: {} for n in pp.build_workflow("a.mp4", preset="dlss_sr").values()}
    nodes.update({n["class_type"]: {} for n in pp.build_timeline_workflow("a.mp4", source_frames=49, source_fps=24).values()})
    (tmp_path/"frame_interpolation").mkdir()
    (tmp_path/"frame_interpolation"/"rife_v4.26.safetensors").write_bytes(b"fake model"*200)
    caps = {c["id"]: c for c in pp.capabilities(nodes, model_root=tmp_path)["presets"]}
    assert not caps["dlss_sr"]["available"]
    assert "VapourSynth Python" in caps["dlss_sr"]["missing_runtime"]
    (runtime / "python.exe").write_bytes(b"test interpreter")
    caps = {c["id"]: c for c in pp.capabilities(nodes, model_root=tmp_path)["presets"]}
    assert caps["dlss_sr"]["available"]
    assert not caps["dlss_sr"]["inference_verified"]
    assert not caps["dlss_nr"]["available"]
    assert "nvngx_dlssnr.dll" in caps["dlss_nr"]["missing_runtime"]
    assert str(runtime) not in json.dumps(caps)
    graph = pp.build_workflow("a.mp4", preset="dlss_sr")
    assert graph["4"]["inputs"]["operation"] == "Upscale only"


def test_dlss_ada_description_tracks_actual_runtime_smoke_and_invalidates_changed_dll(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "COMFY_ROOT", tmp_path)
    runtime = tmp_path / "custom_nodes" / "ComfyUI-DLSS5" / "runtime"
    runtime.mkdir(parents=True)
    names = ("config.json", "nvngx_dlss.dll", "vsdlsssr.dll", "nvngx_dlssnr.dll", "vsdlssnr.dll")
    for name in names[1:]:
        (runtime / name).write_bytes(b"isolated test DLL" * 150)
    (runtime / "python.exe").write_bytes(b"interpreter")
    (runtime / "config.json").write_text(json.dumps({"python": str(runtime / "python.exe")}))
    nodes = {n["class_type"]: {} for n in pp.build_workflow("a.mp4", preset="dlss_nr").values()}
    nodes.update({"LoadImage": {}, "SaveImage": {}})
    nodes.update({n["class_type"]: {} for n in pp.build_timeline_workflow("a.mp4", source_frames=49, source_fps=24).values()})

    def nr_capability():
        return next(c for c in pp.capabilities(nodes, model_root=tmp_path)["presets"] if c["id"] == "dlss_nr")

    pending = nr_capability()
    assert not pending["inference_verified"]
    assert "尚未完成本机推理验证" in pending["description"]
    proof = {"dlss_nr": {"success": True, "runtime_files": {
        name: [(runtime / name).stat().st_size, (runtime / name).stat().st_mtime_ns] for name in names}}}
    (runtime / "verification.json").write_text(json.dumps(proof))
    verified = nr_capability()
    assert verified["inference_verified"]
    assert "社区 Ada" in verified["label"]
    assert "已通过本机短片推理" in verified["description"]
    (runtime / "nvngx_dlssnr.dll").write_bytes(b"changed binary" * 151)
    changed = nr_capability()
    assert not changed["inference_verified"]
    assert "尚未完成本机推理验证" in changed["description"]


def test_dlss_private_gpu_cache_is_released_even_when_native_inference_fails(monkeypatch):
    module_path = pp.COMFY_ROOT / "custom_nodes" / "ComfyUI-DLSS5" / "studio_integration.py"
    if not module_path.is_file():
        pytest.skip("Optional DLSS studio integration is not installed")
    spec = importlib.util.spec_from_file_location("studio_dlss_cleanup_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    manager = SimpleNamespace(unload_all_models=lambda: calls.append("unload"),
                              soft_empty_cache=lambda: calls.append("empty"))
    monkeypatch.setitem(sys.modules, "comfy", SimpleNamespace(model_management=manager))
    implementation = SimpleNamespace(_DEPTH_CACHE={"network": object()}, _RAFT_CACHE={"network": object()})

    class FailingPipeline:
        def run(self):
            calls.append("native")
            raise RuntimeError("native failed")

    wrapped = module.wrap_easy_pipeline(FailingPipeline, implementation)
    with pytest.raises(RuntimeError, match="native failed"):
        wrapped().run()
    assert calls == ["unload", "empty", "native", "empty"]
    assert not implementation._DEPTH_CACHE and not implementation._RAFT_CACHE


def test_queue_validation_errors_are_reported_without_polling(tmp_path):
    class BadResponse:
        ok = False
        text = '{"node_errors":{"4":{"errors":[{"message":"Required input is missing"}]}}}'

    class Session:
        def post(self, *args, **kwargs):
            return BadResponse()

        def get(self, *args, **kwargs):
            pytest.fail("Validation failure must not poll or silently downgrade")

    client = pp.PostprocessComfyClient()
    client.session = Session()
    with pytest.raises(RuntimeError, match="Required input is missing"):
        client.render({}, tmp_path / "unused.mp4")


def test_cached_loadvideo_ui_output_never_replaces_the_saved_render(tmp_path):
    saved = {"filename": "actual-render.mp4", "subfolder": "studio", "type": "output"}
    uploaded = {"filename": "original-input.mp4", "subfolder": "", "type": "input"}

    class Response:
        ok = True

        def __init__(self, data=None):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

        def iter_content(self, size):
            yield b"rendered-result"

    class Session:
        def post(self, *args, **kwargs):
            return Response({"prompt_id": "test-prompt"})

        def get(self, url, **kwargs):
            if "/history/" in url:
                # Cached input UI is appended AFTER the actual output node.
                return Response({"test-prompt": {"status": {"completed": True}, "outputs": {
                    "91": {"images": [saved]}, "1": {"images": [uploaded]}}}})
            assert kwargs["params"] == saved
            return Response()

    client = pp.PostprocessComfyClient()
    client.session = Session()
    output = tmp_path / "result.mp4"
    result = client.render({"1": {"class_type": "LoadVideo"}, "91": {"class_type": "SaveVideo"}}, output)
    assert result["asset"] == saved and output.read_bytes() == b"rendered-result"


def test_ffmpeg_prepare_stitch_and_loop_have_exact_dimensions_timing_audio(tmp_path):
    source = tmp_path / "source.mp4"
    pp._run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24", "-t", "1.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", source])
    plans = pp.plan_segments(1.5, segment_seconds=1, overlap_seconds=.25)
    paths = []
    for index, plan in enumerate(plans):
        path = tmp_path / f"part{index}.mp4"
        prepared = pp.prepare_input(source, path, start=plan["start"], duration=plan["duration"])
        assert prepared["frames"] % 8 == 1
        info = pp.probe_video(path)
        assert info["has_audio"]
        assert info["frames"] == prepared["frames"]
        assert info["fps"] == 24
        paths.append(path)
    assembled = tmp_path / "assembled.mp4"
    info = pp.stitch_segments(paths, plans, assembled)
    assert info["duration"] == pytest.approx(1.5, abs=1/24)
    result = pp.finalize_video(assembled, tmp_path / "loop.mp4", loop_seconds=1.5, fps=30, seam_seconds=.25)
    assert (result["width"], result["height"], result["frames"]) == (1920, 1080, 45)
    assert result["duration"] == pytest.approx(1.5, abs=1/30)
    assert result["has_audio"]


def test_completed_whole_passes_resume_but_changed_prompt_invalidates_receipt(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    pp._run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:size=160x90:rate=24", "-t", "0.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", source])
    renders = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class Session:
        def get(self, *args, **kwargs):
            return Response()

        def post(self, *args, **kwargs):
            return Response()

    class Client:
        base_url = "http://test.invalid"
        session = Session()

        def __init__(self, *args):
            pass

        def upload(self, path):
            return str(path)

        def render(self, graph, destination, **kwargs):
            renders.append(graph)
            output_fps = graph["90"]["inputs"]["fps"]
            pp._run_ffmpeg(["-f", "lavfi", "-i", f"color=c=blue:size=1920x1080:rate={output_fps}",
                "-t", "0.5", "-c:v", "libx264", destination])
            return {"path": str(destination), "prompt_id": "fake-only-in-test"}

    monkeypatch.setattr(pp, "PostprocessComfyClient", Client)
    monkeypatch.setattr(pp, "capabilities", lambda *args, **kwargs: {"presets": [{"id": "rtx_vsr", "available": True}]})
    target = tmp_path / "output.mp4"
    first = pp.run_postprocess(source, target, "rtx_vsr", "first prompt")
    pp.run_postprocess(source, target, "rtx_vsr", "first prompt", crossfade=.3)
    assert len(renders) == 2
    assert first["frames"] == 15
    pp.run_postprocess(source, target, "rtx_vsr", "changed prompt")
    assert len(renders) == 4


def test_loop_worker_resumes_whole_pass_after_finish_failure_without_chunking_or_dissolve(tmp_path, monkeypatch):
    source, output = tmp_path / "source.mp4", tmp_path / "output.mp4"
    source.write_bytes(b"test source")
    infos = {str(source): {"width": 864, "height": 480, "frames": 362, "fps": 24.0, "duration": 362/24}}
    renders, prepares, finishes = [], [], []
    failing = [True]

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class Session:
        def get(self, *args, **kwargs):
            return Response()

        def post(self, url, **kwargs):
            assert url.endswith("/free")
            return Response()

    class Client:
        base_url, session = "http://test.invalid", Session()

        def __init__(self, *args):
            pass

        def upload(self, path):
            return str(path)

        def render(self, graph, destination, **kwargs):
            is_finish = any(n["class_type"] == "StudioFrameInterpolateSampled" for n in graph.values())
            renders.append("finish" if is_finish else "refine")
            if is_finish and failing[0]:
                failing[0] = False
                raise RuntimeError("simulated interrupted finish")
            destination.write_bytes(b"test rendered video")
            infos[str(destination)] = dict(width=1920 if is_finish else 960, height=1080 if is_finish else 544,
                frames=450 if is_finish else 361, fps=30.0 if is_finish else 24.0, duration=15 if is_finish else 361/24)
            return {"path": str(destination), "prompt_id": "fake-only-in-test"}

    def prepare(source, destination, **kwargs):
        prepares.append(kwargs)
        destination.write_bytes(b"test prepared video")
        return {"path": str(destination), "frames": 361}

    def finalize(source, original, destination, **kwargs):
        finishes.append(kwargs)
        destination.write_bytes(b"test final video")
        return {**infos[str(source)], "path": str(destination), "loop": False}

    monkeypatch.setattr(pp, "PostprocessComfyClient", Client)
    monkeypatch.setattr(pp, "probe_video", lambda p: infos[str(p)])
    monkeypatch.setattr(pp, "prepare_input", prepare)
    monkeypatch.setattr(pp, "finalize_uniform_video", finalize)
    monkeypatch.setattr(pp, "stitch_segments", lambda *a, **k: pytest.fail("A stable loop must not stitch independent diffusion chunks"))
    monkeypatch.setattr(pp, "capabilities", lambda *a, **k: {"presets": [{"id": "redgraft_loop", "available": True}]})
    with pytest.raises(ValueError, match="开启循环"):
        pp.run_postprocess(source, output, "redgraft_loop")
    with pytest.raises(RuntimeError, match="simulated interrupted"):
        pp.run_postprocess(source, output, "redgraft_loop", loop_seconds=15)
    result = pp.run_postprocess(source, output, "redgraft_loop", loop_seconds=15)
    assert renders == ["refine", "finish", "finish"]
    assert len(prepares) == 1 and prepares[0]["duration"] == 361/24
    assert len(finishes) == 1 and finishes[0]["plan"]["duration"] == 15
    assert result["segments"] == 1 and result["resampling"] and result["loop_method"] == "cyclic_rife"
    pp.run_postprocess(source, output, "redgraft_loop", loop_seconds=15)
    assert renders == ["refine", "finish", "finish"]


def test_rtx_loop_preserves_raw_pose_cadence_and_never_loads_diffusion(tmp_path, monkeypatch):
    source, output = tmp_path / "source.mp4", tmp_path / "output.mp4"
    source.write_bytes(b"test original 24fps video")
    info = dict(width=864, height=480, fps=24.0, frames=36, duration=1.5)
    output_info = dict(width=1920, height=1080, fps=30.0, frames=45, duration=1.5)
    graphs = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class Client:
        base_url = "http://test.invalid"
        session = SimpleNamespace(get=lambda *a, **k: Response(), post=lambda *a, **k: Response())

        def __init__(self, *args):
            pass

        def upload(self, path):
            assert Path(path) == source
            return str(path)

        def render(self, graph, destination, **kwargs):
            graphs.append(graph)
            assert not any("Sampler" in n["class_type"] or "VAE" in n["class_type"] for n in graph.values())
            assert graph["8"]["inputs"]["images"] == ["7", 0]
            assert graph["4"]["inputs"]["indexes"] == ",".join(map(str, [*range(0, 36, 2), 0]))
            destination.write_bytes(b"test stable output")
            return {"path": str(destination), "prompt_id": "fake-only-in-test"}

    monkeypatch.setattr(pp, "PostprocessComfyClient", Client)
    monkeypatch.setattr(pp, "probe_video", lambda p: info if Path(p) == source else output_info)
    monkeypatch.setattr(pp, "prepare_input", lambda *a, **k: pytest.fail("A 24fps stable source must not be reencoded before RIFE"))
    monkeypatch.setattr(pp, "finalize_uniform_video", lambda s, original, d, **k: {**output_info, "path": str(d)})
    monkeypatch.setattr(pp, "capabilities", lambda *a, **k: {"presets": [{"id": "rtx_loop", "available": True}]})
    result = pp.run_postprocess(source, output, "rtx_loop", loop_seconds=1.5, cadence_stride=2)
    assert len(graphs) == 1 and len(result["jobs"]) == 1
    assert not result["resampling"] and result["refinement_canvas"] is None
    assert pp.required_models("rtx_loop") == [("frame_interpolation", "rife_v4.26.safetensors")]


def test_anime_timeline_preserves_finite_duration_and_closes_only_explicit_loops():
    plan = pp.video_timeline_plan(source_frames=48, source_fps=24, fps=30)
    assert plan["duration"] == 2 and plan["output_frames"] == 60
    assert plan["anchors"] == [*range(48), 47]
    assert plan["selection"] == list(range(0, 240, 4))
    assert plan["source_fps"] == 24 and plan["cadence_stride"] == 1
    loop = pp.video_timeline_plan(source_frames=362, source_fps=24, fps=30, loop_seconds=15, cadence_stride=2)
    assert loop["anchors"] == [*range(0, 360, 2), 0]
    assert loop["selection"] == list(range(0, 900, 2))
    assert loop["source_span"] == 15 and loop["selection"][-1] < 900
    fractional = pp.video_timeline_plan(source_frames=60, source_fps=30000/1001, fps=30)
    assert fractional["source_fps"] == 30000/1001
    assert fractional["duration"] == pytest.approx(60/(30000/1001), abs=1/30)
    with pytest.raises(ValueError, match="24fps"):
        pp.video_timeline_plan(source_frames=60, source_fps=30, cadence_stride=2)


def test_anime_graph_never_downscales_source_or_loads_diffusion_and_has_fps_passthrough():
    graph = pp.build_anime_sr_workflow("native.mp4", source_frames=49, source_fps=24, fps=30)
    assert graph["3"]["inputs"]["images"] == ["2", 0]
    assert graph["11"]["inputs"]["images"] == ["8", 0]
    assert graph["8"]["class_type"] == "StudioFrameInterpolateSampled"
    assert graph["10"]["inputs"]["model_name"] == "realesr-animevideov3.pth"
    assert not any("VAE" in n["class_type"] or "Sampler" in n["class_type"] or n["class_type"] == "ImageScale" for n in graph.values())
    native = pp.build_anime_sr_workflow("native.mp4", source_frames=49, source_fps=24, fps=24)
    assert native["11"]["inputs"]["images"] == ["2", 0]
    assert not any("Interpolate" in n["class_type"] for n in native.values())
    assert pp.aspect_policy(1280, 736) == "center_crop_small_grid_padding"
    assert pp.aspect_policy(1080, 1920) == "contain_black_padding"


def test_uniform_finalizer_keeps_exact_video_packets_and_audio_time_map(tmp_path):
    import av
    source, rendered, output = (tmp_path / name for name in ("source.mp4", "rendered.mp4", "output.mp4"))
    pp._run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=24", "-f", "lavfi", "-i", "sine=frequency=317:sample_rate=48000", "-t", "1.5", "-c:v", "libx264", "-c:a", "aac", source])
    pp._run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:size=1920x1080:rate=30", "-t", "1.6", "-c:v", "libx264", rendered])
    plan = pp.video_timeline_plan(source_frames=36, source_fps=24, fps=30, loop_seconds=1.6)
    result = pp.finalize_uniform_video(rendered, source, output, plan=plan)
    assert result["frames"] == 48 and result["duration"] == 1.6 and result["has_audio"]
    def packets(path):
        with av.open(str(path)) as container:
            return [bytes(p) for p in container.demux(video=0) if p.size]
    assert packets(output) == packets(rendered)
    with av.open(str(output)) as container:
        audio = container.streams.audio[0]
        assert float(audio.duration*audio.time_base) == pytest.approx(1.6, abs=.025)
        from array import array
        samples = array("f")
        for frame in container.decode(audio):
            assert frame.format.name == "fltp"
            samples.frombytes(bytes(frame.planes[0])[:frame.samples*4])
        assert abs(samples[0]) < .003
        assert max(abs(value) for value in samples[-32:]) < .003


def test_anime_worker_reuses_only_matching_native_timeline_receipts(tmp_path, monkeypatch):
    source, output = tmp_path / "original.mp4", tmp_path / "finished.mp4"
    source.write_bytes(b"original native pixels")
    info = dict(width=1280, height=736, frames=48, fps=24.0, duration=2., has_audio=True)
    result_info = dict(width=1920, height=1080, frames=60, fps=30.0, duration=2., has_audio=False)
    graphs, finishes = [], []
    class Client:
        base_url = "http://test.invalid"
        session = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}), post=lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None))
        def __init__(self, *a): pass
        def upload(self, path):
            assert path == source
            return "native.mp4"
        def render(self, graph, destination, **kwargs):
            graphs.append(graph)
            destination.write_bytes(b"rendered")
            return {"prompt_id": "fake-only-in-test"}
    def finish(rendered, original, destination, *, plan, target_resolution):
        assert original == source
        finishes.append(plan)
        return {**result_info, "path": str(destination)}
    monkeypatch.setattr(pp, "PostprocessComfyClient", Client)
    monkeypatch.setattr(pp, "capabilities", lambda *a, **k: {"presets": [{"id": "anime_sr", "available": True}]})
    monkeypatch.setattr(pp, "probe_video", lambda path: info if Path(path) == source else result_info)
    monkeypatch.setattr(pp, "finalize_uniform_video", finish)
    monkeypatch.setattr(pp, "prepare_input", lambda *a, **k: pytest.fail("Never convert native input before SR"))
    monkeypatch.setattr(pp, "stitch_segments", lambda *a, **k: pytest.fail("No independent movie chunks"))
    first = pp.run_postprocess(source, output, preset="anime_sr")
    pp.run_postprocess(source, output, preset="anime_sr")
    assert len(graphs) == 1 and first["segments"] == 1 and first["resampling"] is False
    assert first["cadence_stride"] == 1 and first["source_resolution"] == [1280, 736]
    pp.run_postprocess(source, output, preset="anime_sr", cadence_stride=2)
    assert len(graphs) == 2 and finishes[-1]["cadence_stride"] == 2


def test_legacy_loop_worker_respects_explicit_stride_one():
    graph = pp.build_rtx_loop_workflow("original.mp4", source_frames=36, duration=1.5, cadence_stride=1)
    assert graph["4"]["inputs"]["indexes"] == ",".join(map(str, [*range(36), 0]))


def test_every_ltx_preset_uses_full_temporal_decode_and_no_short_encode_windows():
    for preset in pp.PRESETS:
        if "ltx" not in preset and "redgraft" not in preset:
            continue
        graph = pp.build_workflow("source.mp4", preset=preset, frames=361)
        decoders = [n for n in graph.values() if n["class_type"] == "VAEDecodeTiled"]
        assert decoders and all(n["inputs"]["temporal_size"] == 4096 for n in decoders), preset
        assert not any(n["class_type"] == "VAEEncodeTiled" for n in graph.values()), preset
    low = pp.build_workflow("native.mp4", preset="redgraft_refine", source_width=864, source_height=480)
    assert low["4"]["inputs"]["width"] == 960
    assert low["51"]["class_type"] == "LTXVLatentUpsampler"
    assert low["28"]["inputs"]["denoise"] == .15
    high = pp.build_workflow("native.mp4", preset="redgraft_refine", source_width=1280, source_height=736)
    assert high["4"]["inputs"]["width"] == 1920 and "51" not in high
    assert not any(n["class_type"] == "RTXVideoSuperResolution" for n in high.values())


@pytest.mark.parametrize("target", ["720p", "1080p", "1440p"])
def test_all_backends_render_requested_resolution_before_final_mux(target):
    width, height = pp.output_size(target)
    anime = pp.build_anime_sr_workflow("source.mp4", target_resolution=target)
    assert (anime["11"]["inputs"]["width"], anime["11"]["inputs"]["height"]) == (width, height)
    for preset in ("redgraft_refine", "redgraft_pixel_2stage", "ltx25_pixel_2x", "ltx23_refine", "h3_refine", "dlss_sr"):
        graph = pp.build_workflow("source.mp4", preset=preset, target_resolution=target)
        last_id = graph["90"]["inputs"]["images"][0]
        last = graph[last_id]["inputs"]
        assert (last["width"], last["height"]) == (width, height), preset
    refine = pp.build_workflow("source.mp4", preset="redgraft_refine", target_resolution=target)
    latent_width = refine["4"]["inputs"]["width"] * (2 if "51" in refine else 1)
    latent_height = refine["4"]["inputs"]["height"] * (2 if "51" in refine else 1)
    assert latent_width >= width and latent_height >= height
    rtx = pp.build_workflow("source.mp4", preset="rtx_vsr", target_resolution=target)
    assert rtx["4"]["inputs"]["resize_type.width"] == width
    loop = pp.build_loop_finish_workflow("source.mp4", target_resolution=target)
    assert loop["4"]["inputs"]["resize_type.height"] == height


@pytest.mark.parametrize("target", ["720p", "1440p"])
def test_uniform_finalizer_preserves_target_size_without_hidden_1080_resize(tmp_path, target):
    width, height = pp.output_size(target)
    original, rendered, result = (tmp_path/name for name in ("source.mp4", "rendered.mp4", "result.mp4"))
    pp._run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:size=160x90:rate=24", "-t", "0.5", "-c:v", "libx264", original])
    pp._run_ffmpeg(["-f", "lavfi", "-i", f"color=c=blue:size={width}x{height}:rate=30", "-t", "0.5", "-c:v", "libx264", rendered])
    plan = pp.video_timeline_plan(source_frames=12, source_fps=24, fps=30)
    output = pp.finalize_uniform_video(rendered, original, result, plan=plan, target_resolution=target)
    assert (output["width"], output["height"], output["frames"]) == (width, height, 15)


def test_timeline_removes_diffusion_padding_even_when_24fps_requires_no_interpolation():
    graph = pp.build_timeline_workflow("361frames.mp4", source_frames=360, source_fps=24, fps=24)
    selector = graph[graph["90"]["inputs"]["images"][0]]
    assert selector["class_type"] == "GetImagesFromBatchIndexed"
    assert selector["inputs"]["indexes"] == ",".join(map(str, range(360)))


def test_rtx_preprocessing_uses_exact_target_aspect_without_anisotropic_stretch():
    for preset in ("rtx_vsr", "ltx23_refine", "h3_refine"):
        for target in pp.OUTPUT_RESOLUTIONS:
            graph = pp.build_workflow("odd-source.mp4", preset=preset, source_width=853, source_height=480, target_resolution=target)
            before, after = graph["3"]["inputs"], graph["4"]["inputs"]
            assert before["width"] >= 853
            assert before["width"]*after["resize_type.height"] == before["height"]*after["resize_type.width"]
            assert before["crop"] == "center"


def test_uniform_finalizer_does_not_invent_audio_for_silent_source(tmp_path):
    original, rendered, result = (tmp_path/name for name in ("silent.mp4", "rendered.mp4", "out.mp4"))
    pp._run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:size=160x90:rate=24", "-t", "0.5", "-c:v", "libx264", original])
    pp._run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:size=1920x1080:rate=30", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "0.5", "-c:v", "libx264", "-c:a", "aac", rendered])
    plan = pp.video_timeline_plan(source_frames=12, source_fps=24, fps=30)
    info = pp.finalize_uniform_video(rendered, original, result, plan=plan)
    assert not info["has_audio"] and info["audio_method"] == "none" and info["frames"] == 15


def test_bounded_rife_adapter_retains_every_requested_sample_across_block_boundaries(monkeypatch):
    class Scalar(float):
        def to(self, **kwargs): return self
    class Tensor:
        device = "cpu"
        dtype = "float32"
        def __init__(self, values): self.values = list(values)
        @property
        def shape(self): return (len(self.values), 1, 1, 1)
        def __len__(self): return len(self.values)
        def __getitem__(self, index):
            return Tensor(self.values[index]) if isinstance(index, slice) else Scalar(self.values[index])
        def __setitem__(self, index, value): self.values[index] = value
        def to(self, **kwargs): return self
    blocks = []
    def interpolate(model, images, multiplier):
        blocks.append(len(images))
        values = []
        for left, right in zip(images, images[1:]):
            values.extend(left+(right-left)*step/multiplier for step in range(multiplier))
        return (Tensor([*values, images[-1]]),)
    manager = SimpleNamespace(intermediate_device=lambda: "cpu", throw_exception_if_processing_interrupted=lambda: None)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(empty=lambda shape, **k: Tensor([0]*shape[0])))
    monkeypatch.setitem(sys.modules, "nodes", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "comfy", SimpleNamespace(model_management=manager))
    monkeypatch.setitem(sys.modules, "comfy.utils", SimpleNamespace(ProgressBar=lambda n: SimpleNamespace(update=lambda n: None)))
    monkeypatch.setitem(sys.modules, "comfy_extras.nodes_upscale_model", SimpleNamespace(ImageUpscaleWithModel=object))
    monkeypatch.setitem(sys.modules, "comfy_extras.nodes_frame_interpolation", SimpleNamespace(FrameInterpolate=SimpleNamespace(execute=interpolate)))
    path = Path(__file__).resolve().parents[1] / "integrations" / "comfyui" / "ComfyUI-StudioPost" / "__init__.py"
    spec = importlib.util.spec_from_file_location("bounded_rife_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    images = Tensor(range(20))
    selection = [0, 4, 39, 40, 41, 79, 80, 94, 95]
    actual = module.StudioFrameInterpolateSampled().interpolate(None, images, 5, ",".join(map(str, selection)))[0]
    assert actual.values == pytest.approx([value/5 for value in selection])
    assert blocks == [9, 9, 4]
