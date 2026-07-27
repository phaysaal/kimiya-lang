"""Content-addressed image observations for Kimiya.

The language owns provenance and freshness; decoding is deliberately narrow
and replaceable. JPEG/PNG work with the standard library. On macOS, `sips`
provides the initial external decoder for Fujifilm RAF and other Core Image
formats without adding a Python dependency.
"""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import struct
import subprocess
from pathlib import Path


MAX_IMAGES_PER_CALL = 8
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_TOTAL_PREVIEW_BYTES = 32 * 1024 * 1024
MAX_PREVIEW_EDGE = 2048


class ImageError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageError("invalid PNG header")
    return struct.unpack(">II", header[16:24])


def _jpeg_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            raise ImageError("invalid JPEG header")
        while True:
            byte = handle.read(1)
            while byte == b"\xff":
                byte = handle.read(1)
            if not byte:
                break
            marker = byte[0]
            if marker in (0xD8, 0xD9):
                continue
            raw_length = handle.read(2)
            if len(raw_length) != 2:
                break
            length = struct.unpack(">H", raw_length)[0]
            if length < 2:
                break
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            }:
                payload = handle.read(5)
                if len(payload) != 5:
                    break
                height, width = struct.unpack(">HH", payload[1:5])
                return width, height
            handle.seek(length - 2, 1)
    raise ImageError("JPEG dimensions not found")


def _dimensions(path: Path) -> tuple[int, int]:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return _png_dimensions(path)
    if suffix in (".jpg", ".jpeg"):
        return _jpeg_dimensions(path)
    raise ImageError(f"no built-in dimension reader for {suffix or 'file'}")


def _source_mime(path: Path) -> str:
    if path.suffix.lower() == ".raf":
        return "image/x-fuji-raf"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _empty(path: Path, reason: str) -> dict:
    return {
        "kind": "image", "path": str(path), "sha": "", "exists": False,
        "mtime": 0, "width": 0, "height": 0, "format": "",
        "mime": "", "preview_path": "", "preview_sha": "",
        "decoder": "", "reason": reason,
    }


def _sips_dimensions(executable: str, path: Path) -> tuple[int, int] | None:
    result = subprocess.run(
        [executable, "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return None
    values = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key in ("pixelWidth", "pixelHeight"):
            try:
                values[key] = int(value)
            except ValueError:
                pass
    if "pixelWidth" in values and "pixelHeight" in values:
        return values["pixelWidth"], values["pixelHeight"]
    return None


def _decode_with_sips(source: Path, destination: Path) -> tuple[int, int]:
    executable = shutil.which("sips")
    if not executable:
        raise ImageError(
            f"{source.suffix.upper()} needs an image decoder; macOS `sips` "
            "was not found")
    source_dimensions = _sips_dimensions(executable, source)
    resize = (
        ["-Z", str(MAX_PREVIEW_EDGE)]
        if source_dimensions and max(source_dimensions) > MAX_PREVIEW_EDGE
        else []
    )
    result = subprocess.run(
        [executable, "-s", "format", "jpeg", *resize,
         str(source), "--out", str(destination)],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0 or not destination.exists():
        detail = result.stderr.strip() or result.stdout.strip()
        raise ImageError(f"sips could not decode {source.name}: {detail[:200]}")
    return source_dimensions or _jpeg_dimensions(destination)


def observe(path_value, artifact_directory: Path) -> dict:
    """Observe one source image and create an immutable transport preview."""
    source = Path(str(path_value))
    if not source.exists() or not source.is_file():
        return _empty(source, "source image does not exist")

    try:
        source_sha = _sha(source)
    except OSError as exc:
        return _empty(source, f"source image is unreadable: {exc}")

    artifact_directory = Path(artifact_directory)
    artifact_directory.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    decoder = "stdlib-copy"

    try:
        if suffix in (".jpg", ".jpeg", ".png"):
            width, height = _dimensions(source)
            needs_resize = (
                max(width, height) > MAX_PREVIEW_EDGE
                or source.stat().st_size > MAX_PREVIEW_BYTES
            )
            if needs_resize and shutil.which("sips"):
                preview = artifact_directory / f"{source_sha[:20]}.jpg"
                decoder = "sips"
                if not preview.exists():
                    _decode_with_sips(source, preview)
            else:
                preview_suffix = ".png" if suffix == ".png" else ".jpg"
                preview = artifact_directory / (
                    f"{source_sha[:20]}{preview_suffix}")
                if not preview.exists():
                    shutil.copyfile(source, preview)
        else:
            preview = artifact_directory / f"{source_sha[:20]}.jpg"
            decoder = "sips"
            if preview.exists():
                width, height = _jpeg_dimensions(preview)
            else:
                width, height = _decode_with_sips(source, preview)
        preview_sha = _sha(preview)
    except (ImageError, OSError, subprocess.SubprocessError) as exc:
        return _empty(source, str(exc))

    return {
        "kind": "image",
        "path": str(source),
        "sha": source_sha,
        "exists": True,
        "mtime": source.stat().st_mtime,
        "width": width,
        "height": height,
        "format": suffix.lstrip(".").upper(),
        "mime": _source_mime(source),
        "preview_path": str(preview),
        "preview_sha": preview_sha,
        "decoder": decoder,
        "reason": "",
    }


def prepare(records) -> tuple[list[str], list[dict]]:
    """Validate observed records and freshness before model transmission."""
    if not isinstance(records, list):
        raise ImageError("gen images must evaluate to a list")
    if len(records) > MAX_IMAGES_PER_CALL:
        raise ImageError(
            f"gen images has {len(records)} items; maximum is "
            f"{MAX_IMAGES_PER_CALL}")

    paths: list[str] = []
    metadata: list[dict] = []
    total = 0
    for index, record in enumerate(records):
        if not isinstance(record, dict) or \
                record.get("kind") not in ("image", "screen"):
            raise ImageError(
                f"gen images item {index + 1} is not from "
                "`observe image(...)` or `observe screen(...)`")
        if not record.get("exists"):
            reason = record.get("reason") or "observation failed"
            raise ImageError(f"image {record.get('path', '')}: {reason}")
        if record["kind"] == "screen":
            # The grounded screen-read: a screenshot observation feeds gen
            # directly. Screenshots are already sized PNGs, so the source
            # doubles as its own preview; the record's SHA is a 12-hex
            # prefix, so freshness compares by prefix.
            source = Path(record["path"])
            if not source.exists() or \
                    not _sha(source).startswith(record.get("sha", "?")):
                raise ImageError(
                    f"screenshot changed after observation: {source}")
            size = source.stat().st_size
            if size > MAX_PREVIEW_BYTES:
                raise ImageError(
                    f"screenshot exceeds {MAX_PREVIEW_BYTES} bytes: "
                    f"{source}")
            total += size
            if total > MAX_TOTAL_PREVIEW_BYTES:
                raise ImageError(
                    f"image previews exceed {MAX_TOTAL_PREVIEW_BYTES} "
                    "bytes for one model call")
            paths.append(str(source))
            metadata.append({
                "path": str(source),
                "sha": record["sha"],
                "preview_sha": record["sha"],
                "decoder": "screen:" + str(record.get("driver", "")),
                "surface": "screen",
                "seat": record.get("actor", "default"),
            })
            continue
        source = Path(record["path"])
        preview = Path(record["preview_path"])
        if not source.exists() or _sha(source) != record.get("sha"):
            raise ImageError(f"image source changed after observation: {source}")
        if not preview.exists() or _sha(preview) != record.get("preview_sha"):
            raise ImageError(
                f"image preview changed after observation: {preview}")
        size = preview.stat().st_size
        if size > MAX_PREVIEW_BYTES:
            raise ImageError(
                f"image preview exceeds {MAX_PREVIEW_BYTES} bytes: {preview}")
        total += size
        if total > MAX_TOTAL_PREVIEW_BYTES:
            raise ImageError(
                f"image previews exceed {MAX_TOTAL_PREVIEW_BYTES} bytes "
                "for one model call")
        paths.append(str(preview))
        metadata.append({
            "path": str(source),
            "sha": record["sha"],
            "preview_sha": record["preview_sha"],
            "decoder": record.get("decoder", ""),
        })
    return paths, metadata
