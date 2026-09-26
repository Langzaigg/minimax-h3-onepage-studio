from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import stat
import struct
import wave
import zipfile

from fastapi.testclient import TestClient
from PIL import Image
import pytest

import main
from routers.access import require_video_access
from services import studio_tasks, video_generation, workspaces

BASE = "/api/studio/workspaces"
ALICE = {"role": "user", "key_id": "alice"}


@pytest.fixture
def client():
    main.app.dependency_overrides[require_video_access] = lambda: ALICE
    with TestClient(main.app) as value:
        yield value


def png(color="blue"):
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, "PNG")
    return output.getvalue()


def create(client, name="测试工作区"):
    result = client.post(BASE, json={"name": name})
    assert result.status_code == 201, result.text
    return result.json()


def upload(client, workspace, contents=None):
    files = contents or [("one.png", png()), ("two.png", png("red"))]
    result = client.post(f"{BASE}/{workspace['id']}/assets", files=[
        ("files", (name, content, "application/octet-stream")) for name, content in files])
    assert result.status_code == 200, result.text
    return result.json()


def archive_files(payload):
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        return {item.filename: archive.read(item) for item in archive.infolist()}


def archive_bytes(files, *, extra=None, compression=zipfile.ZIP_STORED):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        if extra:
            archive.writestr(*extra)
    return output.getvalue()


def import_zip(client, payload):
    return client.post(f"{BASE}/import", files={"file": ("workspace.zip", payload, "application/zip")})


def test_upload_order_dedup_private_paths_and_draft_history(client):
    workspace = create(client)
    assert workspace["revision"] == 1
    workspace = upload(client, workspace, [("../one.png", png()), ("C:\\fake\\two.png", png())])
    assets = workspace["assets"]
    assert workspace["added_asset_ids"] == [item["id"] for item in assets]
    assert [item["name"] for item in assets] == ["one.png", "two.png"]
    assert assets[0]["width"] == 32 and assets[0]["height"] == 24
    assert len(list((workspaces.ROOT / workspace["id"] / "blobs").iterdir())) == 1
    assert "_blob" not in json.dumps(workspace) and str(workspaces.ROOT) not in json.dumps(workspace)
    for asset in assets:
        assert client.get(asset["url"]).content == png()
    base = f"{BASE}/{workspace['id']}"
    draft = {"schema_version": 1, "fields": {"seed": "9223372036854775807"}, "inputs": {"images": [assets[0]["id"]]}}
    updated = client.put(base + "/draft", json={"draft": draft}).json()
    assert updated["draft"] == draft and updated["revision"] == workspace["revision"]
    saved = client.post(base + "/save", json={"name": "完整版本", "draft": draft}).json()
    assert saved["revision"] == workspace["revision"] + 1
    deleted = client.delete(assets[0]["url"]).json()
    assert len(deleted["assets"]) == 1
    assert client.get(assets[0]["url"]).status_code == 404
    restored = client.post(base + f"/history/{saved['revision']}/restore").json()
    assert restored["revision"] == deleted["revision"] + 1 and restored["draft"] == draft
    assert len(restored["assets"]) == 2 and client.get(assets[0]["url"]).content == png()
    history = client.get(base + "/history").json()["revisions"]
    assert history[0]["id"] == restored["revision"]
    assert history[-1]["asset_count"] == 0


def test_zip_roundtrip_preserves_deleted_media_and_versions_and_new_owner(client):
    workspace = upload(client, create(client))
    base = f"{BASE}/{workspace['id']}"
    draft = {"schema_version": 1, "inputs": {"images": [workspace["assets"][0]["id"]]}}
    saved = client.post(base + "/save", json={"draft": draft}).json()
    client.delete(workspace["assets"][0]["url"])
    # Unsaved draft must roundtrip too, independently of the last checkpoint.
    client.put(base + "/draft", json={"draft": {"schema_version": 1, "changed": True}})
    exported = client.get(base + "/export")
    assert exported.status_code == 200
    files = archive_files(exported.content)
    assert len(files) == 3
    manifest = json.loads(files["workspace.json"])
    assert "_owner" not in manifest["current"]
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "bob"}
    response = import_zip(client, exported.content)
    assert response.status_code == 201, response.text
    imported = response.json()
    assert imported["id"] != workspace["id"] and len(imported["assets"]) == 1
    assert imported["draft"]["changed"] is True
    assert workspaces.load_owned(imported["id"], {"role": "user", "key_id": "bob"})["_owner"] == "bob"
    restored = client.post(f"{BASE}/{imported['id']}/history/{saved['revision']}/restore").json()
    assert restored["draft"] == draft and len(restored["assets"]) == 2
    assert [client.get(asset["url"]).content for asset in restored["assets"]] == [png(), png("red")]
    assert client.get(workspace["assets"][1]["url"]).status_code == 404


def test_repeated_save_reuses_current_asset_ids_and_counts_only_new_items(client, monkeypatch):
    workspace = create(client)
    # Same logical material twice in one request produces ordered repeated IDs.
    workspace = upload(client, workspace, [("one.png", png()), ("one.png", png())])
    asset_id = workspace["assets"][0]["id"]
    assert workspace["added_asset_ids"] == [asset_id, asset_id]
    assert len(workspace["assets"]) == 1
    monkeypatch.setattr(workspaces, "MAX_ASSETS", 1)
    for _ in range(3):
        workspace = upload(client, workspace, [("one.png", png())])
        assert workspace["added_asset_ids"] == [asset_id] and len(workspace["assets"]) == 1
        client.post(f"{BASE}/{workspace['id']}/save", json={"draft": {"inputs": {"images": [asset_id]}}})
    assert len(list((workspaces.ROOT / workspace["id"] / "blobs").iterdir())) == 1
    assert client.delete(workspace["assets"][0]["url"]).status_code == 200
    added = upload(client, workspace, [("one.png", png())])
    assert added["added_asset_ids"] != [asset_id]
    # A workspace exactly at the revision cap must still export/import itself.
    monkeypatch.setattr(workspaces, "MAX_REVISIONS", added["revision"])
    exported = client.get(f"{BASE}/{workspace['id']}/export")
    assert exported.status_code == 200
    assert import_zip(client, exported.content).status_code == 201


def test_owner_checks_all_mutations_assets_export_restore_and_admin(client):
    workspace = upload(client, create(client))
    base = f"{BASE}/{workspace['id']}"
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "user", "key_id": "bob"}
    assert client.get(BASE).json() == {"workspaces": []}
    for suffix in ("", "/export", "/history", "/assets/" + workspace["assets"][0]["id"]):
        assert client.get(base + suffix).status_code == 404
    for method, suffix, body in (("patch", "", {"name": "hijack"}), ("put", "/draft", {"draft": {}}),
                                ("post", "/save", {}), ("post", "/history/1/restore", {})):
        assert getattr(client, method)(base + suffix, json=body).status_code == 404
    assert client.delete(base).status_code == 404
    assert client.delete(workspace["assets"][0]["url"]).status_code == 404
    assert client.post(base + "/assets", files={"files": ("test.png", png())}).status_code == 404
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "admin", "key_id": "admin"}
    assert len(client.get(BASE).json()["workspaces"]) == 1
    assert client.get(workspace["assets"][0]["url"]).status_code == 200
    assert client.delete(base).status_code == 204
    assert not (workspaces.ROOT / workspace["id"]).exists()


def test_from_tasks_and_cross_workspace_copy_survive_original_deletion(client):
    workspace = create(client)
    task = studio_tasks.create("image", "alice", prompt="Keep the character")
    source = studio_tasks.directory(task["id"]) / "reference.png"
    source.write_bytes(png())
    studio_tasks.update(task["id"], _inputs=[str(source)], _assets={"image": str(source)}, status="completed")
    metadata = studio_tasks.directory(task["id"]) / "metadata.json"
    original = metadata.read_bytes()
    body = {"kind": "image", "task_id": task["id"], "asset_name": "input_image_0"}
    response = client.post(f"{BASE}/{workspace['id']}/from-task", json=body)
    assert response.status_code == 200, response.text
    asset = response.json()["assets"][0]
    assert asset["source"] == body and response.json()["added_asset_ids"] == [asset["id"]]
    assert metadata.read_bytes() == original
    second = create(client, "第二库")
    response = client.post(f"{BASE}/{second['id']}/from-workspace", json={"source_workspace_id": workspace["id"], "asset_id": asset["id"]})
    assert response.status_code == 200
    copied = response.json()["assets"][0]
    assert copied["source"] == {"workspace_id": workspace["id"], "asset_id": asset["id"]}
    assert client.delete(f"{BASE}/{workspace['id']}").status_code == 204
    assert source.read_bytes() == png() and metadata.read_bytes() == original
    source.unlink()
    assert client.get(copied["url"]).content == png()
    assert client.post(f"{BASE}/{second['id']}/from-task", json=body).status_code == 404


def test_source_task_and_workspace_authorization_cannot_be_bypassed(client):
    workspace = create(client)
    private = studio_tasks.create("image", "bob", prompt="Private")
    source = studio_tasks.directory(private["id"]) / "image.png"
    source.write_bytes(png())
    studio_tasks.update(private["id"], _assets={"image": str(source)})
    body = {"kind": "image", "task_id": private["id"], "asset_name": "image"}
    assert client.post(f"{BASE}/{workspace['id']}/from-task", json=body).status_code == 404
    other = workspaces.create("Private", {"role": "user", "key_id": "bob"})
    copied = workspaces.add_files(other["id"], [(source, "image.png", None)], {"role": "user", "key_id": "bob"})
    body = {"source_workspace_id": other["id"], "asset_id": copied["assets"][0]["id"]}
    assert client.post(f"{BASE}/{workspace['id']}/from-workspace", json=body).status_code == 404
    # Shared H3 asset resolver also checks the task owner before copying.
    video = video_generation.create_task_record(mode="first_last_frame_to_video", prompt="Private", duration=8,
        width=512, height=288, resolution_megapixels=.15, aspect_ratio="16:9", seed=42, warnings=[], owner="bob")
    video_generation.save_input_asset(video["id"], "first_frame", "image.png", png())
    body = {"kind": "video", "task_id": video["id"], "asset_name": "first_frame"}
    assert client.post(f"{BASE}/{workspace['id']}/from-task", json=body).status_code == 404
    main.app.dependency_overrides[require_video_access] = lambda: {"role": "admin", "key_id": "admin"}
    assert client.post(f"{BASE}/{workspace['id']}/from-task", json=body).status_code == 200


def test_asset_resolution_rejects_corrupt_paths_and_symlink_escape(client, tmp_path):
    workspace = upload(client, create(client))
    data = workspaces.load_owned(workspace["id"], ALICE)
    manifest = workspaces.ROOT / workspace["id"] / "workspace.json"
    data["assets"][0]["_blob"] = "../../private.png"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert client.get(workspace["assets"][0]["url"]).status_code == 404
    second = data["assets"][1]
    blob = workspaces.ROOT / workspace["id"] / "blobs" / second["_blob"]
    outside = tmp_path / "private.png"
    outside.write_bytes(png("green"))
    blob.unlink()
    try:
        blob.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert client.get(workspace["assets"][1]["url"]).status_code == 404


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.png", "C:/absolute.png", "blobs\\evil.png", "blobs/evil:stream", "blobs/../escape", "WORKSPACE.JSON"])
def test_import_rejects_unlisted_paths_and_case_collisions(client, name):
    workspace = upload(client, create(client))
    files = archive_files(client.get(f"{BASE}/{workspace['id']}/export").content)
    before = set(workspaces.ROOT.iterdir())
    response = import_zip(client, archive_bytes(files, extra=(name, b"evil")))
    assert response.status_code == 400, response.text
    assert set(workspaces.ROOT.iterdir()) == before


def test_import_rejects_symlinks_duplicates_bombs_hash_and_missing_blobs(client, monkeypatch):
    workspace = upload(client, create(client))
    files = archive_files(client.get(f"{BASE}/{workspace['id']}/export").content)
    blob = next(name for name in files if name.startswith("blobs/"))
    symlink = zipfile.ZipInfo(blob)
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    remaining = {name: value for name, value in files.items() if name != blob}
    assert import_zip(client, archive_bytes(remaining, extra=(symlink, b"../../private"))).status_code == 400
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate = archive_bytes(files, extra=(blob, files[blob]))
    assert import_zip(client, duplicate).status_code == 400
    assert import_zip(client, archive_bytes(remaining)).status_code == 400
    modified = dict(files)
    modified[blob] = png("green")
    assert import_zip(client, archive_bytes(modified)).status_code == 400
    bomb = dict(files)
    bomb[blob] = b"x" * (2 * 1024**2)
    assert import_zip(client, archive_bytes(bomb, compression=zipfile.ZIP_DEFLATED)).status_code == 413
    monkeypatch.setattr(workspaces, "MAX_FILE_BYTES", 10)
    assert import_zip(client, archive_bytes(files)).status_code == 413


def test_import_bounds_central_directory_before_zipfile_allocation(client, monkeypatch):
    workspace = create(client)
    original = client.get(f"{BASE}/{workspace['id']}/export").content
    payload = bytearray(original)
    offset = payload.rfind(b"PK\x05\x06")
    # A malicious end record must be rejected before ZipFile reads its directory.
    struct.pack_into("<HH", payload, offset + 8, 60000, 60000)
    original_zip = workspaces.zipfile.ZipFile
    def guarded(*args, **kwargs):
        pytest.fail("Oversized directory reached ZipFile parser")
    monkeypatch.setattr(workspaces.zipfile, "ZipFile", guarded)
    assert import_zip(client, bytes(payload)).status_code == 413
    monkeypatch.setattr(workspaces.zipfile, "ZipFile", original_zip)


def test_import_accepts_zip64_directory_for_large_export_compatibility(client):
    workspace = create(client)
    payload = client.get(f"{BASE}/{workspace['id']}/export").content
    offset = payload.rfind(b"PK\x05\x06")
    eocd = struct.unpack("<4s4H2LH", payload[offset:offset + 22])
    count, length, position = eocd[4:7]
    zip64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, length, position)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, offset, 1)
    compatible = payload[:offset] + zip64 + locator + payload[offset:]
    result = import_zip(client, compatible)
    assert result.status_code == 201, result.text


def test_import_rebuilds_urls_owner_media_metadata_and_rejects_bad_ids(client):
    workspace = upload(client, create(client))
    files = archive_files(client.get(f"{BASE}/{workspace['id']}/export").content)
    manifest = json.loads(files["workspace.json"])
    manifest["current"].update(_owner="admin", id="x", url="file:///private", path="C:/private")
    for snapshot in [manifest["current"], *manifest["revisions"]]:
        for asset in snapshot["assets"]:
            asset.update(url="file:///private", width=99999, _path="C:/private", source={"task_id": "private"})
    files["workspace.json"] = json.dumps(manifest).encode()
    result = import_zip(client, archive_bytes(files))
    assert result.status_code == 201, result.text
    data = result.json()
    assert data["assets"][0]["width"] == 32
    assert "file:///" not in result.text and "C:/private" not in result.text
    assert workspaces.load_owned(data["id"], ALICE)["_owner"] == "alice"
    manifest["current"]["assets"][0]["id"] = "../malicious"
    files["workspace.json"] = json.dumps(manifest).encode()
    assert import_zip(client, archive_bytes(files)).status_code == 400


def test_media_validation_limits_and_audio(client, monkeypatch):
    workspace = create(client)
    base = f"{BASE}/{workspace['id']}"
    assert client.post(base + "/assets", files={"files": ("fake.png", b"not image")}).status_code == 400
    assert client.post(base + "/assets", files={"files": ("fake.mp4", b"#EXTM3U\nhttp://localhost/private")}).status_code == 400
    assert client.post(base + "/assets", files={"files": ("script.html", b"<html></html>")}).status_code == 400
    audio = BytesIO()
    with wave.open(audio, "wb") as file:
        file.setparams((1, 2, 8000, 800, "NONE", "not compressed"))
        file.writeframes(b"\0\0" * 800)
    result = upload(client, workspace, [("sound.wav", audio.getvalue())])
    assert result["assets"][0]["kind"] == "audio" and result["assets"][0]["duration"] == pytest.approx(.1)
    monkeypatch.setattr(workspaces, "MAX_FILE_BYTES", 10)
    assert client.post(base + "/assets", files={"files": ("oversize.png", png())}).status_code == 413
    assert client.get(base).json()["revision"] == result["revision"]
    assert client.put(base + "/draft", json={"draft": {"huge": "x" * workspaces.MAX_DRAFT_BYTES}}).status_code == 400


def test_history_media_budget_keeps_local_workspaces_roundtrippable(client, monkeypatch):
    monkeypatch.setattr(workspaces, "MAX_ASSETS", 1)
    workspace = create(client)
    base = f"{BASE}/{workspace['id']}"
    for index in range(10):
        workspace = upload(client, workspace, [("image.png", png((index * 20, 100, 150)))])
        assert client.delete(workspace["assets"][0]["url"]).status_code == 200
    response = client.post(base + "/assets", files={"files": ("image.png", png("yellow"))})
    assert response.status_code == 413
    assert len(list((workspaces.ROOT / workspace["id"] / "blobs").iterdir())) == 10
    exported = client.get(base + "/export")
    assert exported.status_code == 200
    assert import_zip(client, exported.content).status_code == 201


def test_metadata_budget_rejects_new_checkpoint_atomically(client, monkeypatch):
    workspace = create(client)
    base = f"{BASE}/{workspace['id']}"
    folder = workspaces.ROOT / workspace["id"]
    original = (folder / "workspace.json").read_bytes()
    monkeypatch.setattr(workspaces, "MAX_MANIFEST_BYTES", 1500)
    assert client.post(base + "/save", json={"draft": {"text": "x" * 1000}}).status_code == 413
    assert client.put(base + "/draft", json={"draft": {"text": "x" * 1000}}).status_code == 413
    assert (folder / "workspace.json").read_bytes() == original
    assert len(list((folder / "revisions").iterdir())) == 1
