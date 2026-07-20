"""Mirrors lib/google-drive-upload.ts. Best-effort archive of a scanned paper
to a school's Google Drive folder, via a small Apps Script web app. Deliberately
never allowed to break the actual scan/grade/save flow — the Supabase Storage
copy is the one thing the in-app "View Paper" viewer relies on. Drive is a
secondary, optional "Open in Drive" link, so any failure here is swallowed."""
import os
from typing import Optional
import httpx


async def upload_to_google_drive(image_base64: str, filename: str, mime_type: str = "image/jpeg") -> Optional[dict]:
    upload_url = os.environ.get("GOOGLE_DRIVE_UPLOAD_URL")
    secret = os.environ.get("GOOGLE_DRIVE_UPLOAD_SECRET")
    if not upload_url or not secret:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                upload_url,
                headers={"Content-Type": "application/json"},
                json={"secret": secret, "imageBase64": image_base64, "filename": filename, "mimeType": mime_type},
            )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        if not data.get("url") or not data.get("fileId"):
            return None
        return {"url": data["url"], "fileId": data["fileId"]}
    except Exception:
        return None
