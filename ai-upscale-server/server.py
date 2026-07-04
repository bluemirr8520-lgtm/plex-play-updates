#!/usr/bin/env python3
"""Small Plex/Real-ESRGAN job server used by Plex Play."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = os.environ.get("AI_HOST", "0.0.0.0")
PORT = int(os.environ.get("AI_PORT", "32600"))
PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/var/lib/plex-ai-upscale"))
WORK_DIR = Path(os.environ.get("WORK_DIR", str(OUTPUT_DIR / "work")))
REALESRGAN_BIN = Path(
    os.environ.get(
        "REALESRGAN_BIN",
        "/opt/plex-ai-upscale/realesrgan/realesrgan-ncnn-vulkan",
    )
)
REALESRGAN_MODEL_DIR = Path(
    os.environ.get("REALESRGAN_MODEL_DIR", str(REALESRGAN_BIN.parent / "models"))
)
MODEL = os.environ.get("AI_MODEL", "realesrgan-x4plus")
TILE_SIZE = os.environ.get("AI_TILE_SIZE", "128")
MAX_CACHE_BYTES = int(float(os.environ.get("MAX_CACHE_GB", "100")) * 1024**3)
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

JOBS: dict[str, dict[str, object]] = {}
JOBS_LOCK = threading.Lock()
JOB_QUEUE: queue.Queue[str] = queue.Queue()
VALID_CLIENT_TOKENS: dict[str, float] = {}
VALID_CLIENT_TOKENS_LOCK = threading.Lock()


def response_for(rating_key: str) -> dict[str, object] | None:
    output = output_path(rating_key)
    if output.is_file():
        return {
            "ratingKey": rating_key,
            "status": "ready",
            "progress": 100,
            "streamUrl": f"/v1/stream/{urllib.parse.quote(rating_key)}.mp4",
        }
    with JOBS_LOCK:
        job = JOBS.get(rating_key)
        return dict(job) if job else None


def output_path(rating_key: str) -> Path:
    return OUTPUT_DIR / f"{rating_key}.mp4"


def update_job(rating_key: str, **values: object) -> None:
    with JOBS_LOCK:
        current = JOBS.setdefault(
            rating_key,
            {"ratingKey": rating_key, "status": "queued", "progress": 0},
        )
        current.update(values)


def plex_source(rating_key: str) -> str:
    request = urllib.request.Request(
        f"{PLEX_URL}/library/metadata/{urllib.parse.quote(rating_key)}",
        headers={"X-Plex-Token": PLEX_TOKEN, "Accept": "application/xml"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        root = ET.parse(response).getroot()
    part = root.find(".//Part")
    if part is None:
        raise RuntimeError("Plex metadata has no playable Part")
    local_file = part.get("file", "")
    if local_file and Path(local_file).is_file():
        return local_file
    part_key = part.get("key", "")
    if not part_key:
        raise RuntimeError("Plex metadata has no Part key")
    separator = "&" if "?" in part_key else "?"
    return f"{PLEX_URL}{part_key}{separator}X-Plex-Token={urllib.parse.quote(PLEX_TOKEN)}"


def video_info(source: str) -> tuple[str, int]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate,nb_frames",
        "-of",
        "json",
        source,
    ]
    data = json.loads(subprocess.check_output(command, text=True, timeout=120))
    stream = data["streams"][0]
    frame_rate = stream.get("avg_frame_rate") or "24000/1001"
    frame_count = int(stream.get("nb_frames") or 0)
    return frame_rate, frame_count


def run_checked(command: list[str], log) -> None:
    log.write("$ " + " ".join(redact_token(value) for value in command) + "\n")
    log.flush()
    subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)


def redact_token(value: str) -> str:
    return value.replace(PLEX_TOKEN, "***") if PLEX_TOKEN else value


def process_job(rating_key: str) -> None:
    job_dir = WORK_DIR / rating_key
    input_frames = job_dir / "input"
    output_frames = job_dir / "output"
    temporary_output = job_dir / "result.mp4"
    log_path = OUTPUT_DIR / f"{rating_key}.log"
    try:
        update_job(rating_key, status="processing", progress=1)
        source = plex_source(rating_key)
        frame_rate, expected_frames = video_info(source)
        input_frames.mkdir(parents=True, exist_ok=True)
        output_frames.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            update_job(rating_key, progress=3)
            run_checked(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    source,
                    "-map",
                    "0:v:0",
                    "-vsync",
                    "0",
                    str(input_frames / "%08d.png"),
                ],
                log,
            )
            total_frames = expected_frames or sum(1 for _ in input_frames.glob("*.png"))
            update_job(rating_key, progress=12)
            command = [
                str(REALESRGAN_BIN),
                "-i",
                str(input_frames),
                "-o",
                str(output_frames),
                "-m",
                str(REALESRGAN_MODEL_DIR),
                "-n",
                MODEL,
                "-s",
                "2",
                "-t",
                TILE_SIZE,
                "-g",
                "0",
                "-j",
                "1:1:1",
                "-f",
                "png",
            ]
            log.write("$ " + " ".join(command) + "\n")
            log.flush()
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            while process.poll() is None:
                completed = sum(1 for _ in output_frames.glob("*.png"))
                if total_frames > 0:
                    update_job(
                        rating_key,
                        progress=min(88, 12 + int(completed * 76 / total_frames)),
                    )
                time.sleep(2)
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command)
            update_job(rating_key, progress=90)
            run_checked(
                [
                    "ffmpeg",
                    "-y",
                    "-framerate",
                    frame_rate,
                    "-i",
                    str(output_frames / "%08d.png"),
                    "-i",
                    source,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a?",
                    "-map_metadata",
                    "1",
                    "-vf",
                    "scale=w='min(3840,iw)':h='min(2160,ih)':force_original_aspect_ratio=decrease",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(temporary_output),
                ],
                log,
            )
        evict_cache(temporary_output.stat().st_size)
        os.replace(temporary_output, output_path(rating_key))
        update_job(rating_key, status="ready", progress=100)
    except Exception as error:
        update_job(
            rating_key,
            status="failed",
            progress=0,
            error=str(error)[:300],
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def evict_cache(required_bytes: int) -> None:
    outputs = sorted(
        OUTPUT_DIR.glob("*.mp4"),
        key=lambda path: path.stat().st_atime,
    )
    current_size = sum(path.stat().st_size for path in outputs)
    while outputs and current_size + required_bytes > MAX_CACHE_BYTES:
        oldest = outputs.pop(0)
        size = oldest.stat().st_size
        oldest.unlink(missing_ok=True)
        current_size -= size


def worker() -> None:
    while True:
        rating_key = JOB_QUEUE.get()
        try:
            process_job(rating_key)
        finally:
            JOB_QUEUE.task_done()


def plex_token_is_valid(token: str) -> bool:
    if not token:
        return False
    if hmac.compare_digest(token, PLEX_TOKEN):
        return True
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    with VALID_CLIENT_TOKENS_LOCK:
        if VALID_CLIENT_TOKENS.get(fingerprint, 0) > now:
            return True
    request = urllib.request.Request(
        f"{PLEX_URL}/library/sections",
        headers={"X-Plex-Token": token, "Accept": "application/xml"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            valid = response.status == HTTPStatus.OK
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        valid = False
    if valid:
        with VALID_CLIENT_TOKENS_LOCK:
            VALID_CLIENT_TOKENS[fingerprint] = now + 300
    return valid


class Handler(BaseHTTPRequestHandler):
    server_version = "PlexAiUpscale/1.0"

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self.send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "engine": "Real-ESRGAN NCNN Vulkan",
                    "model": MODEL,
                },
            )
            return
        if not self.authorized():
            return
        match = re.fullmatch(r"/v1/upscale/([^/]+)", path)
        if match:
            rating_key = urllib.parse.unquote(match.group(1))
            if not valid_key(rating_key):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid rating key"})
                return
            job = response_for(rating_key)
            self.send_json(
                HTTPStatus.OK if job else HTTPStatus.NOT_FOUND,
                job or {"status": "missing"},
            )
            return
        stream_match = re.fullmatch(r"/v1/stream/([^/]+)\.mp4", path)
        if stream_match:
            rating_key = urllib.parse.unquote(stream_match.group(1))
            if valid_key(rating_key):
                self.send_file(output_path(rating_key))
                return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if not self.authorized():
            return
        match = re.fullmatch(r"/v1/upscale/([^/]+)", path)
        if not match:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        rating_key = urllib.parse.unquote(match.group(1))
        if not valid_key(rating_key):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid rating key"})
            return
        existing = response_for(rating_key)
        if existing:
            self.send_json(
                HTTPStatus.OK if existing.get("status") == "ready" else HTTPStatus.ACCEPTED,
                existing,
            )
            return
        update_job(rating_key, status="queued", progress=0)
        JOB_QUEUE.put(rating_key)
        self.send_json(HTTPStatus.ACCEPTED, response_for(rating_key))

    def authorized(self) -> bool:
        supplied = self.headers.get("X-Plex-Token", "")
        if plex_token_is_valid(supplied):
            return True
        self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def send_json(self, status: HTTPStatus, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            values = range_header[6:].split("-", 1)
            try:
                start = int(values[0]) if values[0] else 0
                end = int(values[1]) if values[1] else end
                end = min(end, size - 1)
                if start > end:
                    raise ValueError
            except ValueError:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            status = HTTPStatus.PARTIAL_CONTENT
        else:
            status = HTTPStatus.OK
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        os.utime(path, None)

    def log_message(self, fmt: str, *args: object) -> None:
        print(
            f"{self.address_string()} - {fmt % args}",
            flush=True,
        )


def valid_key(value: str) -> bool:
    return bool(KEY_PATTERN.fullmatch(value))


def main() -> None:
    if not PLEX_TOKEN:
        raise SystemExit("PLEX_TOKEN is required")
    for executable in ("ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise SystemExit(f"{executable} was not found")
    if not REALESRGAN_BIN.is_file():
        raise SystemExit(f"Real-ESRGAN was not found: {REALESRGAN_BIN}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, name="upscale-worker", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Plex AI upscale server listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
