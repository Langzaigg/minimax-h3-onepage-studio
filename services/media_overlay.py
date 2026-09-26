"""CPU media compositing, with explicit geometry and unchanged base timing/audio.

The API layer owns file authorization. This module accepts local, authorized files
and never touches workspace/history records. No overlay is added by default.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import math
import os
from pathlib import Path
import shutil
import tempfile

import av
from PIL import Image, ImageOps, UnidentifiedImageError

from services.post_processing import _run_ffmpeg, probe_video

ANCHORS = (
    "top-left", "top-center", "top-right", "center-left", "center",
    "center-right", "bottom-left", "bottom-center", "bottom-right",
)
_ANCHOR_FACTORS = dict(zip(ANCHORS, ((0, 0), (.5, 0), (1, 0), (0, .5),
                                  (.5, .5), (1, .5), (0, 1), (.5, 1), (1, 1))))


def _number(value, name, *, integer=False, minimum=None):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number) or (integer and not number.is_integer()):
        raise ValueError(f"{name} must be a finite {'integer' if integer else 'number'}")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return int(number) if integer else number


def validate_options(*, x=None, y=None, width=0, height=0, width_percent=None,
                     anchor="top-left", offset_x=0, offset_y=0, opacity=1,
                     start=0, end=None, loop=False, preview_time=None,
                     overlay_frame_time=0, media_kind=None):
    """Validate options without reading files; return normalized function kwargs.

    Both explicit x/y override the anchor. Negative placement is legal and clips
    at the base edge. Zero dimensions retain the original size/aspect ratio.
    """
    if anchor not in ANCHORS:
        raise ValueError("Unknown overlay anchor")
    if (x is None) != (y is None):
        raise ValueError("Provide both overlay x and y, or neither")
    if media_kind not in (None, "image", "video"):
        raise ValueError("media_kind must be image or video")
    if not isinstance(loop, bool):
        raise ValueError("loop must be a boolean")
    options = dict(x=None if x is None else _number(x, "x", integer=True),
                   y=None if y is None else _number(y, "y", integer=True),
                   width=_number(width, "width", integer=True, minimum=0),
                   height=_number(height, "height", integer=True, minimum=0),
                   width_percent=None if width_percent is None else _number(width_percent, "width_percent", minimum=0),
                   anchor=anchor, offset_x=_number(offset_x, "offset_x", integer=True),
                   offset_y=_number(offset_y, "offset_y", integer=True),
                   opacity=_number(opacity, "opacity", minimum=0),
                   start=_number(start, "start", minimum=0),
                   end=None if end is None else _number(end, "end", minimum=0),
                   loop=loop,
                   preview_time=None if preview_time is None else _number(preview_time, "preview_time", minimum=0),
                   overlay_frame_time=_number(overlay_frame_time, "overlay_frame_time", minimum=0),
                   media_kind=media_kind)
    if options["opacity"] > 1:
        raise ValueError("opacity must be between 0 and 1")
    if options["end"] is not None and options["end"] <= options["start"]:
        raise ValueError("end must be greater than start")
    if options["width_percent"] is not None:
        if options["width_percent"] <= 0:
            raise ValueError("width_percent must be positive")
        if options["width"] or options["height"]:
            raise ValueError("Use width_percent or pixel width/height, not both")
    return options


def resolve_geometry(base_size, overlay_size, **options):
    """Pure geometry; returns actual bounds and the intersection with the base."""
    options = validate_options(**options)
    bw, bh = (_number(v, "base size", integer=True, minimum=1) for v in base_size)
    ow, oh = (_number(v, "overlay size", integer=True, minimum=1) for v in overlay_size)
    w, h = options["width"], options["height"]
    if options["width_percent"] is not None:
        w = max(1, round(bw * options["width_percent"] / 100))
    if not w and not h:
        w, h = ow, oh
    elif not w:
        w = max(1, round(h * ow / oh))
    elif not h:
        h = max(1, round(w * oh / ow))
    if options["x"] is not None:
        x, y = options["x"], options["y"]
    else:
        fx, fy = _ANCHOR_FACTORS[options["anchor"]]
        x, y = round((bw - w) * fx) + options["offset_x"], round((bh - h) * fy) + options["offset_y"]
    return {"x": x, "y": y, "width": w, "height": h,
            "bounds": [x, y, x + w, y + h],
            "visible_bounds": [min(bw, max(0, x)), min(bh, max(0, y)),
                               min(bw, max(0, x + w)), min(bh, max(0, y + h))]}


def probe_media(path, media_kind=None):
    """Identify supported image/video content, not just the supplied extension."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("Media file does not exist")
    if media_kind not in (None, "image", "video"):
        raise ValueError("media_kind must be image or video")
    if media_kind != "video":
        try:
            with Image.open(path) as image:
                if image.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError("Image overlay supports PNG, JPEG and WEBP")
                image.seek(0)
                displayed = ImageOps.exif_transpose(image)
                result = {"kind": "image", "width": displayed.width, "height": displayed.height,
                          "format": image.format, "animated_first_frame_only": bool(getattr(image, "n_frames", 1) > 1)}
                displayed.load()
                return result
        except UnidentifiedImageError:
            if media_kind == "image":
                raise ValueError("Invalid image media") from None
    try:
        info = probe_video(path)
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            codec = stream.codec_context.name
            # Native FFmpeg VP9 decoder can discard the WebM alpha side stream.
            alpha_decoder = "libvpx-vp9" if codec == "vp9" and stream.metadata.get("alpha_mode") == "1" else (
                "libvpx" if codec == "vp8" and stream.metadata.get("alpha_mode") == "1" else None)
        if info["width"] <= 0 or info["height"] <= 0 or info["duration"] <= 0:
            raise ValueError("Invalid or empty video")
        return {"kind": "video", **info, "alpha_decoder": alpha_decoder}
    except (av.error.FFmpegError, IndexError) as exc:
        raise ValueError("Invalid image/video media") from exc


def _decoder_options(info):
    return ["-c:v", info["alpha_decoder"]] if info.get("alpha_decoder") else []


def _image_frame(path, info, *, time=0, temporary_dir):
    if info["kind"] == "image":
        with Image.open(path) as image:
            image.seek(0)
            return ImageOps.exif_transpose(image).convert("RGBA")
    frame_path = Path(temporary_dir) / "overlay-frame.png"
    # Output-side seeking decodes from the beginning for precise frame selection.
    _run_ffmpeg([*_decoder_options(info), "-i", path, "-ss", time,
                 "-frames:v", 1, "-an", "-pix_fmt", "rgba", frame_path])
    if not frame_path.exists():
        raise ValueError("Overlay frame time is outside the video")
    with Image.open(frame_path) as image:
        return image.convert("RGBA")


def _video_signature(path):
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        time_base = stream.time_base
        timestamps = [frame.pts * frame.time_base for frame in container.decode(stream)]
    audio = []
    with av.open(str(path)) as container:
        for stream in container.streams.audio:
            audio.append({"codec": stream.codec_context.name, "rate": stream.codec_context.sample_rate,
                          "channels": stream.codec_context.channels, "hash": hashlib.sha256(),
                          "packets": 0, "timestamps": []})
        index = {stream.index: i for i, stream in enumerate(container.streams.audio)}
        for packet in container.demux():
            if packet.stream.type == "audio" and packet.size:
                item = audio[index[packet.stream.index]]
                item["hash"].update(bytes(packet)); item["packets"] += 1
                item["timestamps"].append((None if packet.pts is None else packet.pts * packet.time_base,
                                           None if packet.dts is None else packet.dts * packet.time_base,
                                           packet.duration * packet.time_base))
    for item in audio:
        item["hash"] = item["hash"].hexdigest()
    return {"timestamps": timestamps, "time_base": time_base, "audio": audio}


def _verify_video(before, before_signature, path):
    after = probe_video(path)
    signature = _video_signature(path)
    if any(after[k] != before[k] for k in ("width", "height", "has_audio")):
        raise RuntimeError("Overlay export changed base dimensions or audio presence")
    old, new = before_signature["timestamps"], signature["timestamps"]
    tolerance = max(Fraction(1, 1000000), before_signature["time_base"])
    if len(old) != len(new) or any(abs(a - b) > tolerance for a, b in zip(old, new)):
        raise RuntimeError("Overlay export changed base frame timestamps or frame count")
    if abs(after["fps"] - before["fps"]) > .001 or abs(after["duration"] - before["duration"]) > 1 / max(before["fps"], 1):
        raise RuntimeError("Overlay export changed base video timing")
    if signature["audio"] != before_signature["audio"]:
        raise RuntimeError("Overlay export changed base audio packets or timing")
    return {**after, "frames": len(new), "timing_verified": True, "audio_packets_preserved": True}


def apply_overlay(source, overlay, destination, *, x=None, y=None, width=0, height=0,
                  width_percent=None, anchor="top-left", offset_x=0, offset_y=0,
                  opacity=1, start=0, end=None, loop=False, preview_time=None,
                  overlay_frame_time=0, media_kind=None):
    """Composite to a new file atomically; source=None is never implicit.

    overlay=None copies the base unchanged. For a still base without preview_time,
    a video overlay uses overlay_frame_time and returns that choice explicitly.
    A still preview at video time t obeys [start,end), takes t-start+frame_time,
    and loops only when requested. Overlay audio is intentionally never mixed.
    """
    options = validate_options(x=x, y=y, width=width, height=height, width_percent=width_percent,
                               anchor=anchor, offset_x=offset_x, offset_y=offset_y, opacity=opacity,
                               start=start, end=end, loop=loop, preview_time=preview_time,
                               overlay_frame_time=overlay_frame_time, media_kind=media_kind)
    source, destination = Path(source), Path(destination)
    base = probe_media(source, media_kind)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if overlay is None:
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        return {**base, "path": str(destination), "overlay": {"enabled": False}}
    overlay = Path(overlay)
    material = probe_media(overlay)
    geometry = resolve_geometry((base["width"], base["height"]),
                                (material["width"], material["height"]), **options)
    if material["kind"] == "video" and options["overlay_frame_time"] >= material["duration"] and not options["loop"]:
        raise ValueError("overlay_frame_time is outside the video")
    frame_time = options["overlay_frame_time"]
    if material["kind"] == "video" and options["loop"]:
        frame_time %= material["duration"]
    enabled = options["opacity"] > 0
    sample_time = frame_time
    if base["kind"] == "image" and options["preview_time"] is not None:
        t = options["preview_time"]
        enabled &= t >= options["start"] and (options["end"] is None or t < options["end"])
        if material["kind"] == "video":
            sample_time = max(0, t - options["start"]) + frame_time
            if options["loop"]:
                sample_time %= material["duration"]
            else:
                enabled &= sample_time < material["duration"]
    result_overlay = {"enabled": True, "applied": bool(enabled), "source_kind": material["kind"],
                      **geometry, "options": options, "audio_policy": "base audio unchanged; overlay audio excluded"}
    if base["kind"] == "image" and material["kind"] == "video":
        result_overlay.update(frame_time=sample_time, video_frame_extracted=True,
                              note="Video overlay rendered as one selected frame on a still image")
    with tempfile.TemporaryDirectory(prefix=".media-overlay-", dir=destination.parent) as temporary_dir:
        output = Path(temporary_dir) / ("result" + destination.suffix)
        if base["kind"] == "image":
            if destination.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                raise ValueError("Image output must be PNG, JPEG or WEBP")
            with Image.open(source) as image:
                canvas = ImageOps.exif_transpose(image).convert("RGBA")
            if enabled:
                layer = _image_frame(overlay, material, time=sample_time, temporary_dir=temporary_dir)
                layer = layer.resize((geometry["width"], geometry["height"]), Image.Resampling.LANCZOS)
                layer.putalpha(layer.getchannel("A").point(lambda a: round(a * options["opacity"])))
                canvas.alpha_composite(layer, dest=(geometry["x"], geometry["y"]))
            if destination.suffix.lower() in (".jpg", ".jpeg"):
                # JPEG has no alpha. Flatten only at the final format boundary.
                flattened = Image.new("RGB", canvas.size, "white")
                flattened.paste(canvas, mask=canvas.getchannel("A")); flattened.save(output, quality=95)
            else:
                canvas.save(output, **({"lossless": True} if destination.suffix.lower() == ".webp" else {}))
            check = probe_media(output, "image")
            if (check["width"], check["height"]) != (base["width"], base["height"]):
                raise RuntimeError("Overlay export changed image dimensions")
        else:
            if destination.suffix.lower() not in (".mp4", ".mov", ".mkv"):
                raise ValueError("Video output must be MP4, MOV or MKV")
            signature = _video_signature(source)
            if not signature["timestamps"]:
                raise ValueError("Base video is empty")
            base_start = float(signature["timestamps"][0])
            first = options["start"] + base_start
            last = (options["end"] if options["end"] is not None else base["duration"]) + base_start
            if material["kind"] == "image":
                # Normalize EXIF orientation and retain alpha before FFmpeg reads it.
                layer_path = Path(temporary_dir) / "overlay.png"
                _image_frame(overlay, material, temporary_dir=temporary_dir).save(layer_path)
                input_options = ["-loop", 1, "-framerate", str(Fraction(base["fps"]).limit_denominator(100000)), "-i", layer_path]
                trim = ""
            else:
                input_options = (["-stream_loop", -1] if options["loop"] else []) + _decoder_options(material) + ["-i", overlay]
                trim = f"trim=start={frame_time:.9f},"
                if not options["loop"]:
                    last = min(last, first + material["duration"] - frame_time)
            # All generated filter expressions contain validated numbers only.
            layer_filter = (f"{trim}setpts=PTS-STARTPTS+{first:.9f}/TB,"
                            f"scale={geometry['width']}:{geometry['height']}:flags=lanczos,"
                            f"format=rgba,colorchannelmixer=aa={options['opacity']:.9f}")
            expression = f"gte(t,{first:.9f})*lt(t,{last:.9f})"
            graph = (f"[1:v]{layer_filter}[layer];[0:v][layer]overlay="
                     f"x={geometry['x']}:y={geometry['y']}:format=auto:alpha=straight:"
                     f"eof_action=pass:repeatlast=0:enable='{expression}'[v]")
            pixel_format = "yuv420p" if base["width"] % 2 == base["height"] % 2 == 0 else "yuv444p"
            args = ["-copyts", "-i", source, *input_options, "-filter_complex", graph,
                    "-map", "[v]", "-map", "0:a?", "-map_metadata", 0,
                    "-frames:v", len(signature["timestamps"]), "-fps_mode", "passthrough",
                    "-enc_time_base", str(signature["time_base"]),
                    "-c:v", "libx264", "-pix_fmt", pixel_format, "-crf", 16, "-preset", "medium",
                    "-c:a", "copy"]
            if destination.suffix.lower() in (".mp4", ".mov"):
                args += ["-video_track_timescale", signature["time_base"].denominator, "-movflags", "+faststart"]
            _run_ffmpeg([*args, output])
            check = {"kind": "video", **_verify_video(base, signature, output)}
        os.replace(output, destination)
    return {**check, "path": str(destination), "overlay": result_overlay}
