"""Workspace HTTP API; files are streamed to temporary staging before validation."""

import asyncio
from pathlib import Path
import tempfile

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from routers.access import require_video_access
from services import workspaces

router = APIRouter(prefix="/api/studio/workspaces", tags=["创作流程"])


async def _call(function, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except FileNotFoundError:
        raise HTTPException(404, "工作区、任务或素材不存在")
    except workspaces.WorkspaceError as error:
        raise HTTPException(error.status_code, str(error))
    except ValueError:
        raise HTTPException(400, "工作区参数无效")


async def _stage(upload, target, limit):
    size = 0
    with target.open("wb") as output:
        while block := await upload.read(1024**2):
            size += len(block)
            if size > limit:
                raise HTTPException(413, "上传文件超过大小限制")
            output.write(block)
    if not size:
        raise HTTPException(400, "上传文件为空")
    return size


@router.get("")
async def list_workspaces(access=Depends(require_video_access)):
    return {"workspaces": await _call(workspaces.list_owned, access)}


@router.post("", status_code=201)
async def create_workspace(body: dict = Body(...), access=Depends(require_video_access)):
    return await _call(workspaces.create, body.get("name"), access)


@router.post("/import", status_code=201)
async def import_workspace(file: UploadFile = File(...), access=Depends(require_video_access)):
    with tempfile.TemporaryDirectory(prefix="frame-studio-import-") as temporary:
        path = Path(temporary) / "workspace.zip"
        await _stage(file, path, workspaces.MAX_TOTAL_BYTES + workspaces.MAX_MANIFEST_BYTES + 10 * 1024**2)
        return await _call(workspaces.import_zip, path, access)


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: str, access=Depends(require_video_access)):
    data = await _call(workspaces.load_owned, workspace_id, access)
    return workspaces.public(data)


@router.patch("/{workspace_id}")
async def rename_workspace(workspace_id: str, body: dict = Body(...), access=Depends(require_video_access)):
    return await _call(workspaces.rename, workspace_id, body.get("name"), access)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: str, access=Depends(require_video_access)):
    await _call(workspaces.delete, workspace_id, access)
    return Response(status_code=204)


@router.post("/{workspace_id}/assets")
async def upload_assets(workspace_id: str, files: list[UploadFile] = File(...), access=Depends(require_video_access)):
    await _call(workspaces.load_owned, workspace_id, access)
    if not files or len(files) > workspaces.MAX_ASSETS:
        raise HTTPException(400, "素材数量无效")
    with tempfile.TemporaryDirectory(prefix="frame-studio-upload-") as temporary:
        entries, total = [], 0
        for index, upload in enumerate(files):
            path = Path(temporary) / str(index)
            total += await _stage(upload, path, workspaces.MAX_FILE_BYTES)
            if total > workspaces.MAX_TOTAL_BYTES:
                raise HTTPException(413, "上传素材总大小超过限制")
            entries.append((path, upload.filename, None))
        return await _call(workspaces.add_files, workspace_id, entries, access)


@router.get("/{workspace_id}/assets/{asset_id}")
async def get_asset(workspace_id: str, asset_id: str, access=Depends(require_video_access)):
    path = await _call(workspaces.asset_path, workspace_id, asset_id, access)
    return FileResponse(path)


@router.delete("/{workspace_id}/assets/{asset_id}")
async def delete_asset(workspace_id: str, asset_id: str, access=Depends(require_video_access)):
    return await _call(workspaces.delete_asset, workspace_id, asset_id, access)


@router.post("/{workspace_id}/from-task")
async def from_task(workspace_id: str, body: dict = Body(...), access=Depends(require_video_access)):
    if not all(isinstance(body.get(key), str) for key in ("kind", "task_id", "asset_name")):
        raise HTTPException(400, "任务素材参数无效")
    return await _call(workspaces.from_task, workspace_id, body["kind"], body["task_id"], body["asset_name"], access)


@router.post("/{workspace_id}/from-workspace")
async def from_workspace(workspace_id: str, body: dict = Body(...), access=Depends(require_video_access)):
    if not all(isinstance(body.get(key), str) for key in ("source_workspace_id", "asset_id")):
        raise HTTPException(400, "工作区素材参数无效")
    return await _call(workspaces.from_workspace, workspace_id, body["source_workspace_id"], body["asset_id"], access)


@router.put("/{workspace_id}/draft")
async def set_draft(workspace_id: str, body: dict = Body(...), access=Depends(require_video_access)):
    return await _call(workspaces.set_draft, workspace_id, body.get("draft"), access)


@router.post("/{workspace_id}/save")
async def save_workspace(workspace_id: str, body: dict = Body(default={}), access=Depends(require_video_access)):
    if "draft" in body and not isinstance(body["draft"], dict):
        raise HTTPException(400, "草稿需为 JSON 对象")
    return await _call(workspaces.save, workspace_id, access, draft=body.get("draft"), name=body.get("name"))


@router.get("/{workspace_id}/history")
async def history(workspace_id: str, access=Depends(require_video_access)):
    return {"revisions": await _call(workspaces.history, workspace_id, access)}


@router.post("/{workspace_id}/history/{revision_id}/restore")
async def restore(workspace_id: str, revision_id: int, access=Depends(require_video_access)):
    return await _call(workspaces.restore, workspace_id, revision_id, access)


@router.get("/{workspace_id}/export")
async def export(workspace_id: str, access=Depends(require_video_access)):
    path = await _call(workspaces.export_zip, workspace_id, access)
    return FileResponse(path, media_type="application/zip", filename=f"workspace-{workspace_id}.zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))
