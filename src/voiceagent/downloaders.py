from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from urllib import error, request
from uuid import uuid4


@dataclass(slots=True)
class DownloadFile:
    url: str
    destination: Path
    size_bytes: int


@dataclass(slots=True)
class DownloadProgress:
    completed_bytes: int
    total_bytes: int
    download_speed_bytes_per_second: int


class AriaDownloader:
    def __init__(self, connections: int = 10) -> None:
        self.connections = connections
        self._logger = logging.getLogger(__name__)

    def download(
        self,
        files: list[DownloadFile],
        progress_callback=None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not files:
            return

        aria2c = shutil.which("aria2c")
        if aria2c is None:
            raise RuntimeError("aria2c is not installed. Install aria2 to enable model downloads.")

        for file in files:
            file.destination.parent.mkdir(parents=True, exist_ok=True)

        fd, input_file_raw = tempfile.mkstemp(prefix="voiceagent-aria2-", suffix=".txt")
        input_file = Path(input_file_raw)
        rpc_port = self._reserve_port()
        rpc_secret = uuid4().hex
        try:
            os.close(fd)
            input_file.write_text(self._build_input_file(files), encoding="utf-8")
            args = [
                aria2c,
                "--continue=true",
                "--max-connection-per-server",
                str(self.connections),
                "--split",
                str(self.connections),
                "--min-split-size",
                "1M",
                "--file-allocation=none",
                "--auto-file-renaming=false",
                "--allow-overwrite=true",
                "--disable-ipv6=true",
                "--summary-interval=0",
                "--console-log-level=warn",
                "--enable-rpc=true",
                "--rpc-listen-all=false",
                "--rpc-listen-port",
                str(rpc_port),
                "--rpc-secret",
                rpc_secret,
                "--dir",
                str(files[0].destination.parent),
                "--input-file",
                str(input_file),
            ]

            if headers:
                for key, value in headers.items():
                    args.extend(["--header", f"{key}: {value}"])

            total_bytes = sum(file.size_bytes for file in files)
            progress_callback = progress_callback or (lambda progress: None)
            progress_callback(
                DownloadProgress(
                    completed_bytes=self._current_size(files),
                    total_bytes=total_bytes,
                    download_speed_bytes_per_second=0,
                )
            )

            self._logger.info("Starting aria2 download file_count=%s total_bytes=%s", len(files), total_bytes)
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                while proc.poll() is None:
                    progress = self._poll_progress(rpc_port, rpc_secret, total_bytes)
                    if progress is None:
                        progress = DownloadProgress(
                            completed_bytes=self._current_size(files),
                            total_bytes=total_bytes,
                            download_speed_bytes_per_second=0,
                        )
                    progress_callback(progress)
                    time.sleep(0.2)
            finally:
                stdout, stderr = proc.communicate()

            progress_callback(
                DownloadProgress(
                    completed_bytes=self._current_size(files),
                    total_bytes=total_bytes,
                    download_speed_bytes_per_second=0,
                )
            )
            if proc.returncode != 0:
                raise RuntimeError((stderr or stdout or "aria2 download failed").strip())

            missing_files = [str(file.destination) for file in files if not file.destination.exists()]
            if missing_files:
                raise RuntimeError(f"aria2 completed without creating expected files: {', '.join(missing_files)}")

            self._logger.info("aria2 download completed file_count=%s total_bytes=%s", len(files), total_bytes)
        finally:
            input_file.unlink(missing_ok=True)

    def get_remote_size(self, url: str, headers: dict[str, str] | None = None) -> int:
        req = request.Request(url, method="HEAD")
        for key, value in (headers or {}).items():
            req.add_header(key, value)

        with request.urlopen(req, timeout=60) as response:
            raw_size = response.headers.get("x-linked-size") or response.headers.get("Content-Length")

        if raw_size is None:
            raise RuntimeError(f"Could not determine remote size for {url}")

        return int(raw_size)

    def _build_input_file(self, files: list[DownloadFile]) -> str:
        lines: list[str] = []
        for file in files:
            lines.append(file.url)
            lines.append(f" out={file.destination.name}")
        return "\n".join(lines) + "\n"

    def _current_size(self, files: list[DownloadFile]) -> int:
        return sum(file.destination.stat().st_size for file in files if file.destination.exists())

    def _poll_progress(self, rpc_port: int, rpc_secret: str, total_bytes: int) -> DownloadProgress | None:
        keys = ["completedLength", "totalLength", "downloadSpeed"]
        downloads = []
        downloads.extend(self._rpc_call(rpc_port, rpc_secret, "aria2.tellActive", [keys]) or [])
        downloads.extend(self._rpc_call(rpc_port, rpc_secret, "aria2.tellWaiting", [0, 1000, keys]) or [])
        downloads.extend(self._rpc_call(rpc_port, rpc_secret, "aria2.tellStopped", [0, 1000, keys]) or [])
        if not downloads:
            return None

        completed_bytes = sum(int(item.get("completedLength", "0")) for item in downloads)
        reported_total_bytes = sum(int(item.get("totalLength", "0")) for item in downloads)
        download_speed = sum(int(item.get("downloadSpeed", "0")) for item in downloads)
        return DownloadProgress(
            completed_bytes=completed_bytes,
            total_bytes=reported_total_bytes or total_bytes,
            download_speed_bytes_per_second=download_speed,
        )

    def _rpc_call(self, rpc_port: int, rpc_secret: str, method: str, params: list[object]) -> object | None:
        payload = {
            "jsonrpc": "2.0",
            "id": method,
            "method": method,
            "params": [f"token:{rpc_secret}", *params],
        }
        req = request.Request(
            f"http://127.0.0.1:{rpc_port}/jsonrpc",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=0.5) as response:
                data = json.load(response)
        except (ConnectionError, TimeoutError, error.URLError):
            return None

        return data.get("result")

    def _reserve_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return int(sock.getsockname()[1])


def format_bytes(num_bytes: int) -> str:
    value = float(max(num_bytes, 0))
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return "0 B"


def format_transfer_rate(num_bytes_per_second: int) -> str:
    return f"{format_bytes(num_bytes_per_second)}/s"
