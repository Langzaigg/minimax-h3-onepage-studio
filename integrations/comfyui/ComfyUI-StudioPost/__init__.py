"""Memory-bounded adapters around ComfyUI's official SR and RIFE execution.

No new networks or interpolation math: these nodes only discard unneeded
intermediates early. Workflows remain ordinary exportable ComfyUI API graphs.
"""
import torch
import nodes
from comfy import model_management
from comfy.utils import ProgressBar
from comfy_extras.nodes_upscale_model import ImageUpscaleWithModel
from comfy_extras.nodes_frame_interpolation import FrameInterpolate


class StudioImageUpscaleToSize:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"upscale_model": ("UPSCALE_MODEL",), "images": ("IMAGE",),
            "width": ("INT", {"default": 1920, "min": 64, "max": 8192}),
            "height": ("INT", {"default": 1080, "min": 64, "max": 8192})}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "upscale"
    CATEGORY = "studio/video"
    DESCRIPTION = "Run the official image SR node per frame at native input size, then resize the 4x result immediately. No temporal processing or input downscale."

    def upscale(self, upscale_model, images, width, height):
        result = torch.empty((len(images), height, width, images.shape[-1]), dtype=images.dtype,
                             device=model_management.intermediate_device())
        progress = ProgressBar(len(images))
        scale = nodes.ImageScale()
        for index in range(len(images)):
            model_management.throw_exception_if_processing_interrupted()
            enlarged = ImageUpscaleWithModel.execute(upscale_model, images[index:index+1])[0]
            source_h, source_w = enlarged.shape[1:3]
            if abs((source_w/source_h)/(width/height)-1) <= .03:
                finished = scale.upscale(enlarged, "lanczos", width, height, "center")[0]
            else:
                factor = min(width/source_w, height/source_h)
                fit_w, fit_h = max(1, round(source_w*factor)), max(1, round(source_h*factor))
                fitted = scale.upscale(enlarged, "lanczos", fit_w, fit_h, "disabled")[0]
                finished = torch.zeros((1, height, width, images.shape[-1]), dtype=fitted.dtype, device=fitted.device)
                x, y = (width-fit_w)//2, (height-fit_h)//2
                finished[:, y:y+fit_h, x:x+fit_w] = fitted
                del fitted
            result[index] = finished[0].to(device=result.device, dtype=result.dtype)
            del enlarged, finished
            progress.update(1)
        return (result,)


class StudioFrameInterpolateSampled:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"interp_model": ("INTERP_MODEL",), "images": ("IMAGE",),
            "multiplier": ("INT", {"default": 5, "min": 2, "max": 16}),
            "selection": ("STRING", {"default": "0,4,8", "multiline": True})}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "interpolate"
    CATEGORY = "studio/video"
    DESCRIPTION = "Run native ComfyUI RIFE in bounded groups of adjacent pairs and retain only requested uniform-timeline samples. No repeated full 5x sequence in RAM."

    def interpolate(self, interp_model, images, multiplier, selection):
        if len(images) < 2:
            raise ValueError("RIFE requires at least two anchor frames")
        indexes = [int(part.strip()) for part in selection.split(",")]
        maximum = (len(images)-1)*multiplier
        if not indexes or indexes != sorted(set(indexes)) or indexes[0] < 0 or indexes[-1] > maximum:
            raise ValueError("Interpolation selection must be sorted, unique and within the generated timeline")
        result = torch.empty((len(indexes), *images.shape[1:]), dtype=images.dtype,
                             device=model_management.intermediate_device())
        cursor, progress = 0, ProgressBar(len(indexes))
        # RIFE is pairwise. Adjacent groups share their endpoint and are exactly
        # the same pairs/substeps as one native call; no dissolve or re-timing.
        for first in range(0, len(images)-1, 8):
            model_management.throw_exception_if_processing_interrupted()
            last = min(first+8, len(images)-1)
            lower, upper = first*multiplier, last*multiplier
            block = FrameInterpolate.execute(interp_model, images[first:last+1], multiplier)[0]
            while cursor < len(indexes) and (indexes[cursor] < upper or last == len(images)-1):
                local = indexes[cursor]-lower
                result[cursor] = block[local].to(device=result.device, dtype=result.dtype)
                cursor += 1
                progress.update(1)
            del block
        if cursor != len(indexes):
            raise RuntimeError("Interpolation did not emit the complete requested timeline")
        return (result,)


NODE_CLASS_MAPPINGS = {
    "StudioImageUpscaleToSize": StudioImageUpscaleToSize,
    "StudioFrameInterpolateSampled": StudioFrameInterpolateSampled,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "StudioImageUpscaleToSize": "Studio: Model SR → Final Size (bounded memory)",
    "StudioFrameInterpolateSampled": "Studio: Native RIFE Selected Timeline (bounded memory)",
}
