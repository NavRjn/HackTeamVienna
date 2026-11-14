"""Private local document storage for the dashboard."""

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile

MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
EXTENSIONS = {
    "bim_models": {".ifc"},
    "drawings_plans": {".pdf", ".jpg", ".jpeg", ".png"},
    "schedules": {".csv", ".xlsx"},
    "financial_documents": {".pdf"},
}
MEDIA_TYPES = {
    ".ifc": "application/x-step",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class DocumentStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.root / "metadata.json"
        self.lock = threading.Lock()

    def _read(self) -> list[dict]:
        if not self.metadata_path.exists():
            return []
        try:
            value = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(500, "Document metadata could not be read.") from exc
        if not isinstance(value, list):
            raise HTTPException(500, "Document metadata is invalid.")
        return value

    def list(self) -> list[dict]:
        with self.lock:
            return sorted(self._read(), key=lambda item: item["uploaded_at"], reverse=True)

    @staticmethod
    def _filename(value: str | None) -> tuple[str, str]:
        name = (value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or len(name) > 255 or "\0" in name:
            raise HTTPException(400, "A valid filename is required.")
        extension = Path(name).suffix.lower()
        if not extension:
            raise HTTPException(415, "File extension is not supported.")
        return name, extension

    @staticmethod
    def _validate_content(extension: str, prefix: bytes) -> None:
        valid = True
        if extension == ".pdf":
            valid = prefix.startswith(b"%PDF-")
        elif extension == ".png":
            valid = prefix.startswith(b"\x89PNG\r\n\x1a\n")
        elif extension in {".jpg", ".jpeg"}:
            valid = prefix.startswith(b"\xff\xd8\xff")
        elif extension == ".xlsx":
            valid = prefix.startswith(b"PK\x03\x04")
        elif extension == ".ifc":
            valid = prefix.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"ISO-10303-21;")
        elif extension == ".csv":
            valid = b"\0" not in prefix
        if not valid:
            raise HTTPException(415, "File content does not match its extension.")

    async def save(self, upload: UploadFile, category: str, revision_label: str | None,
                   location_ref: str | None, task_ref: str | None) -> dict:
        original_filename, extension = self._filename(upload.filename)
        if category not in EXTENSIONS or extension not in EXTENSIONS[category]:
            raise HTTPException(415, "File type is not allowed in this category.")
        revision = (revision_label or "").strip() or None
        if revision is not None and len(revision) > 80:
            raise HTTPException(400, "Revision label must be 80 characters or fewer.")
        references = [value.strip() or None if value else None for value in (location_ref, task_ref)]
        if any(value is not None and len(value) > 100 for value in references):
            raise HTTPException(400, "Location and task references must be 100 characters or fewer.")

        document_id = uuid4().hex
        temporary = self.root / ("." + document_id + ".upload")
        stored = self.root / (document_id + extension)
        size = 0
        prefix = bytearray()
        try:
            with temporary.open("xb") as stream:
                while chunk := await upload.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_DOCUMENT_BYTES:
                        raise HTTPException(413, "File exceeds the 100 MB upload limit.")
                    if len(prefix) < 512:
                        prefix.extend(chunk[:512 - len(prefix)])
                    stream.write(chunk)
            if size == 0:
                raise HTTPException(400, "Empty files cannot be uploaded.")
            self._validate_content(extension, bytes(prefix))
            os.replace(temporary, stored)
            document = {
                "id": document_id,
                "original_filename": original_filename,
                "category": category,
                "file_size": size,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "revision_label": revision,
                "location_ref": references[0],
                "task_ref": references[1],
            }
            with self.lock:
                documents = self._read()
                documents.append(document)
                temp_metadata = self.root / (".metadata-" + document_id + ".tmp")
                try:
                    temp_metadata.write_text(json.dumps(documents, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(temp_metadata, self.metadata_path)
                finally:
                    temp_metadata.unlink(missing_ok=True)
            return document
        except Exception:
            temporary.unlink(missing_ok=True)
            if stored.exists() and not any(item.get("id") == document_id for item in self._read()):
                stored.unlink(missing_ok=True)
            raise

    def file(self, document_id: str) -> tuple[dict, Path, str]:
        if not re.fullmatch(r"[a-f0-9]{32}", document_id):
            raise HTTPException(404, "Document not found.")
        document = next((item for item in self.list() if item["id"] == document_id), None)
        if document is None:
            raise HTTPException(404, "Document not found.")
        _, extension = self._filename(document["original_filename"])
        if extension not in MEDIA_TYPES:
            raise HTTPException(404, "Document not found.")
        path = self.root / (document_id + extension)
        if not path.is_file():
            raise HTTPException(404, "Document not found.")
        return document, path, extension