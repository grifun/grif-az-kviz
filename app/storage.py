"""Where question-set files live: a local folder, or a folder in an S3 bucket (e.g. Filebase).

Both backends store one JSON file per set and expose the same small interface, used by SetStore:
list() -> {file name: version token}, read(name), write(name, text), trash(name), where(name).
Only top-level *.json files count; deleted sets go to a ".deleted/" subfolder.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger("azkviz.sets")


class StorageError(Exception):
    """The storage could not be read or written (missing folder, S3 error, network...)."""


def _trash_name(name: str) -> str:
    return f"{Path(name).stem}-{time.strftime('%Y%m%d-%H%M%S')}.json"


class LocalBackend:
    min_interval = 0.0          # local files are cheap to check on every refresh

    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.location = str(self.dir)

    def list(self) -> dict[str, str]:
        out = {}
        try:
            files = list(self.dir.glob("*.json"))
        except OSError as e:
            raise StorageError(str(e)) from e
        for f in files:
            try:
                st = f.stat()
            except OSError:          # removed between listing and stat
                continue
            out[f.name] = f"{st.st_mtime_ns}-{st.st_size}"
        return out

    def read(self, name: str) -> str:
        try:
            return (self.dir / name).read_text(encoding="utf-8")
        except OSError as e:
            raise StorageError(str(e)) from e

    def write(self, name: str, text: str) -> None:
        path = self.dir / name
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)            # atomic: readers never see a half-written file
        except OSError as e:
            raise StorageError(str(e)) from e

    def trash(self, name: str) -> str:
        target = self.dir / ".deleted" / _trash_name(name)
        try:
            target.parent.mkdir(exist_ok=True)
            os.replace(self.dir / name, target)
        except OSError as e:
            raise StorageError(str(e)) from e
        return str(target)

    def where(self, name: str) -> str:
        return str(self.dir / name)


class S3Backend:
    min_interval = 10.0         # listing a bucket costs a request; don't do it more often than this

    def __init__(self, bucket: str, prefix: str = "", client=None, **client_kwargs):
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("s3", config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=20,
                                                      retries={"max_attempts": 3, "mode": "standard"}),
                                  **client_kwargs)
        self.client = client
        self.bucket = bucket
        prefix = prefix.strip("/")
        self.prefix = prefix + "/" if prefix else ""
        self.location = f"s3://{bucket}/{self.prefix}"

    def _call(self, what: str, fn, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return fn(**kwargs)
        except (BotoCoreError, ClientError) as e:
            raise StorageError(f"{what} {self.location}: {e}") from e

    def list(self) -> dict[str, str]:
        out = {}
        paginator = self.client.get_paginator("list_objects_v2")
        pages = self._call("list", lambda: list(paginator.paginate(Bucket=self.bucket, Prefix=self.prefix)))
        for page in pages:
            for obj in page.get("Contents", []):
                name = obj["Key"][len(self.prefix):]
                if "/" in name or not name.endswith(".json"):   # skips .deleted/ and other subfolders
                    continue
                out[name] = f"{obj['ETag'].strip(chr(34))}-{obj['Size']}"
        return out

    def read(self, name: str) -> str:
        res = self._call("read", self.client.get_object, Bucket=self.bucket, Key=self.prefix + name)
        return res["Body"].read().decode("utf-8")

    def write(self, name: str, text: str) -> None:
        self._call("write", self.client.put_object, Bucket=self.bucket, Key=self.prefix + name,
                   Body=text.encode("utf-8"), ContentType="application/json; charset=utf-8")

    def trash(self, name: str) -> str:
        key, target = self.prefix + name, f"{self.prefix}.deleted/{_trash_name(name)}"
        self._call("copy", self.client.copy_object, Bucket=self.bucket, Key=target,
                   CopySource={"Bucket": self.bucket, "Key": key})
        self._call("delete", self.client.delete_object, Bucket=self.bucket, Key=key)
        return f"s3://{self.bucket}/{target}"

    def where(self, name: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}{name}"


def s3_backend_from_env(env=os.environ) -> S3Backend | None:
    """S3 storage if S3FOLDER is set (``bucket`` or ``bucket/folder``), otherwise None.

    Credentials and endpoint: S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY (or the standard AWS_* variables),
    S3_ENDPOINT (default Filebase), S3_REGION (default "auto"). With S3_BUCKET set, S3FOLDER is
    just the folder inside that bucket.
    """
    folder = env.get("S3FOLDER", "").strip().strip("/")
    if not folder and not env.get("S3_BUCKET"):
        return None
    if env.get("S3_BUCKET"):
        bucket, prefix = env["S3_BUCKET"].strip(), folder
    else:
        bucket, _, prefix = folder.partition("/")
    kwargs = {"endpoint_url": env.get("S3_ENDPOINT") or "https://s3.filebase.io",
              "region_name": env.get("S3_REGION") or "auto"}
    if env.get("S3_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"] = env["S3_ACCESS_KEY_ID"]
    if env.get("S3_SECRET_ACCESS_KEY"):
        kwargs["aws_secret_access_key"] = env["S3_SECRET_ACCESS_KEY"]
    return S3Backend(bucket, prefix, **kwargs)


def seed_if_empty(backend, source: Path) -> int:
    """Upload the bundled set files when the storage has none yet (first start with a new bucket)."""
    if backend.list():
        return 0
    files = sorted(Path(source).glob("*.json"))
    for f in files:
        backend.write(f.name, f.read_text(encoding="utf-8"))
    if files:
        log.info("Question storage %s was empty: uploaded %d bundled set files from %s",
                 backend.location, len(files), source)
    return len(files)
