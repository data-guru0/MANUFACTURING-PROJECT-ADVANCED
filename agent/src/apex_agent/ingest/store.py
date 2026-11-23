"""MinIO, content-addressed: every object's name is the sha256 of its bytes, so writing it twice is harmless.

raw/<sha>.eml     the email exactly as received (evidence; never parsed again by the LLM path)
incoming/<sha>    an attachment not yet checked by the security gate
files/<sha>       an attachment the gate passed (S4 reads only from here)
quarantine/<sha>  an attachment the gate stopped (virus, lying type, ZIP bomb...)
"""

import hashlib
import io

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error

from apex_agent.settings import S3Settings, load_as

PREFIXES = ("incoming", "files", "quarantine")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self) -> None:
        s = load_as(S3Settings)
        self.bucket = s.bucket
        # ponytail: plain HTTP inside the internal `data` network; S19 adds TLS between services.
        self.client = Minio(s.endpoint, access_key=s.access_key, secret_key=s.secret_key.get_secret_value(), secure=False)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=content_type)

    def get(self, key: str) -> bytes:
        r = self.client.get_object(self.bucket, key)
        try:
            return r.read()
        finally:
            r.close()
            r.release_conn()

    def _has(self, key: str) -> bool:
        try:
            self.client.stat_object(self.bucket, key)
            return True
        except S3Error as e:
            if e.code in ("NoSuchKey", "NoSuchObject"):
                return False
            raise

    def attachment(self, digest: str) -> bytes:
        """An attachment's bytes, wherever the gate has put them (the same file can arrive in many emails)."""
        for prefix in PREFIXES:
            if self._has(f"{prefix}/{digest}"):
                return self.get(f"{prefix}/{digest}")
        raise FileNotFoundError(f"attachment {digest} is not in MinIO")

    def place(self, digest: str, prefix: str) -> None:
        """Put the gate's verdict into MinIO: the file ends up under files/ or quarantine/, no longer incoming/."""
        if not self._has(f"{prefix}/{digest}"):
            src = next(f"{p}/{digest}" for p in PREFIXES if self._has(f"{p}/{digest}"))
            self.client.copy_object(self.bucket, f"{prefix}/{digest}", CopySource(self.bucket, src))
        self.client.remove_object(self.bucket, f"incoming/{digest}")  # no error if it is already gone
