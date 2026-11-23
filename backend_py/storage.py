"""Short-lived storage for images returned by the configured image provider."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from urllib.parse import quote


MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
SAFE_NAME = re.compile(r"[a-f0-9]{48}\.(?:png|jpg|webp)\Z")
VERCEL_BLOB_API_BASE = "https://blob.vercel-storage.com"
VERCEL_BLOB_TOKEN_PATTERN = re.compile(r"^vercel_blob_rw_([A-Za-z0-9]{8,64})_[A-Za-z0-9_-]{8,}$")


def image_type(data: bytes) -> str | None:
    """Recognize supported container signatures; this does not decode pixels."""
    if not isinstance(data, bytes):
        return None
    if (len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n"
            and data[12:16] == b"IHDR"
            and int.from_bytes(data[16:20], "big") > 0
            and int.from_bytes(data[20:24], "big") > 0):
        return "image/png"
    if len(data) > 4 and data[:3] == b"\xff\xd8\xff" and data[-2:] == b"\xff\xd9":
        return "image/jpeg"
    if (len(data) >= 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
            and int.from_bytes(data[4:8], "little") + 8 == len(data)):
        return "image/webp"
    return None


class FileImageStore:
    def __init__(self, directory: str | Path, ttl_seconds: int = 900, now=time.time):
        self.directory = Path(directory).resolve()
        self.ttl_seconds = ttl_seconds
        self.now = now
        self._lock = threading.Lock()

    def save(self, data: bytes, mime: str) -> str:
        if mime not in MIME_EXT or image_type(data) != mime:
            raise ValueError("Unsupported generated image")
        self.directory.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            name = f"{secrets.token_hex(24)}.{MIME_EXT[mime]}"
            try:
                with (self.directory / name).open("xb") as handle:
                    try:
                        os.chmod(handle.name, 0o600)
                    except OSError:
                        pass  # Windows ACLs control local access instead.
                    handle.write(data)
                stamp = self.now()
                os.utime(self.directory / name, (stamp, stamp))
                return name
            except FileExistsError:
                continue
        raise OSError("Could not allocate an image filename")

    def read(self, filename: str) -> tuple[bytes, str] | None:
        if not SAFE_NAME.fullmatch(filename):
            return None
        path = self.directory / filename
        with self._lock:
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                    return None
                if self.now() - info.st_mtime >= self.ttl_seconds:
                    path.unlink(missing_ok=True)
                    return None
                mime = next(mime for mime, ext in MIME_EXT.items() if filename.endswith("." + ext))
                return path.read_bytes(), mime
            except FileNotFoundError:
                return None

    def cleanup(self) -> None:
        with self._lock:
            try:
                paths = list(self.directory.iterdir())
            except FileNotFoundError:
                return
            for path in paths:
                if not SAFE_NAME.fullmatch(path.name):
                    continue
                try:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and self.now() - info.st_mtime >= self.ttl_seconds:
                        path.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass

    def close(self) -> None:
        pass


def parse_vercel_blob_store_id(token: str) -> str:
    """Extract the lowercase store ID from a Vercel Blob read-write token."""
    match = VERCEL_BLOB_TOKEN_PATTERN.fullmatch(token or "")
    if not match:
        raise ValueError("Invalid BLOB_READ_WRITE_TOKEN format")
    return match.group(1).lower()


class VercelBlobImageStore:
    """Short-lived image storage backed by Vercel Blob REST API (for serverless deployments)."""

    def __init__(self, token: str, ttl_seconds: int = 900, now=time.time, *,
                 fetch, prefix: str = "locket-generated", timeout: float = 15,
                 max_image_bytes: int = 10 * 1024 * 1024):
        self.token = token
        self.store_id = parse_vercel_blob_store_id(token)
        self.ttl_seconds = ttl_seconds
        self.now = now
        self.fetch = fetch
        self.prefix = prefix.strip("/") or "locket-generated"
        self.timeout = timeout
        self.max_image_bytes = max_image_bytes
        self.public_host = f"https://{self.store_id}.public.blob.vercel-storage.com"

    def _pathname(self, filename: str) -> str:
        return f"{self.prefix}/{filename}"

    def _blob_url(self, filename: str) -> str:
        return f"{self.public_host}/{self._pathname(filename)}"

    def save(self, data: bytes, mime: str) -> str:
        if mime not in MIME_EXT or image_type(data) != mime:
            raise ValueError("Unsupported generated image")
        name = f"{secrets.token_hex(24)}.{MIME_EXT[mime]}"
        pathname = quote(self._pathname(name), safe="/")
        url = f"{VERCEL_BLOB_API_BASE}/{pathname}"
        response = self.fetch(
            url,
            method="PUT",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": mime,
                "x-content-type": mime,
                "x-add-random-suffix": "0",
                "x-vercel-blob-access": "public",
                "x-api-version": "7",
                "x-cache-control-max-age": str(self.ttl_seconds),
            },
            body=data,
            maximum=64 * 1024,
            timeout=self.timeout,
        )
        if not 200 <= response.status < 300:
            if os.environ.get("VERCEL"):
                print(f"[locket-backend] blob_upload_failed status={response.status}", flush=True)
            raise OSError(f"Vercel Blob upload failed with status {response.status}")
        return name

    def _delete_best_effort(self, blob_url: str) -> None:
        try:
            self.fetch(
                f"{VERCEL_BLOB_API_BASE}/delete",
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "x-api-version": "7",
                },
                body=json.dumps({"urls": [blob_url]}, separators=(",", ":")).encode("utf-8"),
                maximum=16 * 1024,
                timeout=min(5.0, self.timeout),
            )
        except Exception:
            pass

    def read(self, filename: str) -> tuple[bytes, str] | None:
        if not SAFE_NAME.fullmatch(filename):
            return None
        blob_url = self._blob_url(filename)
        try:
            response = self.fetch(
                blob_url,
                method="GET",
                headers={"Accept": "image/png,image/jpeg,image/webp"},
                body=None,
                maximum=self.max_image_bytes,
                timeout=self.timeout,
            )
        except Exception:
            return None
        if response.status == 404 or not 200 <= response.status < 300:
            return None
        last_modified = response.headers.get("last-modified")
        if last_modified:
            try:
                uploaded_ts = parsedate_to_datetime(last_modified).timestamp()
                if self.now() - uploaded_ts >= self.ttl_seconds:
                    self._delete_best_effort(blob_url)
                    return None
            except (TypeError, ValueError, OverflowError):
                pass
        mime = next(m for m, ext in MIME_EXT.items() if filename.endswith("." + ext))
        if image_type(response.body) != mime:
            return None
        return response.body, mime

    def cleanup(self) -> None:
        pass

    def close(self) -> None:
        pass
