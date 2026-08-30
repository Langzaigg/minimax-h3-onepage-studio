from datetime import date, timedelta

from fastapi.testclient import TestClient

import main
import routers.access as access_router
from services import access_control


def configure_access_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(access_control, "KEYS_PATH", tmp_path / "api_keys.json")
    monkeypatch.setattr(access_control, "STATS_PATH", tmp_path / "usage_stats.json")


def test_admin_mode_allows_direct_access_without_credentials(tmp_path, monkeypatch):
    """admin 模式（默认）：无需任何凭据即可直接以管理员身份访问，管理控制台同样可用。"""
    configure_access_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(access_control, "ACCESS_MODE", "admin")
    # register 路由读取的是 config.ACCESS_MODE 的模块级副本，需一并覆盖。
    monkeypatch.setattr(access_router, "ACCESS_MODE", "admin")
    with TestClient(main.app) as client:
        tasks = client.get("/api/video/tasks")
        assert tasks.status_code == 200
        status = client.get("/api/access/status").json()
        assert status["authenticated"] is True
        assert status["role"] == "admin"
        assert client.get("/api/admin/api-keys").status_code == 200
        assert client.get("/api/video/tasks", headers={"X-API-Key": "anything"}).status_code == 200
        # 注册接口在 admin 模式下不可用。
        assert client.post("/api/access/register", json={"username": "someone"}).status_code == 403


def test_video_requires_api_key_and_admin_can_manage_full_keys(tmp_path, monkeypatch):
    configure_access_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(access_control, "ACCESS_MODE", "key")
    with TestClient(main.app) as client:
        assert client.get("/api/video/tasks").status_code == 401
        assert client.post("/api/access/verify", json={"secret": "wrong"}).status_code == 401

        verified = client.post(
            "/api/access/verify", json={"secret": access_control.SERVICE_ADMIN_KEY}
        )
        assert verified.status_code == 200
        assert verified.json()["role"] == "admin"

        created = client.post(
            "/api/admin/api-keys", json={"name": "视频组", "valid_for_hours": 24}
        )
        assert created.status_code == 201
        api_key = created.json()["api_key"]
        key_id = created.json()["id"]
        assert api_key.startswith("dc_")
        assert client.get("/api/admin/api-keys").json()[0]["api_key"] == api_key

        with TestClient(main.app) as api_client:
            response = api_client.get("/api/video/tasks", headers={"X-API-Key": api_key})
            assert response.status_code == 200
            # 分页接口：tasks 之外还返回 total/limit/offset
            assert response.json() == {"tasks": [], "total": 0, "limit": 20, "offset": 0}

            response = api_client.post("/api/access/verify", json={"secret": api_key})
            assert response.status_code == 200
            assert response.json()["role"] == "api_key"
            assert api_client.get("/api/video/tasks").status_code == 200

        revoked = client.delete(f"/api/admin/api-keys/{key_id}")
        assert revoked.status_code == 200
        assert revoked.json()["active"] is False
        with TestClient(main.app) as revoked_client:
            assert revoked_client.get(
                "/api/video/tasks", headers={"X-API-Key": api_key}
            ).status_code == 401


def test_business_usage_is_aggregated_by_week_and_key(tmp_path, monkeypatch):
    configure_access_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(access_control, "ACCESS_MODE", "key")
    with TestClient(main.app) as admin_client:
        created = admin_client.post(
            "/api/admin/api-keys",
            headers={"X-Admin-Key": access_control.SERVICE_ADMIN_KEY},
            json={"name": "统计测试", "valid_for_hours": 24},
        ).json()
        with TestClient(main.app) as api_client:
            for _ in range(2):
                response = api_client.get(
                    "/api/video/tasks", headers={"Authorization": f"Bearer {created['api_key']}"}
                )
                assert response.status_code == 200

        today = date.today()
        response = admin_client.get(
            "/api/admin/stats",
            headers={"X-Admin-Key": access_control.SERVICE_ADMIN_KEY},
            params={
                "start_date": (today - timedelta(days=7)).isoformat(),
                "end_date": today.isoformat(),
                "key_id": created["id"],
            },
        )
        assert response.status_code == 200
        rows = response.json()["rows"]
        matching = [row for row in rows if row["endpoint"] == "GET /api/video/tasks"]
        assert sum(row["count"] for row in matching) == 2


def test_openapi_documents_video_and_admin_auth_headers():
    schema = main.app.openapi()
    video_parameters = schema["paths"]["/api/video/tasks"]["get"]["parameters"]
    assert {item["name"] for item in video_parameters} >= {"X-API-Key", "Authorization"}
    admin_parameters = schema["paths"]["/api/admin/api-keys"]["get"]["parameters"]
    assert {item["name"] for item in admin_parameters} >= {"X-Admin-Key"}
