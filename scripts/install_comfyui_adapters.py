"""Install Tujiang Studio's small ComfyUI adapters without models or runtimes.

--check performs read-only interface and file checks. Exit 0 means compatible;
the JSON up_to_date flag states whether installation would change files.
Installation backs up replaced files and never deletes directories or restarts.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[1]
SOURCES = PROJECT / "integrations/comfyui"
UPSTREAM_REVISION = "f4f59cd1fe39e785a3b600ca20b5e09194a9e38d"
BEGIN = "# BEGIN Frame Studio adapters (managed)"
END = "# END Frame Studio adapters (managed)"
REGISTRATION = """# BEGIN Frame Studio adapters (managed)
try:
    from . import nodes as _studio_implementation
    from .studio_register import register_nodes as _studio_register_nodes
except ImportError:
    import nodes as _studio_implementation
    from studio_register import register_nodes as _studio_register_nodes
NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = _studio_register_nodes(
    NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, _studio_implementation)
# END Frame Studio adapters (managed)
"""
ADAPTERS = (
    "ComfyUI-StudioPost/__init__.py",
    "ComfyUI-DLSS5/studio_filter.py",
    "ComfyUI-DLSS5/studio_integration.py",
    "ComfyUI-DLSS5/studio_register.py",
)
DLSS_REQUIRED_SYMBOLS = {
    "PACKAGE", "torch", "np", "_DEPTH_CACHE", "_RAFT_CACHE", "_bridge_diagnostics",
    "_easy_preset", "DLSS5DepthAnythingV2", "DLSS5OpticalFlow", "DLSS5RAFTFlow",
    "DLSS5TemporalDepthStabilize", "DLSS5EasyPipeline", "_runtime_paths",
    "_runtime_temp_dir", "_save_guides_chunked", "_run_process", "_runtime_timeout",
}
BRIDGE_REQUIRED_SETTINGS = {"style", "style_strength", "intensity", "local_structure",
                            "skin_structure", "auto_mask"}
HIDDEN_PROCESS_PATCHES = (
    ("process = subprocess.Popen(command, stdout=stdout, stderr=stderr)",
     "process = subprocess.Popen(command, stdout=stdout, stderr=stderr,\n"
     "                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == \"nt\" else 0)"),
    ("            timeout=10,\n        )",
     "            timeout=10,\n"
     "            creationflags=subprocess.CREATE_NO_WINDOW if os.name == \"nt\" else 0,\n        )"),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def contained(root, path):
    root, path = Path(root).resolve(), Path(path)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Target is outside ComfyUI root: {path.name}")
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Refusing linked target: {current.name}")
        if current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError(f"Refusing reparse target: {current.name}")
    return path


def source_text(root, relative):
    path = contained(root, root / relative)
    if not path.is_file():
        raise ValueError(f"Missing upstream file: {relative}")
    return path.read_text(encoding="utf-8-sig")


def symbols(text):
    tree = ast.parse(text)
    result = {node.name for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    result.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            result.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return result


def check_class(text, classname, method):
    tree = ast.parse(text)
    return any(isinstance(node, ast.ClassDef) and node.name == classname and
               any(isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == method
                   for member in node.body) for node in tree.body)


def registered_entry(text):
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError("Malformed existing Studio registration markers")
    if BEGIN in text:
        first, last = text.index(BEGIN), text.index(END) + len(END)
        text = text[:first] + REGISTRATION.rstrip() + text[last:]
    else:
        text = text.rstrip() + "\n\n" + REGISTRATION
    ast.parse(text)
    return text


def plan_install(comfy_root):
    root = Path(comfy_root).resolve(strict=True)
    if not root.is_dir() or not (root / "custom_nodes").is_dir():
        raise ValueError("Choose the ComfyUI directory containing nodes.py and custom_nodes")
    for path, classname, method in (
        ("nodes.py", "ImageScale", "upscale"),
        ("comfy_extras/nodes_upscale_model.py", "ImageUpscaleWithModel", "execute"),
        ("comfy_extras/nodes_frame_interpolation.py", "FrameInterpolate", "execute"),
    ):
        if not check_class(source_text(root, path), classname, method):
            raise ValueError(f"Missing ComfyUI interface: {classname}.{method}")
    dlss = "custom_nodes/ComfyUI-DLSS5/"
    upstream = source_text(root, dlss + "nodes.py")
    missing = DLSS_REQUIRED_SYMBOLS - symbols(upstream)
    if missing:
        raise ValueError("Incompatible DLSS upstream interfaces: " + ", ".join(sorted(missing)))
    bridge = source_text(root, dlss + "bridge_runner.py")
    bridge_literals = {node.value for node in ast.walk(ast.parse(bridge))
                       if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    if not BRIDGE_REQUIRED_SETTINGS <= bridge_literals or "core.dlssnr.Enhance" not in bridge:
        raise ValueError("Incompatible DLSS bridge; required native settings are not forwarded")
    entry = source_text(root, dlss + "__init__.py")
    if not {"NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"} <= symbols(entry):
        raise ValueError("DLSS entrypoint does not export the standard node mappings")
    for before, after in HIDDEN_PROCESS_PATCHES:
        if after in upstream:
            continue
        if upstream.count(before) != 1:
            raise ValueError("Unrecognized DLSS subprocess source; review before updating this upstream version")
        upstream = upstream.replace(before, after, 1)
    ast.parse(upstream)
    desired = {dlss + "nodes.py": upstream.encode("utf8"),
               dlss + "__init__.py": registered_entry(entry).encode("utf8")}
    for relative in ADAPTERS:
        content = (SOURCES / relative).read_bytes()
        ast.parse(content.decode("utf-8-sig"))
        desired["custom_nodes/" + relative] = content
    changes = []
    for relative, content in desired.items():
        target = contained(root, root / relative)
        old = target.read_bytes() if target.is_file() else None
        # Upstream files may use CRLF. Preserve their original bytes when the
        # normalized source already matches, avoiding line-ending-only writes.
        same = old == content or (relative.endswith(("/nodes.py", "/__init__.py")) and old is not None and
                                  old.decode("utf-8-sig").replace("\r\n", "\n") == content.decode("utf8"))
        if not same:
            changes.append({"path": relative, "before_sha256": digest(old) if old is not None else None,
                            "after_sha256": digest(content), "content": content})
    return {"root": root, "upstream_reference_revision": UPSTREAM_REVISION, "changes": changes}


def install(plan):
    root, changes = plan["root"], plan["changes"]
    if not changes:
        return {"updated": [], "backup_directory": None}
    # Validate every destination and original digest before the first write.
    for change in changes:
        target = contained(root, root / change["path"])
        actual = digest(target.read_bytes()) if target.is_file() else None
        if actual != change["before_sha256"]:
            raise ValueError("A target changed since preflight; run the installer again")
    backup = contained(root, root / ".studio-adapter-backups" /
                       (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]))
    backup.mkdir(parents=True, exist_ok=False)
    for change in changes:
        target = root / change["path"]
        if target.is_file():
            saved = backup / change["path"]
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(target.read_bytes())
    manifest = [{key: value for key, value in change.items() if key != "content"} for change in changes]
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    for change in changes:
        target = contained(root, root / change["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".studio-" + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(change["content"])
        os.replace(temporary, target)
    return {"updated": [change["path"] for change in changes], "backup_directory": str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfy-root", type=Path, default=PROJECT.parent / "ComfyUI")
    parser.add_argument("--check", action="store_true", help="Check compatibility and changes without writing")
    args = parser.parse_args(argv)
    try:
        plan = plan_install(args.comfy_root)
        report = {"mode": "check" if args.check else "install", "root": str(plan["root"]),
                  "compatible": True, "up_to_date": not plan["changes"],
                  "changes_required": [change["path"] for change in plan["changes"]],
                  "upstream_reference_revision": UPSTREAM_REVISION}
        if not args.check:
            report.update(install(plan))
            report["up_to_date"] = True
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, SyntaxError) as error:
        print(json.dumps({"compatible": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
