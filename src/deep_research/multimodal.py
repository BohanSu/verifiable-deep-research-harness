from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from .providers.base import AttachmentContent, RenderedPage, ResourceLimitExceededError
from .providers.web import _extract_pdf
from .schemas import InputAttachment


MAX_ATTACHMENT_COUNT = 6
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 24 * 1024 * 1024
MAX_ATTACHMENT_TEXT_CHARS = 100_000
MAX_RENDERED_PDF_PAGES = 3
MAX_RENDERED_PDF_BYTES = 12 * 1024 * 1024

_MEDIA_MODALITIES = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/webp": "image",
    "audio/wav": "audio",
    "audio/mpeg": "audio",
    "audio/ogg": "audio",
    "audio/flac": "audio",
    "audio/webm": "audio",
    "audio/mp4": "audio",
    "application/pdf": "document",
    "application/json": "document",
    "text/csv": "document",
    "text/markdown": "document",
    "text/plain": "text",
}
SUPPORTED_ATTACHMENT_MEDIA_TYPES = tuple(_MEDIA_MODALITIES)
_SIGNATURE_REQUIRED_MEDIA_TYPES = frozenset(
    media_type
    for media_type in _MEDIA_MODALITIES
    if media_type.startswith(("image/", "audio/")) or media_type == "application/pdf"
)


def validate_attachment(
    name: str,
    declared_media_type: str,
    data: bytes,
) -> tuple[str, str, str]:
    if not isinstance(data, bytes) or not data:
        raise ValueError("attachment is empty")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("each attachment must be 8 MB or smaller")
    clean_name = _safe_attachment_name(name)
    detected = _detect_media_type(data)
    declared = str(declared_media_type or "").split(";", 1)[0].strip().casefold()
    if declared in {"application/octet-stream", "binary/octet-stream"}:
        declared = ""
    guessed = (mimetypes.guess_type(clean_name)[0] or "").casefold()
    media_type = detected or declared or guessed
    if media_type not in _MEDIA_MODALITIES:
        raise ValueError(f"unsupported attachment media type: {media_type or 'unknown'}")
    if media_type in _SIGNATURE_REQUIRED_MEDIA_TYPES and not detected:
        raise ValueError(
            f"attachment bytes do not contain a valid {media_type} signature"
        )
    if detected and declared and not _compatible_media_types(detected, declared):
        raise ValueError(
            f"attachment bytes are {detected}, not the declared {declared}"
        )
    if media_type.startswith("text/") or media_type == "application/json":
        if b"\x00" in data[:4096]:
            raise ValueError("text attachment contains binary NUL bytes")
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("text attachments must be UTF-8") from error
        if media_type == "application/json":
            try:
                json.loads(decoded)
            except json.JSONDecodeError as error:
                raise ValueError("JSON attachment is not valid JSON") from error
    return clean_name, media_type, _MEDIA_MODALITIES[media_type]


def prepare_attachment_content(
    attachment: InputAttachment,
    data: bytes,
    *,
    render_pdf: bool,
) -> AttachmentContent:
    if hashlib.sha256(data).hexdigest() != attachment.sha256:
        raise ValueError("attachment bytes do not match the durable SHA-256 manifest")
    extracted_text = ""
    parser_version = ""
    rendered_pages: list[RenderedPage] = []
    if attachment.media_type == "application/pdf":
        try:
            extracted_text = _extract_pdf(data)[:MAX_ATTACHMENT_TEXT_CHARS]
            parser_version = "pdftotext-layout-v1"
        except (RuntimeError, ResourceLimitExceededError):
            if not render_pdf:
                raise
        if render_pdf:
            rendered_pages = _render_pdf_pages(data)
            parser_version = (
                f"{parser_version}+pdftoppm-png-v1"
                if parser_version
                else "pdftoppm-png-v1"
            )
    elif attachment.media_type.startswith("text/") or attachment.media_type == "application/json":
        extracted_text = data.decode("utf-8")[:MAX_ATTACHMENT_TEXT_CHARS]
        parser_version = "utf8-text-v1"
    return AttachmentContent(
        attachment=attachment,
        data=data,
        extracted_text=extracted_text,
        parser_version=parser_version,
        rendered_pages=rendered_pages,
    )


def attachment_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_attachment_name(value: str) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or len(name) > 180:
        raise ValueError("attachment filename is missing or too long")
    if name in {".", ".."} or any(ord(character) < 32 for character in name):
        raise ValueError("attachment filename is invalid")
    return name


def _detect_media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "audio/mp4"
    if data.startswith(b"ID3") or (
        len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
    ):
        return "audio/mpeg"
    return ""


def _compatible_media_types(detected: str, declared: str) -> bool:
    aliases = {
        "image/jpg": "image/jpeg",
        "audio/x-wav": "audio/wav",
        "audio/wave": "audio/wav",
        "application/x-pdf": "application/pdf",
    }
    return aliases.get(declared, declared) == detected


def _render_pdf_pages(data: bytes) -> list[RenderedPage]:
    executable = shutil.which("pdftoppm")
    if not executable:
        raise RuntimeError("pdftoppm is required for visual PDF perception")
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.pdf"
        prefix = Path(directory) / "page"
        source.write_bytes(data)
        process = subprocess.Popen(
            [
                executable,
                "-f",
                "1",
                "-l",
                str(MAX_RENDERED_PDF_PAGES),
                "-png",
                "-scale-to",
                "1600",
                str(source),
                str(prefix),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        deadline = time.monotonic() + 30
        while process.poll() is None:
            total = sum(
                path.stat().st_size for path in Path(directory).glob("page-*.png")
            )
            if total > MAX_RENDERED_PDF_BYTES or time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise ResourceLimitExceededError(
                    "resource_limit_exceeded: PDF visual rendering exceeded its limit"
                )
            time.sleep(0.02)
        if process.returncode:
            raise RuntimeError("PDF visual rendering failed")
        paths = sorted(
            Path(directory).glob("page-*.png"),
            key=lambda path: int(re.search(r"(\d+)$", path.stem).group(1)),
        )
        pages = [
            RenderedPage(page=index, media_type="image/png", data=path.read_bytes())
            for index, path in enumerate(paths, start=1)
        ]
        if sum(len(item.data) for item in pages) > MAX_RENDERED_PDF_BYTES:
            raise ResourceLimitExceededError(
                "resource_limit_exceeded: rendered PDF pages exceeded 12 MB"
            )
        return pages
