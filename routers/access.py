from datetime import datetime

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response

from config import ACCESS_SESSION_MAX_AGE_SECONDS
from models.admin import AccessVerifyRequest
from services import access_control


router = APIRouter(prefix="/api/access", tags=["访问验证"])


def current_access(request: Request, touch: bool = False) -> dict | None:
    return access_control.identify_access(request.headers, request.cookies, touch=touch)


def require_video_access(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key", description="管理员创建的 API Key")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization", description="也支持 Bearer API Key")] = None,
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key", description="管理员可直接访问")] = None,
) -> dict:
    access = current_access(request, touch=True)
    if not access or access["role"] not in {"api_key", "admin"}:
        raise HTTPException(status_code=401, detail="AI 视频功能需要有效的 API Key")
    return access


def require_admin(
    request: Request,
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key", description="管理员密钥")] = None,
) -> dict:
    access = current_access(request)
    if not access or access["role"] != "admin":
        raise HTTPException(status_code=401, detail="管理员密钥无效或会话已过期")
    return access


@router.post("/verify", summary="验证管理员密钥或 API Key")
async def verify_access(payload: AccessVerifyRequest, response: Response):
    if access_control.is_admin_secret(payload.secret):
        access = {"role": "admin", "key_id": access_control.ADMIN_KEY_ID, "name": "管理员"}
        session = access_control.create_session("admin")
    else:
        row = access_control.verify_api_key(payload.secret)
        if not row:
            raise HTTPException(status_code=401, detail="管理员密钥或 API Key 无效、已撤销或已过期")
        expires_at = datetime.fromisoformat(row["expires_at"])
        access = {"role": "api_key", "key_id": row["id"], "name": row["name"], "expires_at": row["expires_at"]}
        session = access_control.create_session("api_key", row["id"], expires_at)
    response.set_cookie(
        access_control.COOKIE_NAME,
        session,
        max_age=ACCESS_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"authenticated": True, **access}


@router.get("/status", summary="查询当前浏览器访问状态")
async def access_status(request: Request):
    access = current_access(request)
    return {"authenticated": bool(access), **(access or {})}


@router.delete("/session", status_code=204, summary="清除当前浏览器访问会话")
async def clear_access_session(response: Response):
    response.delete_cookie(access_control.COOKIE_NAME, path="/")
    response.status_code = 204
    return response
