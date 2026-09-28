"""Mirrors lib/google-drive-upload.ts. Best-effort archive of a file to a
Google Drive folder, via a small Apps Script web app. Deliberately never
allowed to break the caller's actual flow (scan/grade/save, or PDF
extraction) — Drive is a secondary archive/"Open in Drive" link, so any
failure here is swallowed.

Two independent Apps Script deployments exist: the original one (scanned
answer papers, GOOGLE_DRIVE_UPLOAD_URL/SECRET) and a second, dedicated one
for syllabus PDFs (GOOGLE_DRIVE_SYLLABUS_UPLOAD_URL/SECRET, its own folder
hardcoded on the script side) — kept fully separate on purpose so neither
feature's uploads can ever land in the other's folder. upload_url/secret
let a caller target either; both default to the original scan-upload pair
so existing callers need no changes.

folder_id is optional and passed straight through to the Apps Script as
"folderId" — omitted entirely (not sent as null) when the caller doesn't
have one. Only the original script reads it; the dedicated syllabus script
ignores it since its own folder is fixed."""
import os
from typing import Optional
import httpx


async def upload_to_google_drive(
    file_base64: str, filename: str, mime_type: str = "image/jpeg", folder_id: Optional[str] = None,
    upload_url: Optional[str] = None, secret: Optional[str] = None,
) -> Optional[dict]:
    upload_url = upload_url or os.environ.get("GOOGLE_DRIVE_UPLOAD_URL")
    secret = secret or os.environ.get("GOOGLE_DRIVE_UPLOAD_SECRET")
    if not upload_url or not secret:
        return None

    body = {"secret": secret, "imageBase64": file_base64, "filename": filename, "mimeType": mime_type}
    if folder_id:
        body["folderId"] = folder_id

    try:
        # Apps Script web apps answer via a 302 to script.googleusercontent.com
        # before the real JSON body — httpx does not follow redirects by
        # default (unlike requests), so without this every call here silently
        # returned None even when the upload itself succeeded.
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.post(
                upload_url,
                headers={"Content-Type": "application/json"},
                json=body,
            )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        if not data.get("url") or not data.get("fileId"):
            return None
        return {"url": data["url"], "fileId": data["fileId"]}
    except Exception:
        return None
