import argparse
import json
import shutil
from pathlib import Path


def export_task(source: Path, destination: Path) -> None:
    metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
    destination.mkdir(parents=True, exist_ok=True)
    for folder_name in ("references", "outputs"):
        source_folder = source / folder_name
        if source_folder.is_dir():
            shutil.copytree(source_folder, destination / folder_name, dirs_exist_ok=True)

    def normalize_newlines(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")

    prompt = normalize_newlines(str(metadata.get("prompt") or ""))
    original_prompt = normalize_newlines(str(metadata.get("original_prompt") or prompt))
    (destination / "prompt.txt").write_text(prompt, encoding="utf-8")
    (destination / "original_prompt.txt").write_text(original_prompt, encoding="utf-8")

    references = []
    for item in metadata.get("_reference_assets", []):
        suffix = Path(item["path"]).suffix
        references.append(
            {
                "kind": item["kind"],
                "index": item["index"],
                "label": f"<Picture {item['index']}>" if item["kind"] == "image" else None,
                "original_filename": item["filename"],
                "file": f"references/{item['asset_name']}{suffix}",
            }
        )
    request = {
        "endpoint": "POST /api/video/reference-tasks",
        "prompt_file": "prompt.txt",
        "duration": metadata["duration"],
        "resolution_megapixels": metadata["resolution_megapixels"],
        "aspect_ratio": metadata["aspect_ratio"],
        "seed": metadata["seed"],
        "polish_prompt_first": False,
        "source_prompt_was_polished": bool(metadata.get("polished_prompt")),
        "ref_image_size": metadata.get("ref_image_size", "match"),
        "use_video_audio": metadata.get("use_video_audio", True),
        "references": references,
    }
    (destination / "request.json").write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    video_name = Path(metadata["_assets"]["video"]).name
    result = {
        key: value
        for key, value in metadata.items()
        if not key.startswith("_") and key not in {"prompt", "original_prompt", "polished_prompt"}
    }
    result.update(
        {
            "prompt_file": "prompt.txt",
            "original_prompt_file": "original_prompt.txt",
            "assets": {"video": f"outputs/{video_name}"},
            "references": references,
        }
    )
    (destination / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="把本地视频任务导出为不含绝对路径的项目示例")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    export_task(args.source.resolve(), args.destination.resolve())


if __name__ == "__main__":
    main()
