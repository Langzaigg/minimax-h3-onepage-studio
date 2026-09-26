import json
from pathlib import Path

import pytest
from PIL import Image

from services import image_generation as images


def test_portrait_reference_is_contained_without_losing_top_or_bottom(tmp_path):
    source = tmp_path / "portrait.png"
    original = Image.new("RGB", (100, 200), (20, 20, 20))
    original.paste((255, 0, 0), (0, 0, 100, 20))
    original.paste((0, 0, 255), (0, 180, 100, 200))
    original.save(source)
    target = tmp_path / "prepared.png"
    assert images.prepare_reference(source, target, 320, 180)
    with Image.open(target) as result:
        assert result.size == (320, 180)
        assert result.getpixel((160, 2))[0] > 240
        assert result.getpixel((160, 177))[2] > 240
        assert result.mode == "RGB"


def test_text_only_workflow_has_requested_latent_size_and_edits_use_reference_latent():
    text = images.build_image_workflow("A castle at night", 2048, 1152, 31)
    assert text["12"]["class_type"] == "EmptyLatentImage"
    assert text["12"]["inputs"] == {"width": 2048, "height": 1152, "batch_size": 1}
    assert text["8"]["inputs"]["latent_image"] == ["12", 0]
    edit = images.build_image_workflow("Retain the character", 2048, 1152, 31,
                                       ["first.png", "second.png", "third.png", "fourth.png"])
    assert "12" not in edit
    assert edit["8"]["inputs"]["latent_image"] == ["6", 2]
    assert edit["6"]["inputs"]["images.image_4"] == ["24", 0]
    assert edit["7"]["inputs"] == {"model": ["3", 0], "device": "auto", "dtype": "default"}
    assert edit["8"]["inputs"]["steps"] == 25
    assert edit["8"]["inputs"]["cfg"] == 1.0
    assert edit["10"]["inputs"]["images"] == ["11", 0]
    with pytest.raises(ValueError):
        images.build_image_workflow("Too many", 2048, 1152, 31, ["ref.png"] * 5)


class FakeComfy:
    instances = []

    def __init__(self):
        self.base_url = "http://127.0.0.1:8188"
        self.session = type("Session", (), {"close": lambda _: None})()
        self.submitted = []
        self.uploaded = []
        self.instances.append(self)

    def json(self, method, route, **kwargs):
        if route == "/prompt":
            self.submitted.append(kwargs["json"]["prompt"])
            return {"prompt_id": "test-prompt-id"}
        if route.startswith("/history/"):
            return {"test-prompt-id": {"status": {"completed": True, "status_str": "success"},
                "outputs": {"10": {"images": [{"filename": "result.png", "type": "output"}]}}}}
        raise AssertionError(route)

    def upload(self, path):
        self.uploaded.append(path)
        return path.name

    def download(self, descriptor, destination):
        Image.new("RGBA", (1536, 864), (40, 80, 120, 255)).save(destination)


@pytest.fixture
def fake_comfy(monkeypatch):
    FakeComfy.instances = []
    monkeypatch.setattr(images, "ImageComfyClient", FakeComfy)
    def no_websocket(*args, **kwargs):
        raise OSError("no websocket; exercise history fallback")
    monkeypatch.setattr(images, "connect", no_websocket)
    return FakeComfy


def test_reference_generation_records_comfy_id_pads_and_returns_rgb(tmp_path, fake_comfy):
    source = tmp_path / "source.png"
    Image.new("RGB", (100, 150), (50, 50, 50)).save(source)
    submitted, progress = [], []
    result = images.run_image_generation("保留完整角色和标题：狼仔鸽鸽", tmp_path / "job",
        images=[source], preset="landscape-draft", seed=99, on_submitted=submitted.append,
        progress_callback=lambda pct, stage: progress.append((pct, stage)))
    assert submitted == ["test-prompt-id"]
    assert result["seed"] == 99
    assert result["width"] == 1536 and result["height"] == 864
    assert images.PAD_INSTRUCTION in result["prompt"]
    with Image.open(result["image_path"]) as image:
        assert image.mode == "RGB"
    saved = json.loads((tmp_path / "job" / "image_generation_state.json").read_text(encoding="utf-8"))
    assert saved["completed"] and saved["comfy_prompt_id"] == result["comfy_prompt_id"]
    assert progress[-1][0] == 100


def test_resume_reads_existing_history_without_submitting_or_repolishing(tmp_path, fake_comfy, monkeypatch):
    state = {"comfy_prompt_id": "test-prompt-id", "client_id": "old-client", "seed": 812,
             "width": 1536, "height": 864, "prompt": "Persisted exact prompt", "polished_prompt": "Kept"}
    (tmp_path / "image_generation_state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(images, "polish_image_prompt", lambda *args: pytest.fail("Resumed work must not call LLM"))
    result = images.run_image_generation("New request text", tmp_path, resume_prompt_id="test-prompt-id",
                                         polish_prompt_first=True)
    assert not fake_comfy.instances[0].submitted
    assert result["seed"] == 812 and result["prompt"] == "Persisted exact prompt"


def test_history_execution_errors_are_not_reported_as_success():
    with pytest.raises(RuntimeError, match="out of memory"):
        images._history_result({"status": {"status_str": "error", "messages": [
            ["execution_error", {"exception_message": "CUDA out of memory"}]]}})
    with pytest.raises(RuntimeError, match="没有输出"):
        images._history_result({"status": {"completed": True}, "outputs": {}})


def test_prompt_polish_keeps_exact_user_text_and_images_in_multimodal_request(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    Image.new("RGB", (120, 80), "navy").save(source)
    captured = []
    class FakeLLM:
        def complete_text(self, messages, options):
            captured.append(messages)
            return "完整保留角色，标题为狼仔鸽鸽，扩展月夜背景。"
    monkeypatch.setattr(images, "LLMClient", FakeLLM)
    result = images.polish_image_prompt("准确标题：狼仔鸽鸽", [source], "landscape-hd")
    assert "狼仔鸽鸽" in result
    content = captured[0][1]["content"]
    assert "准确标题：狼仔鸽鸽" in content[0]["text"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
