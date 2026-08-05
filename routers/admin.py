import asyncio
from threading import Lock
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from models.admin import ApiKeyCreateRequest, ApiKeyRecord, UsageStatsResponse
from routers.access import require_admin
from services import access_control
from services.service_update import (
    ServiceUpdateError,
    schedule_service_restart,
    update_repository,
)


router = APIRouter(prefix="/api/admin", tags=["系统运维"], dependencies=[Depends(require_admin)])
update_lock = Lock()


@router.get("/api-keys", response_model=list[ApiKeyRecord], summary="查看全部 API Key（含完整密钥）")
async def get_api_keys():
    return access_control.list_api_keys()


@router.post("/api-keys", response_model=ApiKeyRecord, status_code=201, summary="创建 API Key")
async def post_api_key(payload: ApiKeyCreateRequest):
    return access_control.create_api_key(payload.name, payload.valid_for_hours)


@router.delete("/api-keys/{key_id}", response_model=ApiKeyRecord, summary="撤销 API Key")
async def delete_api_key(key_id: str):
    try:
        return access_control.revoke_api_key(key_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="API Key 不存在")


@router.get("/stats", response_model=UsageStatsResponse, summary="查询按周聚合的 API 调用统计")
async def get_usage_stats(
    start_date: date = Query(default_factory=lambda: date.today() - timedelta(days=84)),
    end_date: date = Query(default_factory=date.today),
    key_id: str | None = Query(None),
):
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
    if (end_date - start_date).days > 730:
        raise HTTPException(status_code=400, detail="单次统计范围不能超过两年")
    return access_control.usage_stats(start_date, end_date, key_id)


@router.post("/update-and-restart", summary="拉取 Git 更新并重启服务")
async def update_and_restart(
):
    if not update_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="服务更新正在执行，请勿重复提交")

    restart_scheduled = False
    try:
        update_result = await asyncio.to_thread(update_repository)
        restart_result = schedule_service_restart()
        restart_scheduled = True
        return {
            "message": "Git 拉取成功，服务即将重启",
            "git": update_result,
            "restart": restart_result,
        }
    except ServiceUpdateError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        if not restart_scheduled:
            update_lock.release()
