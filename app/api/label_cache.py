from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

TTL_SECONDS = 7 * 24 * 60 * 60
MAX_WORKERS = 8
DEFAULT_DIR = Path(__file__).resolve().parent.parent.parent / ".label_cache"
ENV_VAR = "PHARMAKON_LABEL_CACHE_DIR"

_UNSET: Any = object()
_MISSING: Any = object()
_override: Any = _UNSET


def cache_dir() -> Path | None:
    if _override is not _UNSET:
        return _override
    configured = os.environ.get(ENV_VAR)
    if configured is None:
        return DEFAULT_DIR
    return Path(configured) if configured else None


def set_cache_dir(path: Path | None) -> None:
    global _override
    _override = path


def reset_cache_dir() -> None:
    global _override
    _override = _UNSET


def _path_for(root: Path, namespace: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return root / namespace / f"{digest}.json"


def _read(path: Path, now: float) -> Any:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = float(stored["fetched_at"])
        value = stored["value"]
    except (OSError, ValueError, KeyError, TypeError):
        return _MISSING
    if now - fetched_at > TTL_SECONDS or fetched_at > now + 60:
        return _MISSING
    return value


def _write(path: Path, value: Any, now: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{time.monotonic_ns()}.tmp")
        tmp.write_text(
            json.dumps({"fetched_at": now, "value": value}), encoding="utf-8"
        )
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        return


def cached(namespace: str, key: str, loader: Callable[[], Any]) -> Any:
    root = cache_dir()
    if root is None:
        return loader()
    path = _path_for(root, namespace, key)
    hit = _read(path, time.time())
    if hit is not _MISSING:
        return hit
    value = loader()
    _write(path, value, time.time())
    return value


def fetch_concurrently(
    keys: list[str], fetch: Callable[[str], Any]
) -> dict[str, Any | Exception]:
    unique = list(dict.fromkeys(keys))
    if not unique:
        return {}
    results: dict[str, Any | Exception] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(unique))) as pool:
        futures = {key: pool.submit(fetch, key) for key in unique}
        for key, future in futures.items():
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = exc
    return results
