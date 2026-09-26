"""Exact delivery sizes and source recipes traced to published workflows."""

from fractions import Fraction
import re

TARGET_RESOLUTIONS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
}


def target_size(name="1080p"):
    try:
        return TARGET_RESOLUTIONS[name]
    except (KeyError, TypeError):
        raise ValueError("交付目标须为720p、1080p或2K（2560×1440）") from None


WORKFLOW_SOURCE = "https://github.com/peterducan-hub/PeterDuncan_Comfyui/blob/dbec37e38bc6d670f07426ed1e713c2a4cc4bfb5/MINIMAX_H3_LTX2.5_Upscaler_v1.json"
_SOURCE_PROFILES = {
    "720p": (736, 416, .3),
    "1080p": (960, 544, .5),
    "1440p": (1376, 768, 1.0),
}


def parse_aspect(value: str) -> Fraction:
    """Parse dimensions exactly; decimal custom ratios never pass through float."""
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("画面比例须为正数宽:高，例如16:9")
    parts = value.strip().split(":")
    if len(parts) != 2 or any(not re.fullmatch(r"\d+(?:\.\d+)?", part.strip()) for part in parts):
        raise ValueError("画面比例须为正数宽:高，例如16:9")
    try:
        ratio = Fraction(parts[0].strip()) / Fraction(parts[1].strip())
    except ZeroDivisionError:
        raise ValueError("画面比例的宽和高必须大于0") from None
    if ratio <= 0:
        raise ValueError("画面比例的宽和高必须大于0")
    return ratio


def ratio_text(ratio: Fraction) -> str:
    return f"{ratio.numerator}:{ratio.denominator}"


def fit_even_aspect(width: int, height: int, ratio: Fraction) -> tuple[int, int]:
    """Largest exact-ratio even-pixel rectangle fully inside the sampling grid."""
    ratio = Fraction(ratio)
    unit_width, unit_height = 2 * ratio.numerator, 2 * ratio.denominator
    multiplier = min(int(width) // unit_width, int(height) // unit_height)
    if multiplier < 1:
        raise ValueError(
            f"当前采样画布无法容纳精确比例{ratio_text(ratio)}的最小偶数尺寸"
            f"{unit_width}×{unit_height}；请选择标准比例或提高像素预算"
        )
    return unit_width * multiplier, unit_height * multiplier


def resolution_profiles() -> list[dict]:
    rows = []
    for name, (sampling_width, sampling_height, megapixels) in _SOURCE_PROFILES.items():
        source_width, source_height = fit_even_aspect(sampling_width, sampling_height, Fraction(16, 9))
        output_width, output_height = target_size(name)
        padded_width, padded_height = ((source_width + 31) // 32 * 32,
                                       (source_height + 31) // 32 * 32)
        native_width, native_height = source_width * 2, source_height * 2
        rows.append({
            "id": name, "label": "2K · 1440p" if name == "1440p" else name,
            "source_width": source_width, "source_height": source_height,
            "sampling_width": sampling_width, "sampling_height": sampling_height,
            "source_megapixels": megapixels, "aspect_ratio": "16:9",
            "output_width": output_width, "output_height": output_height,
            "native_2x_width": native_width, "native_2x_height": native_height,
            "msr_padded_source_width": padded_width, "msr_padded_source_height": padded_height,
            "msr_sampling_width": padded_width * 2, "msr_sampling_height": padded_height * 2,
            "workflow_source": WORKFLOW_SOURCE,
            "author_saved_selection": name == "1440p",
            "description": (
                "作者保存的1376×768档；源成片裁至严格16:9，交付尺寸为本机适配。"
                if name == "1440p" else
                "选择网上作者工作流已有尺寸档；源成片裁至严格16:9，交付尺寸为本机适配。"
            ) + (f" MSR按实际源{source_width}×{source_height}做latent2×，"
                 f"内容为{native_width}×{native_height}，最后交付{output_width}×{output_height}。"),
        })
    return rows


def source_profile(name: str) -> dict:
    for row in resolution_profiles():
        if row["id"] == name:
            return row
    raise ValueError("初采方案须为720p、1080p或1440p；自定义像素预算请留空")
