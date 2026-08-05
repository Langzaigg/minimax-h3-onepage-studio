import json
import shutil
from pathlib import Path

from config import HISTORY_RETENTION_DAYS, STORAGE_USAGE_THRESHOLD_PERCENT, VIDEO_DATA_FOLDER
from services.video_generation import TASK_ID_RE, cleanup_expired_video_tasks


def cleanup_expired_history(retention_days: int = HISTORY_RETENTION_DAYS) -> dict[str, int]:
    return {"video_tasks": cleanup_expired_video_tasks(retention_days)}


def disk_usage_percent() -> float:
    root = Path(VIDEO_DATA_FOLDER).resolve().anchor or str(Path.cwd().anchor)
    usage = shutil.disk_usage(root)
    return usage.used / usage.total * 100 if usage.total else 0.0


def _emergency_candidates() -> list[tuple[float, Path]]:
    rows: list[tuple[float, Path]] = []
    root = Path(VIDEO_DATA_FOLDER)
    if not root.exists():
        return rows
    for path in root.iterdir():
        if not path.is_dir() or not TASK_ID_RE.fullmatch(path.name):
            continue
        try:
            data = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
            if data.get("status") in {"completed", "failed"}:
                rows.append(((path / "metadata.json").stat().st_mtime, path))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(rows, key=lambda item: item[0])


def emergency_cleanup_if_needed(
    threshold_percent: float = STORAGE_USAGE_THRESHOLD_PERCENT,
) -> dict[str, float | int | bool]:
    before = disk_usage_percent()
    deleted = 0
    if before >= threshold_percent:
        for _, path in _emergency_candidates():
            if disk_usage_percent() < threshold_percent:
                break
            try:
                shutil.rmtree(path)
                deleted += 1
            except OSError as error:
                print(f"Storage cleanup could not delete {path}: {error}")
    after = disk_usage_percent()
    return {"triggered": before >= threshold_percent, "deleted": deleted, "before": before, "after": after}
