"""Run with ``python -m backend_py.server``. Local composition needs Pillow.

Secrets are read exclusively from the supplied environment. Importing this module
does not read .env files, start a listener, or make network requests.
"""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import ssl
import threading
import time
from typing import Mapping
from urllib.parse import urlsplit

from .storage import FileImageStore, MIME_EXT, VercelBlobImageStore, image_type, parse_vercel_blob_store_id
from .composition import CompositionError, compose_images, validate_composition_request


MIB = 1024 * 1024
DEFAULT_HOSTS = (
    "firebasestorage.googleapis.com",
    "storage.googleapis.com",
    "cdn.locketcamera.com",
    "images.locketcamera.com",
    "media.locketcamera.com",
    "img.locketcamera.com",
    "storage.locketcamera.com",
    "locket-4252a.firebasestorage.app",
    "locket-4252a.appspot.com",
    "lh3.googleusercontent.com",
    "imagedelivery.net",
)
OPENAI_EDIT_URL = "https://api.openai.com/v1/images/edits"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4/accounts"
DEFAULT_CLOUDFLARE_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
DNS_NAME = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def fail(status: int, code: str, message: str):
    raise AppError(status, code, message)


def _integer(env, name, default, minimum, maximum):
    value = env.get(name, "")
    if value == "":
        return default
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"Invalid {name}")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"Invalid {name}")
    return number


def _dns_name(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return False
    except ValueError:
        return bool(DNS_NAME.fullmatch(value)) and value != "localhost" and not value.endswith(".localhost")


def _split_https(value: str):
    if not isinstance(value, str) or not value or re.search(r"[\x00-\x20\x7f\\]", value):
        raise ValueError("Invalid HTTPS URL")
    parts = urlsplit(value)
    # Explicit ports, including :443, and even empty query/fragment delimiters
    # are rejected for origins. Image query strings remain supported below.
    if (parts.scheme != "https" or not parts.netloc or not parts.hostname
            or parts.username is not None or parts.password is not None
            or ":" in parts.netloc or "@" in parts.netloc
            or not _dns_name(parts.hostname.lower())):
        raise ValueError("Invalid HTTPS URL")
    return parts


def _public_origin(value: str) -> str:
    if not value:
        return ""
    try:
        parts = _split_https(value)
        if parts.path not in ("", "/") or "?" in value or "#" in value:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS DNS origin without port, credentials, path, query or fragment") from None
    return f"https://{parts.hostname.lower()}"


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8787
    provider: str = "openai"
    api_key: str = field(default="", repr=False)
    cloudflare_credentials: tuple[tuple[str, str], ...] = ()  # list of (account_id, api_token)
    cloudflare_model: str = DEFAULT_CLOUDFLARE_MODEL
    blob_read_write_token: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    public_base_url: str = ""
    model: str = "gpt-image-2.5-flare"
    allowed_hosts: tuple[str, ...] = DEFAULT_HOSTS
    data_dir: Path = field(default_factory=lambda: Path("data/generated").resolve())
    max_image_bytes: int = 10 * MIB
    max_body_bytes: int = 16 * 1024
    max_provider_bytes: int = 32 * MIB
    download_timeout: float = 8
    provider_timeout: float = 32
    image_ttl_seconds: int = 900
    rate_limit: int = 3
    max_concurrent: int = 1

    @property
    def configured(self) -> bool:
        if len(self.access_token) < 24 or not self.public_base_url:
            return False
        if self.provider == "cloudflare":
            return bool(self.cloudflare_credentials)
        return bool(self.api_key.strip())

    @property
    def composition_configured(self) -> bool:
        return bool(len(self.access_token) >= 24 and self.public_base_url)


def _auto_public_base_url(env: Mapping[str, str]) -> str:
    explicit = env.get("PUBLIC_BASE_URL", "").strip()
    if explicit:
        return _public_origin(explicit)
    for vercel_var in ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        candidate = env.get(vercel_var, "").strip()
        if candidate:
            if not candidate.startswith("https://"):
                candidate = "https://" + candidate
            return _public_origin(candidate)
    return ""


def read_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    hosts = tuple(host.strip().lower() for host in env.get("INPUT_IMAGE_HOSTS", ",".join(DEFAULT_HOSTS)).split(",") if host.strip())
    if not hosts or any(not _dns_name(host) for host in hosts):
        raise ValueError("INPUT_IMAGE_HOSTS must contain exact trusted DNS hostnames")
    model = env.get("OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model):
        raise ValueError("Invalid OPENAI_IMAGE_MODEL")
    cf_model = env.get("CLOUDFLARE_IMAGE_MODEL", DEFAULT_CLOUDFLARE_MODEL)
    if not re.fullmatch(r"[A-Za-z0-9@/_.-]{1,120}", cf_model) or ".." in cf_model:
        raise ValueError("Invalid CLOUDFLARE_IMAGE_MODEL")
    # Hỗ trợ nhiều credentials
    cf_credentials = []

    # Ưu tiên đọc từ CLOUDFLARE_CREDENTIALS (JSON)
    raw_creds = env.get("CLOUDFLARE_CREDENTIALS", "").strip()
    if raw_creds:
        try:
            data = json.loads(raw_creds)
            if not isinstance(data, list):
                print("[locket-backend] CLOUDFLARE_CREDENTIALS must be a JSON array; ignoring it", flush=True)
                data = []
            for pos, item in enumerate(data):
                if not isinstance(item, dict):
                    print(f"[locket-backend] CLOUDFLARE_CREDENTIALS[{pos}] is not an object; skipped", flush=True)
                    continue
                acc = str(item.get("account_id", "")).strip()
                tok = str(item.get("api_token", "")).strip()
                if acc and tok and re.fullmatch(r"[A-Fa-f0-9]{16,64}", acc):
                    cf_credentials.append((acc, tok))
                else:
                    print(f"[locket-backend] CLOUDFLARE_CREDENTIALS[{pos}] needs hex account_id and api_token; skipped", flush=True)
        except ValueError:
            print("[locket-backend] CLOUDFLARE_CREDENTIALS is not valid JSON; falling back to CLOUDFLARE_API_TOKEN", flush=True)
        print(f"[locket-backend] loaded {len(cf_credentials)} Cloudflare credential(s) from CLOUDFLARE_CREDENTIALS", flush=True)

    # Fallback về 1 cặp cũ (tương thích ngược)
    cf_token = env.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not cf_credentials:
        cf_account = env.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        if cf_account and not re.fullmatch(r"[A-Fa-f0-9]{16,64}", cf_account):
            raise ValueError("Invalid CLOUDFLARE_ACCOUNT_ID")
        if cf_account and cf_token:
            cf_credentials.append((cf_account, cf_token))

    blob_token = env.get("BLOB_READ_WRITE_TOKEN", "")
    api_key = env.get("OPENAI_API_KEY", "")
    token = env.get("BACKEND_ACCESS_TOKEN", "")
    cred_text = "".join(t for _, t in cf_credentials)
    if any(char in api_key + token + cf_token + cred_text + blob_token for char in "\r\n\x00"):
        raise ValueError("Invalid credential format")
    if blob_token:
        parse_vercel_blob_store_id(blob_token)
    raw_provider = env.get("IMAGE_PROVIDER", "").strip().lower()
    if raw_provider in ("", "auto"):
        provider = "cloudflare" if cf_credentials else "openai"
    elif raw_provider in ("cloudflare", "openai"):
        provider = raw_provider
    else:
        raise ValueError("Invalid IMAGE_PROVIDER")
    host = env.get("HOST", "127.0.0.1")
    if not host or re.search(r"[\x00-\x20\x7f]", host):
        raise ValueError("Invalid HOST")
    default_data_dir = "/tmp/locket-ai-generated" if env.get("VERCEL") else "./data/generated"
    return Config(
        host=host, port=_integer(env, "PORT", 8787, 0, 65535),
        provider=provider, api_key=api_key,
        cloudflare_credentials=tuple(cf_credentials),
        cloudflare_model=cf_model, blob_read_write_token=blob_token,
        access_token=token,
        public_base_url=_auto_public_base_url(env), model=model,
        allowed_hosts=hosts, data_dir=Path(env.get("DATA_DIR", default_data_dir)).resolve(),
        max_image_bytes=_integer(env, "MAX_IMAGE_BYTES", 10 * MIB, 1024, 20 * MIB),
        download_timeout=_integer(env, "DOWNLOAD_TIMEOUT_MS", 8000, 100, 8000) / 1000,
        provider_timeout=_integer(env, "PROVIDER_TIMEOUT_MS", 32000, 100, 35000) / 1000,
        image_ttl_seconds=_integer(env, "IMAGE_TTL_SECONDS", 900, 30, 86400),
        rate_limit=_integer(env, "RATE_LIMIT_PER_MINUTE", 3, 1, 60),
        max_concurrent=_integer(env, "MAX_CONCURRENT", 1, 1, 4),
    )


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return ip.is_global and not ip.is_multicast and not ip.is_reserved and not ip.is_unspecified
    except ValueError:
        return False


def resolve_public_addresses(host: str, timeout: float = 2) -> tuple[str, ...]:
    """Bound DNS waiting and fail closed if any answer is not public."""
    output = queue.Queue(maxsize=1)

    def lookup():
        try:
            output.put(tuple(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))))
        except Exception:
            output.put(None)

    threading.Thread(target=lookup, daemon=True).start()
    try:
        addresses = output.get(timeout=max(0.001, timeout))
    except queue.Empty:
        addresses = None
    if not addresses:
        fail(400, "image_dns_failed", "The image host could not be resolved.")
    if any(not is_public_address(address) for address in addresses):
        fail(400, "unsafe_image_host", "The image host must resolve to public addresses.")
    return addresses


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    hostname: str
    addresses: tuple[str, ...]


def _strip_default_https_port(value: str) -> str:
    if isinstance(value, str) and value.startswith("https://"):
        parts = urlsplit(value)
        if parts.hostname and parts.netloc.lower() == f"{parts.hostname.lower()}:443":
            return f"https://{parts.hostname}{parts.path}" + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")
    return value


def validate_input_url(value: str, config: Config, resolver=resolve_public_addresses, timeout=2) -> ValidatedUrl:
    normalized = _strip_default_https_port(value)
    try:
        parts = _split_https(normalized)
    except (ValueError, TypeError):
        if os.environ.get("VERCEL") and isinstance(value, str):
            raw_parts = urlsplit(value)
            safe_netloc = raw_parts.netloc.split("@")[-1][:120]
            print(f"[locket-backend] rejected_url_parse scheme={raw_parts.scheme} netloc={safe_netloc}", flush=True)
        fail(400, "unsafe_image_url", "The image URL must use an approved HTTPS image host.")
    value = normalized
    host = parts.hostname.lower()
    if "#" in value or host not in config.allowed_hosts:
        if "#" not in value and os.environ.get("VERCEL"):
            print(f"[locket-backend] rejected_image_host={host}", flush=True)
        fail(400, "unsafe_image_url", "The image URL must use an approved HTTPS image host.")
    try:
        addresses = tuple(resolver(host, timeout))
    except AppError:
        raise
    except Exception:
        fail(400, "image_dns_failed", "The image host could not be resolved.")
    if not addresses or any(not is_public_address(address) for address in addresses):
        fail(400, "unsafe_image_host", "The image host must resolve to public addresses.")
    return ValidatedUrl(value, host, addresses)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated address, preserving TLS SNI/certificate checks."""
    def __init__(self, host, addresses, deadline):
        super().__init__(host, timeout=max(0.001, deadline - time.monotonic()), context=ssl.create_default_context())
        self.addresses, self.deadline = addresses, deadline
        self._active_socket = None

    def connect(self):
        last_error = None
        for address in self.addresses:
            raw = None
            try:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                raw = socket.create_connection((address, 443), timeout=remaining)
                raw.settimeout(max(0.001, self.deadline - time.monotonic()))
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                self._active_socket = self.sock
                return
            except OSError as error:
                last_error = error
                if raw is not None:
                    raw.close()
        raise last_error or OSError("No public address")


def request_https(url: str, *, method: str, headers: Mapping[str, str], body: bytes | None,
                  maximum: int, timeout: float, addresses: tuple[str, ...] | None = None) -> Response:
    """Direct HTTPS only: no ambient proxy, redirect, credential or cookie reuse."""
    deadline = time.monotonic() + timeout
    parts = _split_https(url)
    if "#" in url:
        raise ValueError("URL fragments are not supported")
    if addresses is None:
        addresses = resolve_public_addresses(parts.hostname, min(2, max(0.001, deadline - time.monotonic())))
    if not addresses or any(not is_public_address(address) for address in addresses):
        raise ValueError("Non-public upstream address")
    connection = _PinnedHTTPSConnection(parts.hostname, addresses, deadline)
    def interrupt_at_deadline():
        # http.client may detach connection.sock after a Connection: close
        # response. Retain the TLS socket so status/header trickles cannot extend
        # the total deadline beyond the configured provider/download budget.
        active = connection._active_socket
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            active.close()

    watchdog = threading.Timer(max(0.001, deadline - time.monotonic()), interrupt_at_deadline)
    watchdog.daemon = True
    watchdog.start()
    try:
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.request(method, target, body=body, headers=dict(headers))
        connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        length = response_headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > maximum):
            raise ValueError("Upstream response too large or invalid")
        if 300 <= response.status < 400:
            return Response(response.status, response_headers, b"")
        body_limit = maximum if 200 <= response.status < 300 else min(maximum, 16 * 1024)
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            # read1 returns after one available read, so a trickle cannot reset
            # the overall deadline indefinitely.
            chunk = response.read1(min(64 * 1024, body_limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > body_limit:
                raise ValueError("Upstream response too large")
        return Response(response.status, response_headers, b"".join(chunks))
    finally:
        watchdog.cancel()
        connection.close()


def _bounded_response(response: Response, maximum: int, code: str) -> bytes:
    length = response.headers.get("content-length")
    if ((length is not None and (not str(length).isdigit() or int(length) > maximum))
            or not isinstance(response.body, bytes) or len(response.body) > maximum):
        fail(413, code, "The image or response is too large.")
    return response.body


def download_image(url: ValidatedUrl, config: Config, fetch=request_https, timeout=None) -> tuple[bytes, str]:
    try:
        response = fetch(url.url, method="GET", headers={"Accept": "image/png,image/jpeg,image/webp"}, body=None,
                         maximum=config.max_image_bytes, timeout=timeout or config.download_timeout, addresses=url.addresses)
    except Exception:
        fail(502, "image_download_failed", "The input image could not be downloaded.")
    if not 200 <= response.status < 300:
        fail(502, "image_download_failed", "The input image could not be downloaded.")
    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        fail(415, "unsupported_image", "Use a PNG, JPEG or WebP image.")
    data = _bounded_response(response, config.max_image_bytes, "input_too_large")
    if image_type(data) != mime:
        fail(415, "invalid_image", "The downloaded bytes are not a supported image.")
    return data, mime


def create_openai_editor(config: Config, fetch=request_https):
    def edit(image: tuple[bytes, str], prompt: str) -> tuple[bytes, str]:
        data, mime = image
        payload = {
            "model": config.model,
            "images": [{"image_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}],
            "prompt": prompt, "n": 1, "size": "1024x1024", "quality": "low", "output_format": "png",
        }
        try:
            response = fetch(OPENAI_EDIT_URL, method="POST",
                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + config.api_key},
                             body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                             maximum=config.max_provider_bytes, timeout=config.provider_timeout)
        except Exception:
            fail(502, "provider_unavailable", "The image provider is unavailable or timed out.")
        if response.status == 429:
            fail(429, "provider_rate_limited", "The image provider rate limit was reached.")
        if not 200 <= response.status < 300:
            # Parse only a bounded error code; provider text is never forwarded.
            blocked = False
            try:
                error = json.loads(response.body[:config.max_body_bytes]).get("error", {})
                blocked = error.get("code") in ("content_policy_violation", "moderation_blocked", "safety_violation")
            except (ValueError, TypeError, AttributeError):
                pass
            if blocked:
                fail(422, "generation_blocked", "The image provider blocked this request.")
            fail(502, "provider_rejected_request", "The image provider rejected the request. Check its key, billing and model configuration.")
        raw = _bounded_response(response, config.max_provider_bytes, "provider_response_too_large")
        try:
            payload = json.loads(raw)
            encoded = payload["data"][0]["b64_json"]
            if not isinstance(encoded, str) or not encoded:
                raise ValueError()
        except (ValueError, TypeError, KeyError, IndexError):
            fail(502, "invalid_provider_response", "The image provider did not return an edited image.")
        if len(encoded) > 4 * ((config.max_image_bytes + 2) // 3):
            fail(502, "invalid_generated_image", "The provider returned invalid image data.")
        try:
            generated = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            fail(502, "invalid_generated_image", "The provider returned invalid image data.")
        detected = image_type(generated)
        if (detected != "image/png" or len(generated) > config.max_image_bytes
                or base64.b64encode(generated).decode("ascii") != encoded):
            fail(502, "invalid_generated_image", "The provider returned invalid image data.")
        return generated, detected
    return edit


def _cloudflare_dimensions(data: bytes) -> tuple[int, int]:
    """Compute FLUX.2 output dimensions preserving aspect ratio within 512..1024."""
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            width, height = img.size
    except Exception:
        return (768, 768)
    if width <= 0 or height <= 0:
        return (768, 768)
    max_side = 1024
    max_pixels = 576 * 1024
    scale = min(1.0, max_side / max(width, height), (max_pixels / (width * height)) ** 0.5)
    target_w = max(512, min(1024, int(round((width * scale) / 16)) * 16))
    target_h = max(512, min(1024, int(round((height * scale) / 16)) * 16))
    return (target_w, target_h)


def _encode_multipart(fields: list[tuple[str, str]],
                      files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = f"----LocketCF{secrets.token_hex(12)}"
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    for name, filename, mime, content in files:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8")
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


_STYLIZED_PROMPT_RE = re.compile(
    r"\b(?:minecraft|watercolor|anime|ghibli|cartoon|comic|sketch|oil painting|"
    r"pixel|3d|fortnite|zombie|ogre|clay|lego|simpson|disney|pixar|voxel|chibi)\b",
    re.I,
)
_CF_REALISTIC_IDENTITY_LOCK = (
    "Strictly preserve the person's exact face, eyes, nose, mouth, skin tone, hairstyle, "
    "expression, head shape, pose, camera framing, and original background unchanged. "
    "Only apply the requested edit seamlessly with matching natural lighting and realistic camera texture. "
    "Single continuous photo, no distortion."
)
_CF_STYLIZED_IDENTITY_LOCK = (
    "Keep the person's recognizable facial likeness, expression, pose, and camera framing "
    "consistent with the original photo. Single continuous photo, no collage or split-screen."
)


def _optimize_cloudflare_prompt(prompt: str) -> str:
    """Tune Locket prompts for Cloudflare FLUX.2 Klein 4B: lock face identity and avoid false-positive blocks."""
    cleaned = " ".join(prompt.split())
    lower = cleaned.lower()
    if "superhero" in lower and "cape" in lower:
        cleaned = (
            "Show the person in the photo wearing a royal blue armored cosplay jacket "
            "with a gold emblem and a flowing crimson red cape draped over their shoulders."
        )
    elif "shrek" in lower:
        cleaned = (
            "Transform the person's entire face, head, and neck into a realistic friendly fairytale green ogre "
            "with smooth vibrant olive-green skin covering their whole face and neck, iconic green trumpet-shaped ogre ears, "
            "a slightly fuller rounded ogre nose, thick expressive eyebrows, and a rustic medieval brown vest over a cream tunic, "
            "keeping the exact same pose, camera framing, lighting, and original background."
        )
    elif lower.startswith("dress the people in elegant wedding attire"):
        cleaned = (
            "Show the person in the photo wearing sharp tailored formal wedding attire "
            "(a crisp black tuxedo jacket with white dress shirt and black bow tie, "
            "or an elegant white lace bridal gown with a delicate sheer veil) with realistic fabric and lighting."
        )
    lock = _CF_STYLIZED_IDENTITY_LOCK if _STYLIZED_PROMPT_RE.search(cleaned) else _CF_REALISTIC_IDENTITY_LOCK
    if lock.lower() not in cleaned.lower():
        base = cleaned.rstrip(". ")
        if len(base) > 1400:
            base = base[:1400].rsplit(" ", 1)[0].rstrip(",;:. ")
        cleaned = f"{base}. {lock}"
    return cleaned

_cf_index: int | None = None
_cf_lock = threading.Lock()

_CF_BLOCK_CODES = frozenset({"3030", "content_policy_violation", "moderation_blocked", "safety_violation"})


def _cf_errors(body, limit) -> list[tuple[str, str]]:
    """Return (code, short message) pairs from a Cloudflare error body; never raises."""
    try:
        payload = json.loads(body[:limit])
    except (ValueError, TypeError):
        return []
    errors = payload.get("errors") if isinstance(payload, dict) else None
    return [(str(e.get("code", "")), str(e.get("message", ""))[:120])
            for e in (errors if isinstance(errors, list) else []) if isinstance(e, dict)]


def _cf_is_quota_or_auth(status: int, errors) -> bool:
    if status in (401, 403, 429):
        return True
    text = " ".join(f"{c} {m}" for c, m in errors).lower()
    return any(word in text for word in ("neuron", "allocation", "quota", "limit", "4006"))


def create_cloudflare_editor(config: Config, fetch=request_https):
    cooldown: dict[str, float] = {}  # account_id -> monotonic time until which it is skipped

    def edit(image: tuple[bytes, str], prompt: str) -> tuple[bytes, str]:
        if not config.cloudflare_credentials:
            fail(503, "not_configured", "No Cloudflare credentials configured.")

        data, mime = image
        ext = MIME_EXT.get(mime, "jpg")
        width, height = _cloudflare_dimensions(data)
        optimized_prompt = _optimize_cloudflare_prompt(prompt)
        body, content_type = _encode_multipart(
            fields=[("prompt", optimized_prompt), ("width", str(width)), ("height", str(height))],
            files=[("input_image_0", f"input.{ext}", mime, data)],
        )

        global _cf_index
        n = len(config.cloudflare_credentials)
        last_status = None

        with _cf_lock:
            if _cf_index is None:
                # Serverless instances start cold; a random first pick spreads load
                # across credentials instead of always starting at index 0.
                _cf_index = secrets.randbelow(n)
            start = _cf_index % n
            _cf_index = (start + 1) % n

        # Skip accounts that recently reported exhausted quota / bad auth, unless
        # every account is cooling down (then try them all rather than give up).
        now = time.monotonic()
        order = [(start + i) % n for i in range(n)]
        candidates = [i for i in order if cooldown.get(config.cloudflare_credentials[i][0], 0) <= now] or order
        # One shared deadline: trying several accounts must not exceed the budget the
        # Locket script / Vercel function is willing to wait.
        budget_deadline = now + config.provider_timeout

        for idx in candidates:
            remaining = budget_deadline - time.monotonic()
            if remaining < 1:
                break
            account_id, api_token = config.cloudflare_credentials[idx]
            tag = f"idx={idx}/{n} account=...{account_id[-4:]}"
            url = f"{CLOUDFLARE_API_BASE}/{account_id}/ai/run/{config.cloudflare_model}"

            try:
                response = fetch(
                    url,
                    method="POST",
                    headers={
                        "Content-Type": content_type,
                        "Authorization": "Bearer " + api_token,
                    },
                    body=body,
                    maximum=config.max_provider_bytes,
                    timeout=remaining,
                )
            except Exception:
                print(f"[locket-backend] cf_credential {tag} network_error", flush=True)
                last_status = 502
                continue

            ok_status = 200 <= response.status < 300
            payload = None
            errors = []
            if not ok_status:
                errors = _cf_errors(response.body, config.max_body_bytes)
            else:
                raw = _bounded_response(response, config.max_provider_bytes, "provider_response_too_large")
                try:
                    payload = json.loads(raw)
                except (ValueError, TypeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("success") is False:
                    errors = _cf_errors(raw, config.max_provider_bytes)
                    ok_status = False  # HTTP 200 but Cloudflare reported failure

            if not ok_status:
                summary = "; ".join(f"{c}:{m}" for c, m in errors[:3]) or "-"
                print(f"[locket-backend] cf_credential {tag} status={response.status} errors={summary}", flush=True)
                if any(code in _CF_BLOCK_CODES for code, _ in errors):
                    fail(422, "generation_blocked", "The image provider blocked this request.")
                if _cf_is_quota_or_auth(response.status, errors):
                    cooldown[account_id] = time.monotonic() + 600
                    last_status = 429 if response.status == 429 or _cf_is_quota_or_auth(0, errors) else response.status
                else:
                    last_status = response.status
                continue

            print(f"[locket-backend] cf_credential {tag} status={response.status} response_received", flush=True)
            # Thành công → parse ảnh
            try:
                if not isinstance(payload, dict):
                    raise ValueError()
                # Cloudflare trả về image dạng base64 trong result
                result = payload.get("result")
                if isinstance(result, dict) and "image" in result:
                    encoded = result["image"]
                elif isinstance(result, str):
                    encoded = result
                else:
                    raise ValueError()
                if not isinstance(encoded, str) or not encoded:
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                print(f"[locket-backend] cf_credential {tag} empty_result; trying next", flush=True)
                last_status = 502
                continue

            if len(encoded) > 4 * ((config.max_image_bytes + 2) // 3):
                fail(502, "invalid_generated_image", "The provider returned invalid image data.")

            try:
                # Cloudflare đôi khi trả base64 thuần, đôi khi có data:image/...
                if "," in encoded:
                    encoded = encoded.split(",", 1)[1]
                generated = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                fail(502, "invalid_generated_image", "The provider returned invalid image data.")

            detected = image_type(generated)
            if detected not in ("image/png", "image/jpeg", "image/webp") or len(generated) > config.max_image_bytes:
                fail(502, "invalid_generated_image", "The provider returned invalid image data.")
            return generated, detected

        # Tất cả credentials đều fail
        if last_status == 429:
            fail(429, "provider_rate_limited", "All Cloudflare accounts are rate limited.")
        fail(502, "provider_unavailable", "All Cloudflare providers failed.")

    return edit


def create_image_editor(config: Config, fetch=request_https):
    if config.provider == "cloudflare":
        return create_cloudflare_editor(config, fetch)
    return create_openai_editor(config, fetch)


def create_image_store(config: Config, *, fetch=request_https, now=time.time):
    if config.blob_read_write_token:
        return VercelBlobImageStore(
            config.blob_read_write_token,
            config.image_ttl_seconds,
            now,
            fetch=fetch,
            max_image_bytes=config.max_image_bytes,
        )
    return FileImageStore(config.data_dir, config.image_ttl_seconds, now)


class BackendServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Do not log exception messages, URLs, photos, credentials, or prompts.
        pass

    def service_actions(self):
        if time.monotonic() - self._last_cleanup >= 60:
            self._last_cleanup = time.monotonic()
            try:
                self.image_store.cleanup()
            except OSError:
                pass

    def server_close(self):
        super().server_close()
        if hasattr(self, "image_store"):
            self.image_store.close()


def create_handler_class(*, config: Config, store, fetch=request_https,
                         resolver=resolve_public_addresses, edit_image=None,
                         compose_image=None, composition_manifest_path=None,
                         composition_asset_root=None, now=time.time):
    edit = edit_image or create_image_editor(config, fetch)
    def compose(images, preset_id):
        return compose_images(images, preset_id, manifest_path=composition_manifest_path,
                              asset_root=composition_asset_root)
    composer = compose_image or compose
    state_lock, request_times = threading.Lock(), deque()
    in_flight = 0

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocketBackend"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            if getattr(self, "connection", None) is not None:
                self.connection.settimeout(10)

        def log_message(self, format, *args):
            pass

        def _json(self, status, body, extra=None):
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                self.wfile.write(encoded)
            if status >= 400 and getattr(self, "connection", None) is not None:
                self.wfile.flush()
                # On Windows, closing a socket with an unread request body can
                # reset the connection before the error response reaches the
                # caller. Discard only a small, already-arriving amount after
                # rejecting it; never parse it or wait for an attacker to send
                # the declared Content-Length.
                deadline, remaining = time.monotonic() + 0.01, 64 * 1024
                try:
                    self.connection.settimeout(0.002)
                    while remaining > 0 and time.monotonic() < deadline:
                        chunk = self.rfile.read1(min(remaining, 16 * 1024))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except (OSError, AttributeError):
                    pass

        def _error(self, status, code, message, extra=None):
            self._json(status, {"error": {"code": code, "message": message}}, extra)

        def send_error(self, code, message=None, explain=None):
            # BaseHTTPRequestHandler normally reflects request text in HTML.
            self._error(code, "invalid_request", "The request could not be processed.")

        def _dispatch(self):
            nonlocal in_flight
            try:
                path = self.path.split("?", 1)[0]
                if self.command == "GET" and path == "/health":
                    health = {"ok": True, "configured": config.configured,
                              "compositionConfigured": config.composition_configured}
                    if config.provider == "cloudflare":
                        health["cloudflareCredentials"] = len(config.cloudflare_credentials)
                    return self._json(200, health)
                if self.command == "GET" and path.startswith("/images/"):
                    result = store.read(path[len("/images/"):])
                    if result is None:
                        return self._error(404, "image_not_found", "The image does not exist or has expired.")
                    data, mime = result
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    self.wfile.write(data)
                    return
                if path not in ("/v1/images/edit", "/v1/images/compose"):
                    return self._error(404, "not_found", "Not found.")
                is_composition = path == "/v1/images/compose"
                if self.command != "POST":
                    return self._error(405, "method_not_allowed", "Use POST.", {"Allow": "POST"})
                if is_composition:
                    if not config.composition_configured:
                        fail(503, "not_configured", "Composition needs a backend access token and public HTTPS URL.")
                elif not config.configured:
                    fail(503, "not_configured", "The backend needs its API key, access token and public HTTPS URL.")
                auth = self.headers.get_all("Authorization", [])
                expected = ("Bearer " + config.access_token).encode("utf-8")
                if len(auth) != 1 or not hmac.compare_digest(auth[0].encode("utf-8"), expected):
                    fail(401, "unauthorized", "A valid backend access token is required.")
                if not re.match(r"application/json(?:\s*;|$)", self.headers.get("Content-Type", ""), re.I):
                    fail(415, "invalid_content_type", "Use application/json.")
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1 or not lengths[0].isdigit():
                    fail(400, "invalid_request", "A valid Content-Length is required.")
                length = int(lengths[0])
                if length > config.max_body_bytes:
                    fail(413, "request_too_large", "The request is too large.")
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError()
                    body = json.loads(raw)
                except (ValueError, OSError):
                    fail(400, "invalid_json", "The request body must be valid JSON.")
                if is_composition:
                    if (not isinstance(body, dict) or set(body) - {"imageUrl", "imageUrls", "presetId"}
                            or not isinstance(body.get("presetId"), str) or not body["presetId"]
                            or len(body["presetId"]) > 100 or ("imageUrl" in body) == ("imageUrls" in body)):
                        fail(400, "invalid_input", "Provide presetId and either imageUrl or imageUrls.")
                    source_urls = [body["imageUrl"]] if "imageUrl" in body else body["imageUrls"]
                    if (not isinstance(source_urls, list) or len(source_urls) not in (1, 4)
                            or any(not isinstance(url, str) or not url or len(url) > 4096 for url in source_urls)):
                        fail(400, "invalid_input", "Provide one source photo, or four photos for a four-slot preset.")
                    if compose_image is None:
                        validate_composition_request(body["presetId"], len(source_urls),
                                                     manifest_path=composition_manifest_path,
                                                     asset_root=composition_asset_root)
                else:
                    if (not isinstance(body, dict) or not isinstance(body.get("imageUrl"), str)
                            or len(body["imageUrl"]) > 4096 or not isinstance(body.get("prompt"), str)
                            or not body["prompt"].strip() or len(body["prompt"]) > 4096):
                        fail(400, "invalid_input", "Provide imageUrl and a non-empty prompt of at most 4096 characters.")
                    source_urls = [body["imageUrl"]]
                with state_lock:
                    if in_flight >= config.max_concurrent:
                        fail(429, "backend_busy", "An image is already being generated. Try again later.")
                    stamp = now()
                    while request_times and stamp - request_times[0] >= 60:
                        request_times.popleft()
                    if len(request_times) >= config.rate_limit:
                        fail(429, "backend_rate_limited", "The backend request limit was reached.")
                    request_times.append(stamp)
                    in_flight += 1
                try:
                    download_deadline = time.monotonic() + config.download_timeout
                    images = []
                    for source_url in source_urls:
                        remaining = download_deadline - time.monotonic()
                        if remaining <= 0:
                            fail(502, "image_download_failed", "The input image could not be downloaded.")
                        url = validate_input_url(source_url, config, resolver, min(2, remaining))
                        remaining = download_deadline - time.monotonic()
                        if remaining <= 0:
                            fail(502, "image_download_failed", "The input image could not be downloaded.")
                        images.append(download_image(url, config, fetch, remaining))
                        if time.monotonic() > download_deadline:
                            fail(502, "image_download_failed", "The input image could not be downloaded.")
                    metadata = {}
                    if is_composition:
                        generated, mime, metadata = composer(images, body["presetId"])
                        if (not isinstance(metadata, dict) or metadata.get("sourceImageCount") != len(images)
                                or type(metadata.get("sourceImageCount")) is not int
                                or type(metadata.get("slotCount")) is not int or metadata["slotCount"] not in (1, 4)
                                or len(images) not in (1, metadata["slotCount"]) or mime != "image/png"):
                            fail(502, "invalid_generated_image", "The image composer did not return a valid image.")
                        metadata = {"sourceImageCount": metadata["sourceImageCount"], "slotCount": metadata["slotCount"]}
                    else:
                        generated, mime = edit(images[0], body["prompt"].strip())
                    if (not isinstance(generated, bytes) or len(generated) > config.max_image_bytes
                            or image_type(generated) != mime):
                        fail(502, "invalid_generated_image", "The image provider did not return a valid image.")
                    filename = store.save(generated, mime)
                    self._json(200, {"outputUrl": f"{config.public_base_url}/images/{filename}",
                                     "expiresInSeconds": config.image_ttl_seconds, **metadata})
                finally:
                    with state_lock:
                        in_flight -= 1
            except (AppError, CompositionError) as error:
                self._error(error.status, error.code, error.message)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as error:
                if os.environ.get("VERCEL"):
                    print(f"[locket-backend] internal_error={type(error).__name__}", flush=True)
                self._error(500, "internal_error", "The image request failed.")

        do_GET = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_DELETE = _dispatch
        do_PATCH = _dispatch
        do_HEAD = _dispatch
        do_OPTIONS = _dispatch

    return Handler


def create_server(*, config: Config | None = None, env: Mapping[str, str] | None = None,
                  fetch=request_https, resolver=resolve_public_addresses, edit_image=None,
                  compose_image=None, composition_manifest_path=None, composition_asset_root=None,
                  image_store=None, now=time.time) -> BackendServer:
    config = config or read_config(env)
    store = image_store or create_image_store(config, fetch=fetch, now=now)
    Handler = create_handler_class(
        config=config, store=store, fetch=fetch, resolver=resolver,
        edit_image=edit_image, compose_image=compose_image,
        composition_manifest_path=composition_manifest_path,
        composition_asset_root=composition_asset_root, now=now,
    )
    server = BackendServer((config.host, config.port), Handler)
    server.image_store = store
    server._last_cleanup = 0
    server.config = config
    return server


def create_vercel_handler(*, config: Config | None = None, env: Mapping[str, str] | None = None,
                          fetch=request_https, resolver=resolve_public_addresses,
                          edit_image=None, compose_image=None, image_store=None, now=time.time):
    """Build a BaseHTTPRequestHandler subclass compatible with Vercel's Python runtime."""
    resolved_config = config or read_config(env)
    store = image_store or create_image_store(resolved_config, fetch=fetch, now=now)
    return create_handler_class(
        config=resolved_config, store=store, fetch=fetch, resolver=resolver,
        edit_image=edit_image, compose_image=compose_image, now=now,
    )


def main():
    try:
        server = create_server()
    except (ValueError, OSError):
        raise SystemExit("Backend configuration or listener setup failed. Check environment settings.") from None
    print(f"Locket AI backend listening on port {server.server_port}. See /health for configuration status.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
