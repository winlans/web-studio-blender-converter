#!/usr/bin/env python3
"""Runpod Serverless handler for standardizing temporary GLB assets."""

from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


GLB_MAGIC = b"glTF"
JSON_CHUNK = 0x4E4F534A
DEFAULT_MAX_INPUT_BYTES = 512 * 1024 * 1024
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ModelConvertError(RuntimeError):
    """A concise error that is safe to expose through the Runpod job result."""


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _output_prefix() -> str:
    return os.getenv("MODEL_CONVERT_OUTPUT_PREFIX", "model-convert/output").strip().strip("/")


def expected_output_key(task_id: str) -> str:
    prefix = _output_prefix()
    if not prefix:
        raise RuntimeError("MODEL_CONVERT_OUTPUT_PREFIX must not be empty")
    return f"{prefix}/{task_id}.glb"


def validate_job_input(job: dict[str, Any]) -> tuple[str, str, str]:
    payload = job.get("input")
    if not isinstance(payload, dict):
        raise ModelConvertError("input must be an object")

    task_id = str(payload.get("task_id", "")).strip()
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ModelConvertError("task_id is invalid")

    source_url = str(payload.get("source_url", "")).strip()
    try:
        parsed = urllib.parse.urlsplit(source_url)
    except ValueError as exc:
        raise ModelConvertError("source_url is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelConvertError("source_url must be an http(s) URL")

    output_key = str(payload.get("output_key", "")).strip()
    required_output_key = expected_output_key(task_id)
    if output_key != required_output_key:
        raise ModelConvertError("output_key does not match task_id")
    return task_id, source_url, output_key


def download(source_url: str, destination: Path, max_bytes: int, timeout_seconds: int) -> int:
    request = urllib.request.Request(
        source_url,
        headers={
            "Accept": "model/gltf-binary,application/octet-stream;q=0.9,*/*;q=0.1",
            "User-Agent": "cysta-model-converter/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            length_header = response.headers.get("Content-Length")
            if length_header:
                try:
                    declared_length = int(length_header)
                except ValueError as exc:
                    raise ModelConvertError("source returned an invalid Content-Length") from exc
                if declared_length > max_bytes:
                    raise ModelConvertError("source GLB exceeds the maximum allowed size")

            total = 0
            with destination.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ModelConvertError("source GLB exceeds the maximum allowed size")
                    output.write(chunk)
    except ModelConvertError:
        destination.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        raise ModelConvertError(f"source download failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise ModelConvertError("source download failed") from exc

    if total == 0:
        destination.unlink(missing_ok=True)
        raise ModelConvertError("source GLB is empty")
    return total


def _read_glb_json(path: Path) -> dict[str, Any]:
    file_size = path.stat().st_size
    if file_size < 20:
        raise ModelConvertError("GLB is too small")

    with path.open("rb") as stream:
        header = stream.read(12)
        magic, version, declared_length = struct.unpack("<4sII", header)
        if magic != GLB_MAGIC or version != 2 or declared_length != file_size:
            raise ModelConvertError("invalid GLB header")

        while stream.tell() < file_size:
            chunk_header = stream.read(8)
            if len(chunk_header) != 8:
                raise ModelConvertError("invalid GLB chunk header")
            chunk_length, chunk_type = struct.unpack("<II", chunk_header)
            if chunk_length < 0 or stream.tell() + chunk_length > file_size:
                raise ModelConvertError("invalid GLB chunk length")
            chunk = stream.read(chunk_length)
            if chunk_type == JSON_CHUNK:
                try:
                    parsed = json.loads(chunk.rstrip(b" \t\r\n\x00").decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ModelConvertError("invalid GLB JSON chunk") from exc
                if not isinstance(parsed, dict):
                    raise ModelConvertError("invalid GLB JSON document")
                return parsed
    raise ModelConvertError("GLB JSON chunk is missing")


def validate_input_glb(path: Path) -> None:
    _read_glb_json(path)


def _index(items: Any, index: Any, name: str) -> dict[str, Any]:
    if not isinstance(items, list) or not isinstance(index, int) or not 0 <= index < len(items):
        raise ModelConvertError(f"output GLB contains an invalid {name} reference")
    value = items[index]
    if not isinstance(value, dict):
        raise ModelConvertError(f"output GLB contains an invalid {name}")
    return value


def validate_output_glb(path: Path) -> None:
    document = _read_glb_json(path)
    meshes = document.get("meshes")
    if not isinstance(meshes, list) or not meshes:
        raise ModelConvertError("output GLB contains no meshes")

    primitive_count = 0
    for mesh in meshes:
        primitives = mesh.get("primitives") if isinstance(mesh, dict) else None
        if not isinstance(primitives, list):
            raise ModelConvertError("output GLB contains an invalid mesh")
        for primitive in primitives:
            primitive_count += 1
            if not isinstance(primitive, dict) or primitive.get("mode", 4) != 4:
                raise ModelConvertError("output GLB contains a non-triangle primitive")
            attributes = primitive.get("attributes")
            if not isinstance(attributes, dict) or not {"POSITION", "NORMAL", "TEXCOORD_0"}.issubset(attributes):
                raise ModelConvertError("output GLB primitive is missing required attributes")
            if not isinstance(primitive.get("indices"), int):
                raise ModelConvertError("output GLB primitive is missing indices")

            material = _index(document.get("materials"), primitive.get("material"), "material")
            pbr = material.get("pbrMetallicRoughness")
            texture_info = pbr.get("baseColorTexture") if isinstance(pbr, dict) else None
            if not isinstance(texture_info, dict):
                raise ModelConvertError("output GLB material is missing baseColorTexture")
            texture = _index(document.get("textures"), texture_info.get("index"), "texture")
            image = _index(document.get("images"), texture.get("source"), "image")
            if not isinstance(image.get("bufferView"), int):
                raise ModelConvertError("output GLB base color image is not embedded")

    if primitive_count == 0:
        raise ModelConvertError("output GLB contains no primitives")


def _log_tail(path: Path, max_bytes: int = 8192) -> str:
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - max_bytes))
            return stream.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def run_blender(source: Path, output: Path, log_path: Path) -> None:
    blender_bin = os.getenv("BLENDER_BIN", "/opt/blender/blender").strip()
    script_path = os.getenv(
        "BLENDER_SCRIPT",
        str(Path(__file__).with_name("blender_standardize_glb.py")),
    ).strip()
    timeout_seconds = _positive_int_env("BLENDER_TIMEOUT_SECONDS", 300)
    resolution = _positive_int_env("BLENDER_TEXTURE_RESOLUTION", 2048)

    command = [
        blender_bin,
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python",
        script_path,
        "--",
        str(source),
        str(output),
        "--resolution",
        str(resolution),
        "--margin",
        "16",
        "--samples",
        "1",
        "--uv",
        "preserve",
        "--apply-modifiers",
        "--bake-mode",
        "color",
    ]
    try:
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise ModelConvertError("Blender conversion timed out") from exc
    except FileNotFoundError as exc:
        raise ModelConvertError("Blender executable or conversion script was not found") from exc
    except OSError as exc:
        raise ModelConvertError("Blender could not be started") from exc

    if return_code != 0:
        tail = _log_tail(log_path)
        if tail:
            print(f"[model-convert] Blender failed:\n{tail}", flush=True)
        raise ModelConvertError("Blender conversion failed")
    if not output.is_file():
        raise ModelConvertError("Blender did not produce an output GLB")


def _s3_client() -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required") from exc

    endpoint = os.getenv("S3_ENDPOINT", os.getenv("AWS_ENDPOINT_URL_S3", "")).strip() or None
    kwargs: dict[str, Any] = {
        "region_name": os.getenv("S3_REGION", os.getenv("AWS_REGION", "us-east-1")).strip(),
    }
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    access_key = os.getenv("S3_ACCESS_KEY_ID", os.getenv("AWS_ACCESS_KEY_ID", "")).strip()
    secret_key = os.getenv("S3_SECRET_ACCESS_KEY", os.getenv("AWS_SECRET_ACCESS_KEY", "")).strip()
    session_token = os.getenv("S3_SESSION_TOKEN", os.getenv("AWS_SESSION_TOKEN", "")).strip()
    if access_key:
        kwargs["aws_access_key_id"] = access_key
    if secret_key:
        kwargs["aws_secret_access_key"] = secret_key
    if session_token:
        kwargs["aws_session_token"] = session_token
    return boto3.client("s3", **kwargs)


def _public_url(output_key: str) -> str:
    base = _required_env("S3_PUBLIC_BASE_URL").rstrip("/")
    return f"{base}/{urllib.parse.quote(output_key, safe='/')}"


def existing_output(client: Any, bucket: str, output_key: str) -> dict[str, Any] | None:
    try:
        response = client.head_object(Bucket=bucket, Key=output_key)
    except Exception as exc:
        response_metadata = getattr(exc, "response", {})
        error = response_metadata.get("Error", {}) if isinstance(response_metadata, dict) else {}
        if error.get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise ModelConvertError("could not check existing output") from exc

    size = int(response.get("ContentLength") or 0)
    if size < 20:
        return None
    return {
        "output_url": _public_url(output_key),
        "output_key": output_key,
        "output_size": size,
        "reused": True,
    }


def convert(job: dict[str, Any]) -> dict[str, Any]:
    task_id, source_url, output_key = validate_job_input(job)
    bucket = _required_env("S3_BUCKET")
    client = _s3_client()

    reused = existing_output(client, bucket, output_key)
    if reused is not None:
        return {"task_id": task_id, **reused}

    max_input_bytes = _positive_int_env("MODEL_CONVERT_MAX_INPUT_BYTES", DEFAULT_MAX_INPUT_BYTES)
    download_timeout = _positive_int_env("MODEL_CONVERT_DOWNLOAD_TIMEOUT_SECONDS", 120)
    work_root = Path(os.getenv("MODEL_CONVERT_WORK_ROOT", "/tmp/model-convert"))
    work_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"{task_id}-", dir=work_root) as temporary:
        work_dir = Path(temporary)
        source = work_dir / "input.glb"
        output = work_dir / "output.glb"
        log_path = work_dir / "blender.log"

        download(source_url, source, max_input_bytes, download_timeout)
        validate_input_glb(source)
        run_blender(source, output, log_path)
        validate_output_glb(output)

        try:
            client.upload_file(
                str(output),
                bucket,
                output_key,
                ExtraArgs={
                    "ContentType": "model/gltf-binary",
                    "CacheControl": "private, max-age=86400",
                },
            )
        except Exception as exc:
            raise ModelConvertError("output upload failed") from exc

        return {
            "task_id": task_id,
            "output_url": _public_url(output_key),
            "output_key": output_key,
            "output_size": output.stat().st_size,
            "reused": False,
        }


def handler(job: dict[str, Any]) -> dict[str, Any]:
    try:
        return convert(job)
    except ModelConvertError:
        raise
    except Exception as exc:
        print(f"[model-convert] unexpected error: {type(exc).__name__}: {exc}", flush=True)
        raise ModelConvertError("model conversion failed") from exc


if __name__ == "__main__":
    import runpod

    runpod.serverless.start({"handler": handler})
