"""Google Workspace operations through OAuth, using the user's stored token."""

from __future__ import annotations

import base64
import mimetypes
import os
from email.message import EmailMessage
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, integration_secret, load_spec, require_module, resolve_path

discovery = require_module("googleapiclient.discovery", "google-api-python-client")
Credentials = require_module("google.oauth2.credentials", "google-auth")
Request = require_module("google.auth.transport.requests", "google-auth")
Flow = require_module("google_auth_oauthlib.flow", "google-auth-oauthlib")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/chat.messages",
]


def _paths(spec: dict) -> tuple[Path, Path]:
    data_dir = resolve_path(spec.get("data_dir") or Path.home() / ".winbrow")
    credentials = resolve_path(
        spec.get("credentials_path")
        or integration_secret("google", "credentials_path")
        or os.environ.get("GOOGLE_OAUTH_CLIENT")
        or data_dir / "google_credentials.json"
    )
    token = resolve_path(
        spec.get("token_path")
        or integration_secret("google", "token_path")
        or os.environ.get("GOOGLE_OAUTH_TOKEN")
        or data_dir / "google_token.json"
    )
    return credentials, token


def _credentials(spec: dict):
    credentials_path, token_path = _paths(spec)
    if token_path.is_file():
        creds = Credentials.Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request.Request())
        if creds.valid:
            return creds
    if not credentials_path.is_file():
        fail(
            "Google credentials are missing. Add your OAuth client JSON in the "
            f"Integrations panel. Expected: {credentials_path}"
        )
    return None


def auth(spec: dict) -> None:
    credentials_path, token_path = _paths(spec)
    if not credentials_path.is_file():
        fail(f"OAuth client JSON not found: {credentials_path}")
    flow = Flow.InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=int(spec.get("port") or 0), open_browser=True)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    emit(True, f"Google authorization saved to {token_path}")


def _service(spec: dict, name: str, version: str):
    creds = _credentials(spec)
    if creds is None:
        fail("Google authorization is required. Use the Connect button in Integrations.")
    return discovery.build(name, version, credentials=creds, cache_discovery=False)


def gmail(spec: dict) -> None:
    service = _service(spec, "gmail", "v1")
    operation = str(spec.get("gmail_operation") or "list").lower()
    if operation == "list":
        result = service.users().messages().list(
            userId="me", q=str(spec.get("query") or ""), maxResults=int(spec.get("limit") or 10),
        ).execute()
        emit(True, "Gmail messages listed.", data=result)
    elif operation == "send":
        message = EmailMessage()
        message["To"] = str(spec.get("to") or "")
        message["Subject"] = str(spec.get("subject") or "")
        message.set_content(str(spec.get("body") or ""))
        if not message["To"] or not message["Subject"]:
            fail("to and subject are required to send mail.")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        emit(True, "Gmail message sent.", data=result)
    else:
        fail(f"Unknown gmail operation '{operation}'. Use list or send.")


def calendar(spec: dict) -> None:
    service = _service(spec, "calendar", "v3")
    operation = str(spec.get("calendar_operation") or "list").lower()
    if operation == "list":
        kwargs = {
            "calendarId": str(spec.get("calendar_id") or "primary"),
            "maxResults": int(spec.get("limit") or 10),
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if spec.get("time_min"):
            kwargs["timeMin"] = str(spec["time_min"])
        result = service.events().list(**kwargs).execute()
        emit(True, "Calendar events listed.", data=result)
    elif operation == "create":
        event = spec.get("event") or {}
        if not event.get("summary") or not event.get("start") or not event.get("end"):
            fail("event.summary, event.start, and event.end are required.")
        result = service.events().insert(
            calendarId=str(spec.get("calendar_id") or "primary"), body=event,
        ).execute()
        emit(True, "Calendar event created.", data=result)
    else:
        fail(f"Unknown calendar operation '{operation}'. Use list or create.")


def drive(spec: dict) -> None:
    service = _service(spec, "drive", "v3")
    operation = str(spec.get("drive_operation") or "search").lower()
    if operation == "search":
        query = str(spec.get("query") or "trashed = false")
        result = service.files().list(
            q=query, pageSize=int(spec.get("limit") or 20),
            fields="files(id,name,mimeType,modifiedTime,webViewLink)",
        ).execute()
        emit(True, "Drive files listed.", data=result)
    elif operation == "upload":
        path = resolve_path(spec.get("path") or "")
        if not path.is_file():
            fail(f"File not found: {path}")
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(path), mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        body = {"name": str(spec.get("name") or path.name)}
        if spec.get("folder_id"):
            body["parents"] = [str(spec["folder_id"])]
        result = service.files().create(body=body, media_body=media, fields="id,name,webViewLink").execute()
        emit(True, "Uploaded to Drive.", data=result)
    elif operation == "download":
        file_id = str(spec.get("file_id") or "")
        if not file_id:
            fail("file_id is required.")
        output = resolve_path(spec.get("output_path") or "")
        output.parent.mkdir(parents=True, exist_ok=True)
        from googleapiclient.http import MediaIoBaseDownload
        with output.open("wb") as handle:
            request = service.files().get_media(fileId=file_id)
            downloader = MediaIoBaseDownload(handle, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        emit(True, f"Downloaded to {output}", path=output)
    else:
        fail(f"Unknown drive operation '{operation}'. Use search, upload, or download.")


def sheets(spec: dict) -> None:
    service = _service(spec, "sheets", "v4")
    spreadsheet = str(spec.get("spreadsheet_id") or "")
    if not spreadsheet:
        fail("spreadsheet_id is required.")
    operation = str(spec.get("sheets_operation") or "read").lower()
    range_name = str(spec.get("range") or "Sheet1")
    if operation == "read":
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet, range=range_name,
        ).execute()
        emit(True, "Sheet values read.", data=result)
    elif operation in ("write", "append"):
        values = spec.get("values") or []
        if not isinstance(values, list):
            fail("values must be a list of rows.")
        body = {"values": values}
        if operation == "append":
            result = service.spreadsheets().values().append(
                spreadsheetId=spreadsheet, range=range_name,
                valueInputOption="USER_ENTERED", body=body,
            ).execute()
        else:
            result = service.spreadsheets().values().update(
                spreadsheetId=spreadsheet, range=range_name,
                valueInputOption="USER_ENTERED", body=body,
            ).execute()
        emit(True, f"Sheet {operation} succeeded.", data=result)
    else:
        fail(f"Unknown sheets operation '{operation}'. Use read, write, or append.")


def docs(spec: dict) -> None:
    service = _service(spec, "docs", "v1")
    document_id = str(spec.get("document_id") or "")
    operation = str(spec.get("docs_operation") or "get").lower()
    if operation == "get":
        if not document_id:
            fail("document_id is required.")
        result = service.documents().get(documentId=document_id).execute()
        emit(True, "Document read.", data=result)
    elif operation == "create":
        result = service.documents().create(body={"title": str(spec.get("title") or "Untitled")}).execute()
        emit(True, "Document created.", data=result)
    elif operation == "append":
        if not document_id:
            fail("document_id is required.")
        text = str(spec.get("text") or "")
        if not text:
            fail("text is required.")
        result = service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]},
        ).execute()
        emit(True, "Text appended.", data=result)
    else:
        fail(f"Unknown docs operation '{operation}'. Use get, create, or append.")


def slides(spec: dict) -> None:
    service = _service(spec, "slides", "v1")
    presentation = str(spec.get("presentation_id") or "")
    if not presentation:
        fail("presentation_id is required.")
    result = service.presentations().get(presentationId=presentation).execute()
    emit(True, "Presentation read.", data=result)


def chat(spec: dict) -> None:
    service = _service(spec, "chat", "v1")
    space = str(spec.get("space") or "")
    text = str(spec.get("text") or "")
    if not space or not text:
        fail("space and text are required.")
    result = service.spaces().messages().create(
        parent=space, body={"text": text},
    ).execute()
    emit(True, "Chat message sent.", data=result)


def main() -> None:
    spec = load_spec()
    service = str(spec.get("service") or "").lower()
    handlers = {
        "auth": auth,
        "gmail": gmail,
        "calendar": calendar,
        "drive": drive,
        "sheets": sheets,
        "docs": docs,
        "slides": slides,
        "chat": chat,
    }
    handler = handlers.get(service)
    if handler is None:
        fail("service is required: auth, gmail, calendar, drive, sheets, docs, slides, or chat.")
    handler(spec)


if __name__ == "__main__":
    main()
