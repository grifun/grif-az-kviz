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
        self.env_names: dict[str, str] = {}   # which environment variable each setting came from (for the log)

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


# Accepted names for each setting. Names match loosely: case, "_", "-" and spaces are ignored,
# so S3FOLDER, S3_FOLDER and s3folder are all the same variable.
S3_VARS = {
    "folder": ("S3FOLDER",),
    "key": ("S3_ACCESS_KEY_ID", "S3_ACCESS_KEY", "S3_KEY", "FILEBASE_KEY", "ACCESS_KEY_ID"),
    "secret": ("S3_SECRET_ACCESS_KEY", "S3_SECRET_KEY", "S3_SECRET", "FILEBASE_SECRET", "SECRET_ACCESS_KEY"),
    "endpoint": ("S3_ENDPOINT",),
    "region": ("S3_REGION",),
}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.upper() if ch.isalnum())


def _clean(value: str) -> str:
    """Trim whitespace and surrounding quotes, e.g. a value pasted as 'https://s3.filebase.io'."""
    value = value.strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def _lookup(env, setting: str) -> tuple[str, str]:
    """(value, variable name used) for a setting, or ("", "") if none of its names is set."""
    wanted = [_norm(n) for n in S3_VARS[setting]]
    for want in wanted:              # in order of preference
        for name, value in env.items():
            if _norm(name) == want and _clean(value):
                return _clean(value), name
    return "", ""


def s3_related_names(env=os.environ) -> list[str]:
    """Names (never values) of environment variables that look storage-related, for diagnostics."""
    return sorted(n for n in env if any(w in n.upper() for w in ("S3", "FILEBASE", "BUCKET", "AWS_")))


def s3_config_from_env(env=os.environ) -> dict | None:
    """S3 settings if S3FOLDER ("bucket" or "bucket/folder") is set, otherwise None.

    Returns {"bucket", "prefix", "client_kwargs", "names"}; "names" maps each setting to the
    variable it came from, so the startup log can say exactly what was used.
    """
    found = {k: _lookup(env, k) for k in S3_VARS}
    folder = found["folder"][0].strip("/")
    if not folder:
        return None
    bucket, _, prefix = folder.partition("/")
    kwargs = {"endpoint_url": found["endpoint"][0] or "https://s3.filebase.io",
              "region_name": found["region"][0] or "auto"}
    if found["key"][0]:
        kwargs["aws_access_key_id"] = found["key"][0]
    if found["secret"][0]:
        kwargs["aws_secret_access_key"] = found["secret"][0]
    names = {k: v[1] for k, v in found.items() if v[1]}
    return {"bucket": bucket, "prefix": prefix, "client_kwargs": kwargs, "names": names}


def s3_backend_from_env(env=os.environ) -> S3Backend | None:
    """S3 storage if S3FOLDER is set (``bucket`` or ``bucket/folder``), otherwise None.

    Credentials and endpoint: S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY (or the standard AWS_* variables),
    S3_ENDPOINT (default Filebase), S3_REGION (default "auto").
    """
    cfg = s3_config_from_env(env)
    if cfg is None:
        return None
    backend = S3Backend(cfg["bucket"], cfg["prefix"], **cfg["client_kwargs"])
    backend.env_names = cfg["names"]
    return backend

