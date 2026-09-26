import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_comfyui_adapters.py"
spec = importlib.util.spec_from_file_location("studio_adapter_installer", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def target(tmp_path):
    root = tmp_path / "ComfyUI"
    dlss = root / "custom_nodes/ComfyUI-DLSS5"
    dlss.mkdir(parents=True)
    extra = root / "comfy_extras"
    extra.mkdir()
    (root / "nodes.py").write_text("class ImageScale:\n def upscale(self): pass\n")
    for filename, classname in (("nodes_upscale_model.py", "ImageUpscaleWithModel"),
                                ("nodes_frame_interpolation.py", "FrameInterpolate")):
        (extra / filename).write_text(f"class {classname}:\n def execute(self): pass\n")
    (dlss / "__init__.py").write_text(
        "from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS\n"
        "WEB_DIRECTORY = './web'\nUSER_EXTENSION = 42\n")
    names = installer.DLSS_REQUIRED_SYMBOLS
    declarations = "\n".join(f"{name} = None" for name in names)
    (dlss / "nodes.py").write_text(declarations + "\n" +
        "def spawn():\n    process = subprocess.Popen(command, stdout=stdout, stderr=stderr)\n" +
        "def probe():\n    return subprocess.run([],\n            timeout=10,\n        )\n")
    keys = installer.BRIDGE_REQUIRED_SETTINGS
    (dlss / "bridge_runner.py").write_text("kwargs = {" + ",".join(repr(key) + ": 1" for key in keys) +
                                         "}\nsource = core.dlssnr.Enhance(source, **kwargs)\n")
    return root


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_check_is_read_only_and_install_preserves_entrypoint_and_backups(tmp_path):
    root = target(tmp_path)
    before = files(root)
    plan = installer.plan_install(root)
    assert plan["changes"] and files(root) == before
    result = installer.install(plan)
    assert result["updated"]
    entry = (root / "custom_nodes/ComfyUI-DLSS5/__init__.py").read_text()
    assert "USER_EXTENSION = 42" in entry and "WEB_DIRECTORY = './web'" in entry
    assert entry.count(installer.BEGIN) == 1
    assert (Path(result["backup_directory"]) / "custom_nodes/ComfyUI-DLSS5/__init__.py").read_bytes() == before["custom_nodes/ComfyUI-DLSS5/__init__.py"]
    assert "CREATE_NO_WINDOW" in (root / "custom_nodes/ComfyUI-DLSS5/nodes.py").read_text()
    assert not installer.plan_install(root)["changes"]
    again = installer.install(installer.plan_install(root))
    assert again["updated"] == [] and again["backup_directory"] is None


def test_missing_upstream_interface_fails_before_any_write(tmp_path):
    root = target(tmp_path)
    (root / "custom_nodes/ComfyUI-DLSS5/bridge_runner.py").write_text("pass\n")
    before = files(root)
    with pytest.raises(ValueError, match="bridge"):
        installer.plan_install(root)
    assert files(root) == before


def test_unknown_subprocess_shape_is_rejected_instead_of_blind_patch(tmp_path):
    root = target(tmp_path)
    path = root / "custom_nodes/ComfyUI-DLSS5/nodes.py"
    path.write_text(path.read_text().replace("timeout=10,", "timeout=11,"))
    before = files(root)
    with pytest.raises(ValueError, match="subprocess"):
        installer.plan_install(root)
    assert files(root) == before


def test_target_symlink_cannot_escape_comfy_root(tmp_path):
    root = target(tmp_path)
    escaped = tmp_path / "outside.py"
    escaped.write_text("do not overwrite\n")
    destination = root / "custom_nodes/ComfyUI-DLSS5/studio_filter.py"
    try:
        destination.symlink_to(escaped)
    except OSError:
        pytest.skip("Creating Windows symlinks requires an enabled developer mode")
    with pytest.raises(ValueError, match="outside|link|reparse"):
        installer.plan_install(root)
    assert escaped.read_text() == "do not overwrite\n"


def test_changed_target_invalidates_plan_without_writing_backups(tmp_path):
    root = target(tmp_path)
    plan = installer.plan_install(root)
    entry = root / "custom_nodes/ComfyUI-DLSS5/__init__.py"
    entry.write_text(entry.read_text() + "USER_CHANGED = True\n")
    before = files(root)
    with pytest.raises(ValueError, match="changed since preflight"):
        installer.install(plan)
    assert files(root) == before
    assert not (root / ".studio-adapter-backups").exists()


def test_registration_preserves_upstream_nodes_and_is_idempotent(monkeypatch):
    package = ModuleType("adapter_test_package")
    package.__path__ = [str(installer.SOURCES / "ComfyUI-DLSS5")]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    module = __import__(package.__name__ + ".studio_register", fromlist=["register_nodes"])
    class Easy:
        pass
    original = {"DLSS5EasyPipeline": Easy, "UnrelatedNode": object}
    labels = {"UnrelatedNode": "Keep me"}
    first, first_labels = module.register_nodes(original, labels, SimpleNamespace())
    second, second_labels = module.register_nodes(first, first_labels, SimpleNamespace())
    assert first["DLSS5EasyPipeline"] is second["DLSS5EasyPipeline"]
    assert second["UnrelatedNode"] is object and second_labels["UnrelatedNode"] == "Keep me"
    assert "StudioDLSS5Filter" in second
    assert original == {"DLSS5EasyPipeline": Easy, "UnrelatedNode": object}
    assert labels == {"UnrelatedNode": "Keep me"}
