import json
import logging
import os
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from core.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]


def extract_file_id(file_id_or_url: str) -> str:
    """Extract the file ID from a Google Drive URL or return as-is if already an ID."""
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", file_id_or_url)
    if match:
        extracted = match.group(1)
        logger.info(f"Extracted file ID from URL: {extracted}")
        return extracted
    return file_id_or_url.strip()


def _get_drive_service():
    """Build Drive API client using OAuth2 token.json."""
    token_path = settings.google_drive_token_json

    if not os.path.exists(token_path):
        raise RuntimeError(
            f"Token file not found: {token_path}. "
            f"Run 'python scripts/auth_drive.py' first to authorize."
        )

    with open(token_path) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data["refresh_token"],
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=SCOPES,
    )

    # Refresh if expired
    if creds.expired or not creds.valid:
        creds.refresh(Request())
        # Save refreshed token back
        token_data["token"] = creds.token
        with open(token_path, "w") as f:
            json.dump(token_data, f, indent=2)
        logger.info("OAuth2 token refreshed")

    return build("drive", "v3", credentials=creds)


def download_file(file_id: str, destination_path: str) -> str:
    """Download a file from Google Drive by its file ID or URL."""
    file_id = extract_file_id(file_id)
    service = _get_drive_service()

    file_metadata = service.files().get(
        fileId=file_id, fields="name,mimeType,size",
        supportsAllDrives=True,
    ).execute()
    logger.info(
        f"Downloading Drive file: {file_metadata.get('name')} "
        f"({file_metadata.get('size', 'unknown')} bytes)"
    )

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(destination_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                logger.info(f"Download progress: {int(status.progress() * 100)}%")

    logger.info(f"Download complete: {destination_path}")
    return destination_path


def upload_file(
    file_path: str,
    file_name: str,
    folder_id: str | None = None,
    mime_type: str = "video/mp4",
) -> dict:
    """Upload a file to Google Drive. Returns dict with 'id', 'name', 'webViewLink'."""
    service = _get_drive_service()

    file_metadata: dict = {"name": file_name}
    if folder_id:
        file_metadata["parents"] = [folder_id]

    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()

    # Make the file accessible via link
    try:
        service.permissions().create(
            fileId=file["id"],
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        logger.warning(f"Could not set public permission: {e}")

    logger.info(f"Uploaded to Drive: {file['name']} (ID: {file['id']})")
    return file
