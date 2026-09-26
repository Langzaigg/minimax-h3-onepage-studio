"""Studio's explicit NR controls; upstream Easy node remains unchanged."""
from __future__ import annotations

import gc
import json
import math
from pathlib import Path
import tempfile

STYLES = {"default": 0, "natural": 1, "cinematic": 2}
# Native public-wrapper defaults are sent explicitly, never inferred from the
# installed DLL. The skin sentinel -1 inherits local_structure in that contract.
CONTROLS = {
    "output_blend": ("intensity", 1.0, 1.0),
    "local_tone": ("style_strength", .4, 1.0),
    "local_structure": ("local_structure", .5, 1.0),
    "skin_mask": ("skin_structure", .3, -1.0),
}


def native_settings(style="default", allow_experimental=False, **controls):
    if style not in STYLES or not isinstance(allow_experimental, bool):
        raise ValueError("Invalid NR style or experimental-range flag")
    allowed = set(CONTROLS) | {name + "_enabled" for name in CONTROLS}
    if controls.keys() - allowed:
        raise ValueError("Unknown NR controls")
    maximum = 5 if allow_experimental else 1
    settings = {"style": STYLES[style], "auto_mask": True, "feature_id": 18, "preset": 0, "depth_inverted": True}
    for name, (native_name, default, fallback) in CONTROLS.items():
        value, enabled = float(controls.get(name, default)), controls.get(name + "_enabled", True)
        if not math.isfinite(value) or not 0 <= value <= maximum or not isinstance(enabled, bool):
            raise ValueError(f"{name} must be finite within 0..{maximum}; enabled must be boolean")
        settings[native_name] = value if enabled else fallback
    return settings


def make_filter_node(implementation):
    class StudioDLSS5Filter:
        @classmethod
        def INPUT_TYPES(cls):
            required = {"image": ("IMAGE",), "style": (list(STYLES),),
                        "allow_experimental": ("BOOLEAN", {"default": False})}
            for name, (_, default, _) in CONTROLS.items():
                required[name] = ("FLOAT", {"default": default, "min": 0.0, "max": 5.0, "step": .01})
                required[name + "_enabled"] = ("BOOLEAN", {"default": True,
                    "tooltip": "Override this parameter; off restores the public wrapper default."})
            return {"required": required}

        RETURN_TYPES = ("IMAGE", "STRING")
        RETURN_NAMES = ("image", "native_parameters")
        FUNCTION = "run"
        CATEGORY = "Studio/Finishing"
        DESCRIPTION = "Same-size NR only, explicit controls, persistent guides; no SR or frame generation."

        def run(self, image, style="default", allow_experimental=False, **controls):
            settings = native_settings(style, allow_experimental, **controls)
            from comfy import model_management
            model_management.unload_all_models()
            model_management.soft_empty_cache()
            try:
                passed, diagnostic = implementation._bridge_diagnostics(("nr",))
                if not passed:
                    raise RuntimeError("DLSS NR preflight failed:\n" + diagnostic)
                # Identical to the existing Easy NR-only guide path, without its
                # opinionated three-look weighting or bounded sequence windows.
                scenario = "Still image" if image.shape[0] == 1 else "Short video / best quality"
                preset = implementation._easy_preset(scenario, int(image.shape[0]))
                depth = implementation.DLSS5DepthAnythingV2().estimate(image, "Small (recommended)", True, 4)[0]
                if preset["flow_model"] == "Optical Flow (fastest)":
                    motion = implementation.DLSS5OpticalFlow().estimate(image, .5, 5, 21)[0]
                else:
                    motion = implementation.DLSS5RAFTFlow().estimate(image, preset["flow_model"], preset["flow_chunk"])[0]
                if image.shape[0] > 1:
                    depth = implementation.DLSS5TemporalDepthStabilize().stabilize(depth, motion, .7, .08)[0]
                python, plugin, snippet = implementation._runtime_paths()
                source = image.detach().to(device="cpu", dtype=implementation.torch.float32)
                with tempfile.TemporaryDirectory(prefix="studio-dlss-filter-", dir=implementation._runtime_temp_dir()) as temporary:
                    folder = Path(temporary)
                    input_path, output_path = folder / "in.npy", folder / "out.npy"
                    implementation.np.save(input_path, source.numpy())
                    depth_path, motion_path = folder / "depth.npy", folder / "motion.npy"
                    implementation._save_guides_chunked(depth, motion, source.shape[1:3], depth_path, motion_path)
                    # All four settings are real native parameters. The upstream
                    # runner already forwards them unchanged, including >1.
                    command = [str(python), str(implementation.PACKAGE / "bridge_runner.py"),
                        str(input_path), str(output_path), "--plugin", str(plugin), "--snippet", str(snippet),
                        "--settings", json.dumps(settings), "--depth", str(depth_path), "--mvec", str(motion_path)]
                    process = implementation._run_process(command, timeout=implementation._runtime_timeout())
                    if process.returncode:
                        raise RuntimeError("DLSS NR failed:\n" + process.stdout + process.stderr)
                    result = implementation.torch.from_numpy(implementation.np.load(output_path))
                report = json.dumps(settings, sort_keys=True)
                return {"ui": {"text": [report]}, "result": (result.to(image.device), report)}
            finally:
                implementation._DEPTH_CACHE.clear()
                implementation._RAFT_CACHE.clear()
                gc.collect()
                model_management.soft_empty_cache()
    return StudioDLSS5Filter
