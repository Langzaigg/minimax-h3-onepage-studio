"""Keep supplied title lettering inside the proportional 10% title-safe box."""
from pathlib import Path
import os

from PIL import Image
from services.post_processing import _run_ffmpeg, probe_video
from services.delivery_profiles import TARGET_RESOLUTIONS


def apply_title(video: Path, title: Path, fade=False) -> dict:
    video, title = Path(video), Path(title)
    info = probe_video(video)
    if (info["width"], info["height"]) not in TARGET_RESOLUTIONS.values():
        raise ValueError("Title overlay requires a finished 720p, 1080p or 2560×1440 video")
    factor = info["width"] / 1920
    with Image.open(title) as raw:
        if raw.width < 1 or raw.height < 1:
            raise ValueError("Empty title image")
        scale = min(600*factor / raw.width, 280*factor / raw.height, 1.25*factor)
        width = max(2, int(raw.width * scale) // 2 * 2)
        height = max(2, int(raw.height * scale) // 2 * 2)
    x, y = round(220*factor), round(620*factor)
    safe = [info["width"]//10, info["height"]//10, info["width"]*9//10, info["height"]*9//10]
    bounds = [x, y, x + width, y + height]
    if not (safe[0] <= bounds[0] < bounds[2] <= safe[2] and safe[1] <= bounds[1] < bounds[3] <= safe[3]):
        raise ValueError("Title image exceeds title-safe area")
    temporary = video.with_name(video.stem + ".title.mp4")
    title_filter = f"scale={width}:{height}:flags=lanczos,format=rgba"
    if fade:
        title_filter += ",fade=t=in:st=1.2:d=0.9:alpha=1"
    graph = f"[1:v]{title_filter}[title];[0:v][title]overlay={x}:{y}:format=auto,format=yuv420p[v]"
    _run_ffmpeg(["-i", video, "-loop", "1", "-framerate", str(info["fps"]), "-i", title,
                 "-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-t", info["duration"],
                 "-frames:v", info["frames"], "-fps_mode", "passthrough", "-c:v", "libx264", "-crf", "16", "-preset", "medium",
                 "-c:a", "copy", "-movflags", "+faststart", temporary])
    check = probe_video(temporary)
    if any(check[key] != info[key] for key in ("width", "height", "frames", "has_audio")) or abs(check["fps"]-info["fps"]) > .001 or abs(check["duration"]-info["duration"]) > 1/info["fps"]:
        raise RuntimeError("Title export changed video timing")
    os.replace(temporary, video)
    return {"title_bounds": bounds, "title_safe_bounds": safe, "title_fade": bool(fade)}
