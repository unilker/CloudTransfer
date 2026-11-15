"""Folder pickers for both sides."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..errors import NotFoundError, ProviderError
from ..providers.base import RemoteItem
from ..providers.gdrive import GoogleDriveSource, is_native
from ..providers.onedrive import OneDriveDest
from .deps import drive_source, onedrive_dest

router = APIRouter(prefix="/api/browse")


def _serialize(item: RemoteItem, *, side: str) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "is_folder": item.is_folder,
        "size": item.size,
        "mime_type": item.mime_type,
        "modified": item.modified,
        "is_shortcut": item.is_shortcut,
        # Native Google formats get converted on the way out; the UI says so.
        "needs_conversion": side == "drive" and is_native(item.mime_type) and not item.is_folder,
        "web_url": item.web_url,
    }


@router.get("/drive")
async def browse_drive(
    parent: str = Query("root", description="Drive folder ID, or 'root' for My Drive"),
    source: GoogleDriveSource = Depends(drive_source),
) -> dict:
    try:
        children = await source.list_children(parent)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Drive folder {parent!r} not found") from exc
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    name = "My Drive"
    if parent != "root":
        try:
            name = (await source.get_item(parent)).name
        except ProviderError:
            name = parent

    folders = [c for c in children if c.is_folder]
    files = [c for c in children if not c.is_folder]
    return {
        "parent": {"id": parent, "name": name},
        "folders": [_serialize(c, side="drive") for c in folders],
        "files": [_serialize(c, side="drive") for c in files],
        "file_count": len(files),
        "folder_count": len(folders),
    }


@router.get("/drive/shared-drives")
async def shared_drives(source: GoogleDriveSource = Depends(drive_source)) -> dict:
    try:
        drives = await source.list_shared_drives()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"drives": [{"id": d.id, "name": d.name} for d in drives]}


@router.get("/onedrive")
async def browse_onedrive(
    parent: str = Query("root", description="OneDrive item ID, or 'root'"),
    dest: OneDriveDest = Depends(onedrive_dest),
) -> dict:
    try:
        if parent == "root":
            root = await dest.get_root()
            parent_id, parent_name = root.id, root.name or "OneDrive"
        else:
            item = await dest.get_item(parent)
            parent_id, parent_name = item.id, item.name
        children = await dest.list_children(parent_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"OneDrive item {parent!r} not found") from exc
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    folders = [c for c in children if c.is_folder]
    files = [c for c in children if not c.is_folder]
    return {
        "parent": {"id": parent_id, "name": parent_name},
        "folders": [_serialize(c, side="onedrive") for c in folders],
        "files": [_serialize(c, side="onedrive") for c in files],
        "file_count": len(files),
        "folder_count": len(folders),
    }


@router.post("/onedrive/folder")
async def create_folder(
    parent: str = Query(..., description="Parent OneDrive folder ID"),
    name: str = Query(..., min_length=1, max_length=255),
    dest: OneDriveDest = Depends(onedrive_dest),
) -> dict:
    """Create a destination folder without leaving the picker."""
    from ..naming import sanitize_name

    safe, reasons = sanitize_name(name, fallback="New folder")
    parent_id = parent
    if parent == "root":
        parent_id = (await dest.get_root()).id
    try:
        folder = await dest.ensure_folder(parent_id, safe)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"folder": _serialize(folder, side="onedrive"), "renamed_because": reasons}


@router.get("/onedrive/quota")
async def quota(dest: OneDriveDest = Depends(onedrive_dest)) -> dict:
    try:
        remaining, total = await dest.get_quota()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"remaining": remaining, "total": total}
