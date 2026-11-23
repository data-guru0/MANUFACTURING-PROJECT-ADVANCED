"""File safety (S3): ClamAV, the REAL file type (libmagic, not the name or the email's Content-Type), and
ZIP limits. Facts are measured here; whether a file may go on is OPA's decision (apex/gate/file_reasons,
limits in policy.yaml -> gate.files). A file with any reason is quarantined: moved to quarantine/ in MinIO
and never parsed again.
"""

import io
import socket
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import magic


class ScannerDown(Exception):
    """ClamAV can't be reached: the job waits and retries. Files are never passed unscanned."""


def clamav(host: str, port: int, data: bytes) -> str | None:
    """The signature name if ClamAV finds something, else None (clamd INSTREAM protocol, stdlib only)."""
    try:
        with socket.create_connection((host, port), timeout=120) as s:
            s.sendall(b"zINSTREAM\0")
            for i in range(0, len(data), 1 << 16):
                chunk = data[i : i + (1 << 16)]
                s.sendall(struct.pack(">I", len(chunk)) + chunk)
            s.sendall(struct.pack(">I", 0))
            reply = b""
            while not reply.endswith(b"\0"):
                if not (part := s.recv(4096)):
                    break
                reply += part
    except OSError as e:
        raise ScannerDown(f"clamd {host}:{port}: {e}") from e
    text = reply.rstrip(b"\0").decode(errors="replace")  # "stream: OK" | "stream: Eicar-Test-Signature FOUND"
    if text.endswith("FOUND"):
        return text.removeprefix("stream:").removesuffix("FOUND").strip()
    if not text.endswith("OK"):
        raise ScannerDown(f"clamd answered {text!r}")
    return None


def true_type(data: bytes) -> str:
    t = str(magic.from_buffer(data[: 1 << 16], mime=True))
    # ponytail: libmagic 5.46 (Debian trixie) reports every ZIP as plain "data"; confirm ZIPs structurally with the
    # stdlib (a valid central directory) instead. Drop this once the image's libmagic recognises ZIPs again.
    if t == "application/octet-stream" and zipfile.is_zipfile(io.BytesIO(data)):
        t = "application/zip"
    return t


@dataclass
class FileFacts:
    filename: str
    ext: str
    true_type: str
    virus: str | None
    zip_problems: list[str] = field(default_factory=list)
    members: list[tuple[str, bytes]] = field(default_factory=list)  # a safe ZIP's files, to check one by one
    size_mb: float = 0.0  # S9: OPA refuses files above policy.yaml -> gate.files.max_file_mb

    def opa_input(self, depth: int) -> dict[str, Any]:
        return {"filename": self.filename, "ext": self.ext, "true_type": self.true_type, "virus": self.virus,
                "zip_problems": self.zip_problems, "depth": depth, "size_mb": round(self.size_mb, 2)}  # fmt: skip


def inspect_zip(data: bytes, limits: dict[str, Any]) -> tuple[list[str], list[tuple[str, bytes]]]:
    """Problems found WITHOUT extracting (sizes come from the ZIP directory), then the members if none."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ["not a readable ZIP"], []
    infos = [i for i in z.infolist() if not i.is_dir()]
    problems = []
    total = sum(i.file_size for i in infos)
    if len(infos) > limits["max_files"]:
        problems.append(f"{len(infos)} files (limit {limits['max_files']})")
    if total > limits["max_total_mb"] * 1_000_000:
        problems.append(f"{total / 1e6:.0f} MB unpacked (limit {limits['max_total_mb']} MB)")
    if total > limits["max_ratio"] * max(len(data), 1):
        problems.append(f"compression ratio {total / max(len(data), 1):.0f}:1 (zip bomb? limit {limits['max_ratio']}:1)")
    if any(i.flag_bits & 0x1 for i in infos):
        problems.append("encrypted members (cannot be scanned)")
    if any(PurePosixPath(i.filename).is_absolute() or ".." in PurePosixPath(i.filename).parts for i in infos):
        problems.append("path traversal in member names")
    if problems:
        return problems, []
    return [], [(PurePosixPath(i.filename).name, z.read(i)) for i in infos]


def facts(filename: str, data: bytes, clamd: tuple[str, int], zip_limits: dict[str, Any]) -> FileFacts:
    ext = PurePosixPath(filename.lower()).suffix.lstrip(".")
    f = FileFacts(filename, ext, true_type(data), clamav(*clamd, data), size_mb=len(data) / 1_000_000)
    if f.true_type == "application/zip" and not f.virus:
        f.zip_problems, f.members = inspect_zip(data, zip_limits)
    return f
