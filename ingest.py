from __future__ import annotations

import argparse
import atexit
import base64
import builtins
import codecs
import ctypes
import difflib
import hashlib
import html
import json
import msvcrt
import os
import re
import stat
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from collections import Counter, deque
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import pypdfium2 as pdfium
import requests
import yaml
from dotenv import dotenv_values, load_dotenv
from PIL import Image
from pypdf import PdfReader, PdfWriter
from requests.adapters import HTTPAdapter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
SUPPORTED = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".html",
    ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp",
    ".md", ".txt", ".tex",
}
DIRECT_TEXT = {".md", ".txt", ".tex", ".html"}
IMAGE_RE = re.compile(r"!\[[^\]]*]\(([^)]+)\)")
SYSTEM_IMPLEMENTATION_PERCENT = 90
CONTENT_STRUCTURE_VERSION = 2
CHILD_CHUNK_VERSION = 2
MIMO_IMAGE_PROMPT_VERSION = 1
LOCAL_MODEL_LOCK = threading.RLock()
MIMO_KEY_LOCK = threading.Lock()
MIMO_KEY_CURSOR = 0
MIMO_KEY_CACHE_LOCK = threading.Lock()
MIMO_KEY_CACHE_SIGNATURE: tuple[int, int] | None = None
MIMO_KEY_CACHE: dict[str, str] = {}
MIMO_KEY_GATE_LOCK = threading.Lock()
MIMO_KEY_GATES: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
MIMO_KEY_COOLDOWNS: dict[str, float] = {}
MIMO_SESSION_LOCK = threading.Lock()
MIMO_SESSIONS: dict[str, requests.Session] = {}
HTTP_SESSION_LOCAL = threading.local()
VISION_CACHE_LOCAL = threading.local()
VERIFICATION_COUNTER_LOCK = threading.Lock()
VERIFICATION_COUNTER = 0
EMBEDDING_VERIFICATION_LOCK = threading.Lock()
EMBEDDING_VERIFIED_THIS_PROCESS: set[str] = set()
EMBEDDING_WARMED_THIS_PROCESS: set[str] = set()
OCR_ENGINE_LOCK = threading.Lock()
OCR_ENGINE = None
NEO4J_STARTED_FOR_DELETE = False
NEO4J_RUNTIME_CONFIG = {
    "wsl_distro": "Ubuntu",
    "container": "WeKnora-neo4j",
}
MEMORY_SAMPLE_LOCK = threading.Lock()
MEMORY_SAMPLES: dict[str, deque[float]] = {}
RUNTIME_RESOURCE_CONFIG: dict = {}
PRINT_LOCK = threading.Lock()
MAX_MINERU_RESULT_ZIP_BYTES = 2 * 1024**3
MAX_MINERU_RESULT_FILES = 100_000
MAX_MINERU_RESULT_EXPANDED_BYTES = 20 * 1024**3
MIN_MINERU_RESULT_FREE_BYTES = 512 * 1024**2
MAX_ARCHIVE_LIST_BYTES = 64 * 1024**2
PROVIDER_SECRET_FIELD = re.compile(
    r"(?:url|uri|token|api.?key|authorization|credential|signature|secret)",
    re.IGNORECASE,
)
PROVIDER_INLINE_SECRET = re.compile(
    r"(?i)\b(?:token|api[_-]?key|authorization|credential|signature|secret)"
    r"\s*[:=]\s*[^\s,;\"'<>]+"
)
PROVIDER_BEARER_SECRET = re.compile(
    r"(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+[^\s,;\"'<>]+"
)


def print(*args, **kwargs) -> None:
    """Keep progress records atomic when document and image workers overlap."""
    with PRINT_LOCK:
        kwargs.setdefault("flush", True)
        builtins.print(*args, **kwargs)


def safe_provider_diagnostic(value: object) -> str:
    """Return provider error context without persisting credentials or signed URLs."""

    def scrub(item: object, depth: int = 0) -> object:
        if depth > 8:
            return "<truncated>"
        if isinstance(item, dict):
            return {
                str(key): (
                    "<redacted>"
                    if PROVIDER_SECRET_FIELD.search(str(key))
                    else scrub(child, depth + 1)
                )
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [scrub(child, depth + 1) for child in item[:50]]
        if isinstance(item, str):
            without_urls = re.sub(
                r"(?i)https?://[^\s\"'<>]+",
                "<redacted-url>",
                item,
            )
            without_bearer = PROVIDER_BEARER_SECRET.sub(
                "<redacted-secret>", without_urls
            )
            return PROVIDER_INLINE_SECRET.sub("<redacted-secret>", without_bearer)
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        return f"<{type(item).__name__}>"

    try:
        return json.dumps(scrub(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return f"<{type(value).__name__}>"


def new_pooled_session(pool_size: int) -> requests.Session:
    """Create a keep-alive HTTP client without hidden automatic retries."""
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=max(2, pool_size),
        pool_maxsize=max(2, pool_size),
        max_retries=0,
        pool_block=True,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def mineru_http_session() -> requests.Session:
    """Reuse MinerU polling/download connections inside each long-lived lane."""
    session = getattr(HTTP_SESSION_LOCAL, "mineru_session", None)
    if session is None:
        session = new_pooled_session(4)
        HTTP_SESSION_LOCAL.mineru_session = session
    return session


def reset_mineru_http_session() -> None:
    """Discard a broken keep-alive pool before the next read-only retry."""
    session = getattr(HTTP_SESSION_LOCAL, "mineru_session", None)
    if session is not None:
        session.close()
        delattr(HTTP_SESSION_LOCAL, "mineru_session")


def mimo_http_session(slot: str, per_key: int) -> requests.Session:
    """Keep one persistent connection pool per MiMo key."""
    with MIMO_SESSION_LOCK:
        session = MIMO_SESSIONS.get(slot)
        if session is None:
            session = new_pooled_session(max(2, per_key))
            MIMO_SESSIONS[slot] = session
        return session


@contextmanager
def mimo_key_request_context(slot: str, cfg: dict):
    """Enforce the advertised per-key in-flight limit."""
    limit = max(1, int(cfg.get("parallel_per_key", 1)))
    with MIMO_KEY_GATE_LOCK:
        configured = MIMO_KEY_GATES.get(slot)
        if configured is None or configured[0] != limit:
            # Configuration is loaded once per worker. A replacement is safe
            # because a changed limit only takes effect after worker reload.
            configured = (limit, threading.BoundedSemaphore(limit))
            MIMO_KEY_GATES[slot] = configured
        semaphore = configured[1]
    semaphore.acquire()
    try:
        yield mimo_http_session(slot, limit)
    finally:
        semaphore.release()


def note_mimo_key_rate_limit(slot: str, seconds: int) -> None:
    with MIMO_KEY_GATE_LOCK:
        MIMO_KEY_COOLDOWNS[slot] = max(
            MIMO_KEY_COOLDOWNS.get(slot, 0.0),
            time.monotonic() + max(30, seconds),
        )


def note_mimo_key_success(slot: str) -> None:
    with MIMO_KEY_GATE_LOCK:
        MIMO_KEY_COOLDOWNS.pop(slot, None)


class ElasticConcurrencyGate:
    """A bounded gate whose capacity can change without replacing threads."""

    def __init__(
        self,
        name: str,
        minimum: int = 1,
        maximum: int = 1,
        initial: int = 1,
        increase_every: int = 8,
    ) -> None:
        self.name = name
        self._condition = threading.Condition()
        self.minimum = max(1, minimum)
        self.maximum = max(self.minimum, maximum)
        self.limit = max(self.minimum, min(self.maximum, initial))
        self.active = 0
        self.waiters = 0
        self.success_streak = 0
        self.increase_every = max(1, increase_every)
        self.cooldown_until = 0.0
        self.last_change_at: float | None = None

    def configure(
        self,
        minimum: int,
        maximum: int,
        initial: int | None = None,
        increase_every: int | None = None,
    ) -> None:
        with self._condition:
            self.minimum = max(1, minimum)
            self.maximum = max(self.minimum, maximum)
            if increase_every is not None:
                self.increase_every = max(1, increase_every)
            target = self.limit if initial is None else initial
            self.limit = max(self.minimum, min(self.maximum, target))
            self._condition.notify_all()

    def set_limit(
        self,
        value: int,
        reason: str = "",
        increase_hold_seconds: float = 0.0,
    ) -> None:
        with self._condition:
            value = max(self.minimum, min(self.maximum, value))
            if value == self.limit:
                return
            now = time.monotonic()
            # Memory pressure must be able to reduce concurrency immediately.
            # Only delay an increase so short-lived free-memory spikes do not
            # make the command and indexing gates oscillate every few seconds.
            if (
                value > self.limit
                and increase_hold_seconds > 0
                and self.last_change_at is not None
                and now - self.last_change_at < increase_hold_seconds
            ):
                return
            previous = self.limit
            self.limit = value
            self.last_change_at = now
            self._condition.notify_all()
        suffix = f"｜{reason}" if reason else ""
        print(f"{self.name}弹性并发：{previous}→{value}{suffix}")

    def acquire(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            self.waiters += 1
            try:
                while self.active >= self.limit:
                    if deadline is None:
                        self._condition.wait()
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                self.active += 1
                return True
            finally:
                self.waiters -= 1

    def release(self) -> None:
        with self._condition:
            self.active = max(0, self.active - 1)
            self._condition.notify_all()

    @contextmanager
    def slot(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def reward(self) -> None:
        with self._condition:
            if time.monotonic() < self.cooldown_until:
                return
            self.success_streak += 1
            if (
                self.success_streak < self.increase_every
                or self.limit >= self.maximum
            ):
                return
            previous = self.limit
            self.limit += 1
            self.success_streak = 0
            self.last_change_at = time.monotonic()
            self._condition.notify_all()
        print(f"{self.name}弹性并发：{previous}→{self.limit}｜连续请求稳定")

    def penalize(self, seconds: int) -> None:
        with self._condition:
            previous = self.limit
            self.limit = max(self.minimum, (self.limit + 1) // 2)
            self.success_streak = 0
            self.cooldown_until = max(
                self.cooldown_until,
                time.monotonic() + max(30, seconds),
            )
            self.last_change_at = time.monotonic()
            self._condition.notify_all()
        if previous != self.limit:
            print(
                f"{self.name}弹性并发：{previous}→{self.limit}｜"
                f"检测到限流，冷却{max(30, seconds)}秒"
            )

    def snapshot(self) -> tuple[int, int, int]:
        with self._condition:
            return self.limit, self.active, self.waiters


class ElasticBatchSizer:
    """Increase MiMo image batch size only after validated responses."""

    def __init__(
        self,
        minimum: int = 1,
        maximum: int = 1,
        initial: int = 1,
        increase_every: int = 4,
    ) -> None:
        self._lock = threading.Lock()
        self.minimum = max(1, minimum)
        self.maximum = max(self.minimum, maximum)
        self.size = max(self.minimum, min(self.maximum, initial))
        self.increase_every = max(1, increase_every)
        self.success_streak = 0

    def configure(
        self,
        minimum: int,
        maximum: int,
        initial: int,
        increase_every: int,
    ) -> None:
        with self._lock:
            self.minimum = max(1, minimum)
            self.maximum = max(self.minimum, maximum)
            self.size = max(self.minimum, min(self.maximum, initial))
            self.increase_every = max(1, increase_every)
            self.success_streak = 0

    def current(self) -> int:
        with self._lock:
            return self.size

    def reward(self, validated_size: int) -> None:
        with self._lock:
            if validated_size < 1:
                return
            # A failed multi-image request reduces the batch size to one.
            # Single-image responses are still strictly ID-validated, so let
            # a longer streak of them recover the batch size to two.  Without
            # this recovery path, size one was an absorbing state because no
            # future request could ever satisfy validated_size >= 2.
            if self.size > 1 and validated_size < 2:
                return
            self.success_streak += 1
            required_successes = (
                self.increase_every * 2
                if self.size == 1 and self.maximum > 1
                else self.increase_every
            )
            if (
                self.success_streak < required_successes
                or self.size >= self.maximum
            ):
                return
            previous = self.size
            self.size += 1
            self.success_streak = 0
        reason = (
            "连续单图编号校验通过"
            if previous == 1
            else "连续批次编号校验通过"
        )
        print(f"MiMo单请求图片数：{previous}→{self.size}｜{reason}")

    def penalize(self, reason: str) -> None:
        with self._lock:
            previous = self.size
            self.size = max(self.minimum, (self.size + 1) // 2)
            self.success_streak = 0
        if previous != self.size:
            print(f"MiMo单请求图片数：{previous}→{self.size}｜{reason}")


MIMO_REQUEST_GATE = ElasticConcurrencyGate("MiMo", 1, 1, 1)
MIMO_BATCH_SIZER = ElasticBatchSizer(1, 1, 1)
MIMO_GATE_INIT_LOCK = threading.Lock()
MIMO_GATE_INITIALIZED = False
WEKNORA_COMMAND_GATE = ElasticConcurrencyGate(
    "WeKnora命令", 1, 6, 4
)
IMPORTANT_IMAGE_TERMS = (
    "如图", "图中", "根据图示", "见下图", "坐标图", "函数图", "几何图",
    "电路图", "实验装置", "题目表格", "地图", "流程图",
)
DIAGRAM_IMAGE_TERMS = (
    "坐标图", "函数图", "几何图", "电路图", "实验装置", "地图", "流程图",
    "抛物线", "坐标系", "电路", "装置图",
)
IGNORED_IMAGE_TERMS = (
    "logo", "校徽", "水印", "二维码", "出版社标志", "出版社标识",
    "页眉", "页脚", "装饰图",
)


@dataclass(frozen=True)
class SourcePart:
    path: Path
    start_page: int


@dataclass(frozen=True)
class MinerUBatch:
    batch_id: str
    token_slot: str


class MinerURetryLater(RuntimeError):
    """The remote task is healthy but is not ready for local consumption."""


class MinerURepartitionRequired(RuntimeError):
    """An old batch used a page size rejected by the current MinerU API."""


class MinerUWaitingFile(RuntimeError):
    """MinerU created the task but still has not received the uploaded file."""


@dataclass
class ClassificationStats:
    archives_extracted: int = 0
    archives_failed: int = 0
    videos_deleted: int = 0
    video_bytes_deleted: int = 0
    supported_files: int = 0
    unsupported_moved: int = 0
    transient_skipped: int = 0
    empty_directories_removed: int = 0


@dataclass
class QuestionUnit:
    number: str
    occurrence: int
    source_stem: str
    stem: str
    answer: str = ""
    explanation: str = ""
    label: str = ""
    kind: str = "question"


@dataclass(frozen=True)
class DocumentClassification:
    document_type: str
    institution: str
    primary_module: str
    module_tags: tuple[str, ...]
    method: str
    confidence: float
    evidence: tuple[str, ...]
    version: int = 1


@dataclass(frozen=True)
class GroupIndexTask:
    group_id: str
    group_name: str
    sources: tuple[Path, ...]
    parsed_paths: tuple[Path, ...]
    parent_path: Path
    child_path: Path
    raw_path: Path
    parent_doc_id: str
    child_doc_id: str
    raw_doc_id: str
    classification: DocumentClassification


@dataclass(frozen=True)
class PreparedContentMigration:
    index: int
    total: int
    group_id: str
    group_name: str
    expected_updated_at: int
    expected_classification_json: str
    old_parent: Path
    old_raw: Path | None
    old_parent_sha256: str
    old_raw_sha256: str
    old_parent_id: str
    old_child_id: str
    old_raw_id: str
    parent_path: Path
    child_path: Path
    raw_path: Path
    final_parent_path: Path
    final_raw_path: Path
    source_hint: Path
    classification: DocumentClassification
    unit_count: int
    unmatched_answers: int
    document_mode: str
    job: Path


class ContentMigrationCancelled(RuntimeError):
    """The group changed or was deleted while a staged migration was running."""


def content_migration_snapshot_matches(
    row: sqlite3.Row | None,
    prepared: PreparedContentMigration,
) -> bool:
    """Compare the complete publication identity, not only a second timestamp."""
    if row is None:
        return False
    expected_raw_path = str(prepared.old_raw) if prepared.old_raw else ""
    return (
        str(row["state"] or "") == "completed"
        and int(row["updated_at"] or 0) == prepared.expected_updated_at
        and str(row["markdown_path"] or "") == str(prepared.old_parent)
        and str(row["raw_path"] or "") == expected_raw_path
        and str(row["parent_doc_id"] or "") == prepared.old_parent_id
        and str(row["child_doc_id"] or "") == prepared.old_child_id
        and str(row["raw_doc_id"] or "") == prepared.old_raw_id
        and str(row["classification_json"] or "")
        == prepared.expected_classification_json
    )


def content_migration_source_files_match(
    prepared: PreparedContentMigration,
) -> bool:
    if (
        not prepared.old_parent.is_file()
        or sha256(prepared.old_parent) != prepared.old_parent_sha256
    ):
        return False
    if prepared.old_raw is None:
        return True
    return (
        prepared.old_raw.is_file()
        and sha256(prepared.old_raw) == prepared.old_raw_sha256
    )


def content_migration_committed_state_matches(
    row: sqlite3.Row | None,
    prepared: PreparedContentMigration,
    parent_doc_id: str,
    child_doc_id: str,
    raw_doc_id: str,
) -> bool:
    if row is None:
        return False
    return (
        str(row["state"] or "") == "completed"
        and str(row["markdown_path"] or "")
        == str(prepared.final_parent_path)
        and str(row["raw_path"] or "") == str(prepared.final_raw_path)
        and str(row["parent_doc_id"] or "") == parent_doc_id
        and str(row["child_doc_id"] or "") == child_doc_id
        and str(row["raw_doc_id"] or "") == raw_doc_id
    )


def content_migration_placed_files_match(
    prepared: PreparedContentMigration,
) -> bool:
    journal = content_migration_journal_path(prepared)
    if not journal.is_file():
        return False
    try:
        payload = json.loads(journal.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    layers = {
        str(layer.get("name") or ""): layer
        for layer in payload.get("layers") or []
    }
    for name, path in (
        ("parent", prepared.final_parent_path),
        ("raw", prepared.final_raw_path),
    ):
        layer = layers.get(name) or {}
        expected = str(layer.get("file_sha256") or "")
        if (
            not bool(layer.get("new_placed"))
            or not expected
            or not path.is_file()
            or sha256(path) != expected
        ):
            return False
    return True


def windows_memory_gb() -> tuple[float, float]:
    if os.name != "nt":
        return 16.0, 8.0

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 16.0, 4.0
    gib = 1024**3
    return status.total_physical / gib, status.available_physical / gib


def smoothed_available_memory_gb(
    kind: str,
    available_gb: float,
    sample_count: int,
) -> float:
    """Use a short median window so worker counts do not flap at a boundary."""
    sample_count = max(1, min(8, sample_count))
    with MEMORY_SAMPLE_LOCK:
        samples = MEMORY_SAMPLES.setdefault(kind, deque(maxlen=8))
        samples.append(available_gb)
        recent = list(samples)[-sample_count:]
    ordered = sorted(recent)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def adaptive_worker_count(cfg: dict, kind: str, requested: int) -> int:
    resources = cfg.get("resource_control") or {}
    if str(resources.get("mode", "auto")).casefold() != "auto":
        return max(1, requested)
    total_gb, instant_available_gb = windows_memory_gb()
    critical_free_gb = max(
        0.75,
        float(resources.get("critical_windows_free_gb", 1.2)),
    )
    scale_up_free_gb = max(
        critical_free_gb + 0.25,
        float(resources.get("scale_up_windows_free_gb", 2.2)),
    )
    sample_count = int(
        resources.get("memory_hysteresis_samples", 3)
    )
    available_gb = (
        instant_available_gb
        if instant_available_gb < critical_free_gb
        else smoothed_available_memory_gb(
            kind, instant_available_gb, sample_count
        )
    )
    reserve_gb = max(
        float(resources.get("reserve_windows_memory_min_gb", 1.5)),
        total_gb * float(resources.get("reserve_windows_memory_ratio", 0.10)),
    )
    usable_gb = max(0.0, available_gb - reserve_gb)
    logical_cpu = max(1, os.cpu_count() or 1)
    if kind == "prequeue":
        configured_max = int(resources.get("prequeue_workers_max", 8))
        memory_limit = max(1, 2 + int(usable_gb / 0.35))
        cpu_limit = max(1, logical_cpu // 2)
    else:
        configured_max = int(resources.get("local_processing_workers_max", 4))
        configured_min = max(
            1,
            int(resources.get("local_processing_workers_min", 2)),
        )
        worker_memory_gb = max(
            0.5,
            float(resources.get("local_worker_memory_gb", 0.75)),
        )
        memory_limit = max(1, 1 + int(usable_gb / worker_memory_gb))
        if available_gb >= scale_up_free_gb:
            memory_limit = max(configured_min, memory_limit)
        cpu_limit = max(1, logical_cpu // 4)
    return max(
        1,
        min(requested, configured_max, memory_limit, cpu_limit),
    )


def adaptive_weknora_command_count(pressure: int = 0) -> int:
    resources = RUNTIME_RESOURCE_CONFIG
    configured_max = max(
        1, int(resources.get("weknora_command_parallel_max", 6))
    )
    total_gb, instant_available_gb = windows_memory_gb()
    critical_free_gb = max(
        0.75,
        float(resources.get("critical_windows_free_gb", 1.2)),
    )
    scale_up_free_gb = max(
        critical_free_gb + 0.25,
        float(resources.get("scale_up_windows_free_gb", 2.2)),
    )
    available_gb = (
        instant_available_gb
        if instant_available_gb < critical_free_gb
        else smoothed_available_memory_gb(
            "weknora-command",
            instant_available_gb,
            int(resources.get("memory_hysteresis_samples", 3)),
        )
    )
    if available_gb < critical_free_gb:
        return 1
    if available_gb < scale_up_free_gb:
        return 1
    reserve_gb = max(
        float(resources.get("reserve_windows_memory_min_gb", 1.5)),
        total_gb * float(resources.get("reserve_windows_memory_ratio", 0.10)),
    )
    usable_gb = max(0.0, available_gb - reserve_gb)
    per_command_gb = max(
        0.5,
        float(resources.get("weknora_command_memory_gb", 1.0)),
    )
    hardware_limit = 8 if total_gb >= 24 else 6 if total_gb >= 15 else 4
    base = max(
        2,
        min(
            configured_max,
            hardware_limit,
            2 + int(usable_gb / per_command_gb),
        ),
    )
    burst_free_gb = max(
        0.5,
        float(resources.get("weknora_command_burst_free_gb", 0.75)),
    )
    if pressure > base and instant_available_gb >= (
        critical_free_gb + burst_free_gb
    ):
        burst_steps = 1 + int(
            max(
                0.0,
                instant_available_gb
                - critical_free_gb
                - burst_free_gb,
            )
            / burst_free_gb
        )
        base += min(pressure - base, burst_steps)
    return min(configured_max, hardware_limit, base)


def adaptive_weknora_index_count(cfg: dict, pressure: int = 0) -> int:
    resources = cfg.get("resource_control") or {}
    configured_max = max(
        1, int(resources.get("weknora_inflight_groups_max", 4))
    )
    configured_min = max(
        1, int(resources.get("weknora_inflight_groups_min", 1))
    )
    total_gb, instant_available_gb = windows_memory_gb()
    critical_free_gb = max(
        0.75,
        float(resources.get("critical_windows_free_gb", 1.2)),
    )
    scale_up_free_gb = max(
        critical_free_gb + 0.25,
        float(resources.get("scale_up_windows_free_gb", 2.2)),
    )
    if instant_available_gb < critical_free_gb:
        return configured_min
    if instant_available_gb < scale_up_free_gb:
        return configured_min
    available_gb = smoothed_available_memory_gb(
        "weknora-index",
        instant_available_gb,
        int(resources.get("memory_hysteresis_samples", 3)),
    )
    reserve_gb = max(
        float(resources.get("reserve_windows_memory_min_gb", 1.5)),
        total_gb * float(resources.get("reserve_windows_memory_ratio", 0.10)),
    )
    usable_gb = max(0.0, available_gb - reserve_gb)
    per_extra_gb = max(
        0.75,
        float(resources.get("weknora_index_extra_memory_gb", 1.5)),
    )
    normal_min = min(configured_max, configured_min)
    base = max(
        configured_min,
        min(configured_max, normal_min + int(usable_gb / per_extra_gb)),
    )
    burst_free_gb = max(
        0.5,
        float(resources.get("weknora_index_burst_free_gb", 0.75)),
    )
    if pressure > base and instant_available_gb >= (
        critical_free_gb + burst_free_gb
    ):
        burst_steps = 1 + int(
            max(
                0.0,
                instant_available_gb
                - critical_free_gb
                - burst_free_gb,
            )
            / burst_free_gb
        )
        base += min(pressure - base, burst_steps)
    return min(configured_max, base)


def require_permanent_delete_confirmation(action: str) -> None:
    if os.getenv("QUESTION_BANK_ALLOW_PERMANENT_DELETE", "") == "I_UNDERSTAND":
        return
    raise RuntimeError(
        f"{action}会永久删除本地文件或远端索引，但尚未显式确认。"
        "若你已经理解且接受不可恢复删除，请在本机.env中设置"
        "QUESTION_BANK_ALLOW_PERMANENT_DELETE=I_UNDERSTAND。"
    )


def require_manual_deletion_sync_confirmation() -> None:
    if (
        os.getenv("QUESTION_BANK_ALLOW_MANUAL_DELETION_SYNC", "")
        == "I_UNDERSTAND"
    ):
        return
    raise RuntimeError(
        "手动删除级联同步会永久删除整份资料的本地文件和远端索引，"
        "但尚未独立确认。请仅在理解该规则后，在本机.env中设置"
        "QUESTION_BANK_ALLOW_MANUAL_DELETION_SYNC=I_UNDERSTAND。"
    )


def guarded_unlink(
    path: Path,
    action: str,
    expected_digest: str | None = None,
) -> str:
    """Delete one explicitly selected ordinary file behind the global gate."""
    require_permanent_delete_confirmation(action)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"拒绝永久删除非普通文件: {path}")
    current_digest = stable_sha256(path)
    if expected_digest and current_digest != expected_digest:
        raise RuntimeError(f"文件摘要已变化，拒绝永久删除: {path}")
    path.unlink()
    if path.exists():
        raise OSError(f"删除调用结束后文件仍存在: {path}")
    return current_digest


def load_settings(config_path: str | Path | None = None) -> dict:
    global RUNTIME_RESOURCE_CONFIG, MIMO_GATE_INITIALIZED, NEO4J_RUNTIME_CONFIG
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "mineru-keys.env", override=False)
    load_dotenv(ROOT / "mimo-keys.env", override=False)
    requested = str(config_path or os.getenv("QUESTION_BANK_CONFIG", "")).strip()
    selected = Path(requested) if requested else ROOT / "config.local.yaml"
    if not selected.is_absolute():
        selected = (ROOT / selected).resolve()
    if not selected.is_file():
        raise RuntimeError(
            f"配置文件不存在: {selected}。先运行 scripts/bootstrap.ps1，"
            "或把 config.example.yaml 复制为 config.local.yaml。"
        )
    os.environ["QUESTION_BANK_CONFIG"] = str(selected)
    cfg = yaml.safe_load(selected.read_text("utf-8"))
    weknora_cfg = cfg.get("weknora", {})
    NEO4J_RUNTIME_CONFIG = {
        "wsl_distro": str(weknora_cfg.get("wsl_distro", "Ubuntu")),
        "container": str(
            weknora_cfg.get("neo4j_container", "WeKnora-neo4j")
        ),
    }
    RUNTIME_RESOURCE_CONFIG = dict(cfg.get("resource_control") or {})
    command_setting = cfg.get("weknora", {}).get("command_parallel", "auto")
    if str(command_setting).casefold() == "auto":
        command_parallel = adaptive_weknora_command_count()
    else:
        command_parallel = int(command_setting)
    command_parallel = max(1, min(8, command_parallel))
    WEKNORA_COMMAND_GATE.configure(
        1,
        max(
            1,
            int(
                RUNTIME_RESOURCE_CONFIG.get(
                    "weknora_command_parallel_max", 6
                )
            ),
        ),
        command_parallel,
    )
    mimo_cfg = cfg.get("ollama", {}).get("mimo", {})
    mimo_max = memory_aware_mimo_ceiling(mimo_cfg)
    mimo_initial = min(
        mimo_max, max(1, int(mimo_cfg.get("initial_parallel", 2)))
    )
    MIMO_REQUEST_GATE.configure(
        1,
        mimo_max,
        mimo_initial,
        int(mimo_cfg.get("increase_every_successes", 8)),
    )
    with MIMO_GATE_INIT_LOCK:
        MIMO_GATE_INITIALIZED = True
    MIMO_BATCH_SIZER.configure(
        1,
        max(1, int(mimo_cfg.get("image_batch_max", 4))),
        max(1, int(mimo_cfg.get("image_batch_initial", 2))),
        max(1, int(mimo_cfg.get("image_batch_increase_every", 4))),
    )
    taxonomy_path = Path(cfg["document_classification"]["taxonomy_file"])
    if not taxonomy_path.is_absolute():
        taxonomy_path = (ROOT / taxonomy_path).resolve()
    cfg["document_classification"]["taxonomy"] = yaml.safe_load(
        taxonomy_path.read_text("utf-8")
    )
    for key, value in cfg["folders"].items():
        cfg["folders"][key] = (ROOT / value).resolve()
        cfg["folders"][key].mkdir(parents=True, exist_ok=True)
    cfg["classification"]["archive_store"] = cfg["folders"]["archives"]
    archive_executable = Path(cfg["classification"]["archive_executable"])
    if not archive_executable.is_absolute():
        discovered_archive_executable = shutil.which(str(archive_executable))
        archive_executable = (
            Path(discovered_archive_executable).resolve()
            if discovered_archive_executable
            else (ROOT / archive_executable).resolve()
        )
    cfg["classification"]["archive_executable"] = archive_executable
    destructive_options = {
        "classification.delete_videos": cfg["classification"].get(
            "delete_videos", False
        ),
        "classification.delete_archives_after_extract": cfg[
            "classification"
        ].get("delete_archives_after_extract", False),
        "document_classification.delete_other_source_after_markdown": cfg[
            "document_classification"
        ].get("delete_other_source_after_markdown", False),
        "cleanup.permanently_delete_source_after_search": cfg["cleanup"].get(
            "permanently_delete_source_after_search", False
        ),
    }
    enabled_destructive = [
        name for name, enabled in destructive_options.items() if bool(enabled)
    ]
    if enabled_destructive:
        require_permanent_delete_confirmation(
            "检测到永久删除选项: " + ", ".join(enabled_destructive)
        )
    if bool((cfg.get("manual_deletions") or {}).get("auto_sync", False)):
        require_manual_deletion_sync_confirmation()
    return cfg


def db_open() -> sqlite3.Connection:
    db = sqlite3.connect(ROOT / "state.db", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""CREATE TABLE IF NOT EXISTS files(
        sha256 TEXT PRIMARY KEY, source_path TEXT, batch_id TEXT, state TEXT,
        markdown_path TEXT, error TEXT, updated_at INTEGER,
        weknora_doc_id TEXT, metrics_json TEXT NOT NULL DEFAULT '{}')""")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(files)")}
    if "weknora_doc_id" not in columns:
        db.execute("ALTER TABLE files ADD COLUMN weknora_doc_id TEXT")
    if "metrics_json" not in columns:
        db.execute(
            "ALTER TABLE files ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'"
        )
    db.execute(
        "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT)"
    )
    db.execute("""CREATE TABLE IF NOT EXISTS groups(
        group_id TEXT PRIMARY KEY, group_name TEXT, state TEXT,
        markdown_path TEXT, parent_doc_id TEXT, child_doc_id TEXT,
        raw_path TEXT, raw_doc_id TEXT,
        error TEXT, updated_at INTEGER, classification_json TEXT)""")
    group_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(groups)")
    }
    if "classification_json" not in group_columns:
        db.execute("ALTER TABLE groups ADD COLUMN classification_json TEXT")
    if "raw_path" not in group_columns:
        db.execute("ALTER TABLE groups ADD COLUMN raw_path TEXT")
    if "raw_doc_id" not in group_columns:
        db.execute("ALTER TABLE groups ADD COLUMN raw_doc_id TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS group_files(
        group_id TEXT NOT NULL, sha256 TEXT NOT NULL, source_path TEXT NOT NULL,
        PRIMARY KEY(group_id, source_path),
        FOREIGN KEY(group_id) REFERENCES groups(group_id) ON DELETE CASCADE,
        FOREIGN KEY(sha256) REFERENCES files(sha256) ON DELETE RESTRICT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS deletion_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        group_id TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,
        markdown_path TEXT NOT NULL,
        markdown_sha256 TEXT NOT NULL,
        requested_at INTEGER NOT NULL,
        completed_at INTEGER,
        success INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS manual_deletion_audit(
        group_id TEXT PRIMARY KEY,
        requested_at INTEGER NOT NULL,
        completed_at INTEGER,
        state TEXT NOT NULL,
        deleted_sources INTEGER NOT NULL DEFAULT 0,
        deleted_markdown INTEGER NOT NULL DEFAULT 0,
        deleted_indexes INTEGER NOT NULL DEFAULT 0,
        selection_json TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS index_cleanup_queue(
        doc_id TEXT NOT NULL,
        knowledge_base_id TEXT NOT NULL,
        group_id TEXT NOT NULL,
        markdown_path TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(doc_id, knowledge_base_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS vision_description_cache(
        cache_key TEXT PRIMARY KEY,
        model TEXT NOT NULL,
        description TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")
    db.commit()
    group_file_columns = db.execute("PRAGMA table_info(group_files)").fetchall()
    group_file_pk = [
        row["name"]
        for row in sorted(group_file_columns, key=lambda item: item["pk"])
        if row["pk"]
    ]
    group_file_fks = {
        row["from"]: row["table"]
        for row in db.execute("PRAGMA foreign_key_list(group_files)").fetchall()
    }
    legacy_group_files = bool(
        db.execute(
            """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='group_files_legacy'"""
        ).fetchone()
    )
    rebuild_group_files = (
        group_file_pk != ["group_id", "source_path"]
        or group_file_fks.get("group_id") != "groups"
        or group_file_fks.get("sha256") != "files"
        or legacy_group_files
    )
    if rebuild_group_files:
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DROP TABLE IF EXISTS group_files_rebuild")
            db.execute("""CREATE TABLE group_files_rebuild(
                group_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                source_path TEXT NOT NULL,
                PRIMARY KEY(group_id, source_path),
                FOREIGN KEY(group_id) REFERENCES groups(group_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(sha256) REFERENCES files(sha256)
                    ON DELETE RESTRICT)""")
            db.execute("""INSERT OR IGNORE INTO group_files_rebuild(
                    group_id,sha256,source_path
                )
                SELECT current.group_id,current.sha256,current.source_path
                FROM group_files current
                JOIN groups g ON g.group_id=current.group_id
                JOIN files f ON f.sha256=current.sha256""")
            if legacy_group_files:
                db.execute("""INSERT OR IGNORE INTO group_files_rebuild(
                        group_id,sha256,source_path
                    )
                    SELECT legacy.group_id,legacy.sha256,legacy.source_path
                    FROM group_files_legacy legacy
                    JOIN groups g ON g.group_id=legacy.group_id
                    JOIN files f ON f.sha256=legacy.sha256""")
            db.execute("DROP TABLE group_files")
            db.execute(
                "ALTER TABLE group_files_rebuild RENAME TO group_files"
            )
            if legacy_group_files:
                db.execute("DROP TABLE group_files_legacy")
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_files_sha256 ON group_files(sha256)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_deletion_audit_sha256 "
        "ON deletion_audit(sha256)"
    )
    db.commit()
    return db


def save_state(
    db,
    sha: str,
    source: Path,
    state: str,
    batch_id: str | None = None,
    md_path: str | None = None,
    error: str = "",
    weknora_doc_id: str | None = None,
    metrics: dict | None = None,
) -> None:
    existing_state_row = db.execute(
        "SELECT state FROM files WHERE sha256=?", (sha,)
    ).fetchone()
    existing_state = (
        str(existing_state_row["state"] or "") if existing_state_row else ""
    )
    if existing_state in {"user_delete_pending", "user_deleted"} and state not in {
        "user_delete_pending",
        "user_deleted",
    }:
        # A stale worker must never resurrect content after the user has
        # requested or completed permanent exclusion.
        return
    if metrics is None:
        existing = db.execute(
            "SELECT metrics_json FROM files WHERE sha256=?", (sha,)
        ).fetchone()
        metrics_json = existing["metrics_json"] if existing else "{}"
    else:
        metrics_json = json.dumps(metrics, ensure_ascii=False, separators=(",", ":"))
    db.execute("""INSERT INTO files(
        sha256,source_path,batch_id,state,markdown_path,error,updated_at,
        weknora_doc_id,metrics_json
    ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(sha256) DO UPDATE SET source_path=excluded.source_path,
        batch_id=COALESCE(excluded.batch_id,files.batch_id),
        state=excluded.state,
        markdown_path=COALESCE(excluded.markdown_path,files.markdown_path),
        error=excluded.error, updated_at=excluded.updated_at,
        weknora_doc_id=COALESCE(excluded.weknora_doc_id,files.weknora_doc_id),
        metrics_json=excluded.metrics_json""",
        (
            sha,
            str(source),
            batch_id,
            state,
            md_path,
            error,
            int(time.time()),
            weknora_doc_id,
            metrics_json,
        ))
    db.commit()


def file_metrics(row: sqlite3.Row | None) -> dict:
    metrics = {
        "started_at": 0,
        "finished_at": 0,
        "source_pages": 0,
        "mineru_batch_submitted_at": 0,
        "mineru_cloud_done_at": 0,
        "mineru_wait_seconds": 0.0,
        "mineru_download_seconds": 0.0,
        "image_placeholders": 0,
        "rule_decisions": 0,
        "llm_judgements": 0,
        "important_images": 0,
        "ignored_images": 0,
        "ocr_images": 0,
        "ocr_seconds": 0.0,
        "vision_images": 0,
        "vision_cache_hits": 0,
        "vision_cache_misses": 0,
        "vision_seconds": 0.0,
        "mimo_prompt_tokens": 0,
        "mimo_completion_tokens": 0,
        "mimo_image_tokens": 0,
        "mimo_key_slots": [],
        "weknora_index_seconds": 0.0,
        "active_processing_seconds": 0.0,
        "total_seconds": 0.0,
        "mineru_token_slots": [],
    }
    if row and "metrics_json" in row.keys() and row["metrics_json"]:
        try:
            saved = json.loads(row["metrics_json"])
            if isinstance(saved, dict):
                metrics.update(saved)
        except json.JSONDecodeError:
            pass
    return metrics


@contextmanager
def single_instance(lock_name: str = ".ingest.lock"):
    lock_path = ROOT / lock_name
    handle = lock_path.open("a+b")
    locked = False
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError as exc:
            raise SystemExit(
                f"已有一个使用{lock_name}的任务正在运行，本次未启动"
            ) from exc
        yield
    finally:
        try:
            if locked:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


def supervisor_progress_stamp() -> float:
    """Return the newest durable progress timestamp without writing state."""
    candidates = [
        ROOT / "ingest.log",
        ROOT / ".runtime" / "logs" / "ingest.stdout.log",
        ROOT / ".runtime" / "logs" / "ingest.stderr.log",
        ROOT / "state.db",
        ROOT / "state.db-wal",
    ]
    return max(
        (path.stat().st_mtime for path in candidates if path.exists()),
        default=time.time(),
    )


def supervisor_gpu_active(threshold_percent: int) -> bool:
    """Treat any NVIDIA workload as activity to avoid a false restart."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        values = [
            int(match.group())
            for match in re.finditer(r"\d+", result.stdout or "")
        ]
        return bool(values) and max(values) >= threshold_percent
    except (OSError, subprocess.SubprocessError, ValueError):
        # Missing evidence must never trigger a destructive restart.
        return True


def supervisor_weknora_processing() -> bool:
    """Return True while either knowledge base reports active indexing."""
    cli = ROOT / "bin" / "weknora.exe"
    if not cli.exists():
        return True
    try:
        result = subprocess.run(
            [str(cli), "kb", "list"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        payload = json.loads(result.stdout)
        if not payload.get("ok"):
            return True
        return any(
            bool(item.get("is_processing"))
            or int(item.get("processing_count") or 0) > 0
            for item in payload.get("data", [])
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return True


def stop_supervised_process_tree(process: subprocess.Popen) -> None:
    """Stop only the exact ingest worker tree owned by this supervisor."""
    if process.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def supervise_ingest(cfg: dict, config_path: str | None = None) -> None:
    """Run ingest as a child and recover only from proven idle stalls."""
    resource = cfg.get("resource_control", {})
    stall_seconds = max(
        600, int(resource.get("stall_watchdog_seconds", 1200))
    )
    poll_seconds = max(
        15, int(resource.get("stall_watchdog_poll_seconds", 60))
    )
    gpu_threshold = max(
        1, int(resource.get("stall_watchdog_gpu_active_percent", 10))
    )
    restart_limit = max(
        1, int(resource.get("stall_watchdog_max_restarts_per_hour", 3))
    )
    restart_times: deque[float] = deque()
    with single_instance(".supervisor.lock"):
        while True:
            worker_command = [
                sys.executable,
                "-u",
                str(Path(__file__).resolve()),
                "--worker",
            ]
            if config_path:
                worker_command.extend(["--config", config_path])
            worker = subprocess.Popen(
                worker_command,
                cwd=ROOT,
            )
            last_progress = max(time.time(), supervisor_progress_stamp())
            watchdog_restart = False
            while worker.poll() is None:
                time.sleep(poll_seconds)
                progress = supervisor_progress_stamp()
                if progress > last_progress:
                    last_progress = progress
                    continue
                if time.time() - last_progress < stall_seconds:
                    continue
                if (
                    supervisor_weknora_processing()
                    or supervisor_gpu_active(gpu_threshold)
                ):
                    # Give a full grace period after observed real work.
                    last_progress = time.time()
                    continue
                print(
                    "停滞监督：日志和数据库均无进展，"
                    "且WeKnora无索引、GPU空闲；仅重启ingest子进程",
                    flush=True,
                )
                stop_supervised_process_tree(worker)
                watchdog_restart = True
                break
            code = worker.wait()
            if code == 0 and not watchdog_restart:
                return
            now = time.time()
            while restart_times and now - restart_times[0] > 3600:
                restart_times.popleft()
            if len(restart_times) >= restart_limit:
                print(
                    f"停滞监督：一小时内已重启{restart_limit}次，"
                    "为避免循环重启已安全停止，源文件未删除",
                    flush=True,
                )
                return
            restart_times.append(now)
            print(
                f"停滞监督：15秒后恢复ingest｜"
                f"本小时重启{len(restart_times)}/{restart_limit}",
                flush=True,
            )
            time.sleep(15)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def md5_digest(path: Path) -> str:
    """Match WeKnora's file_hash without loading a large Markdown into RAM."""
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def part_data_id(part: SourcePart) -> str:
    return f"qb-{sha256(part.path)[:16]}-{part.start_page}"


def mineru_tokens() -> dict[str, str]:
    tokens = {
        "primary": os.getenv("MINERU_API_TOKEN", "").strip(),
        "backup": os.getenv("MINERU_API_TOKEN_BACKUP", "").strip(),
    }
    for index in range(3, 33):
        tokens[f"key{index:02d}"] = os.getenv(
            f"MINERU_API_TOKEN_{index}", ""
        ).strip()
    return {slot: token for slot, token in tokens.items() if token}


def mimo_keys() -> dict[str, str]:
    """Read the editable key file only when it changes.

    Values in the dedicated key file take priority over the process copy
    loaded at startup, so adding or replacing a key takes effect without
    restarting a long batch.
    """
    path = ROOT / "mimo-keys.env"
    try:
        stat_result = path.stat()
        signature = (stat_result.st_mtime_ns, stat_result.st_size)
    except FileNotFoundError:
        signature = (-1, 0)
    global MIMO_KEY_CACHE_SIGNATURE, MIMO_KEY_CACHE
    with MIMO_KEY_CACHE_LOCK:
        if signature == MIMO_KEY_CACHE_SIGNATURE:
            return dict(MIMO_KEY_CACHE)
        file_values = dotenv_values(path) if path.is_file() else {}

        def value(name: str) -> str:
            return (
                str(file_values.get(name) or "").strip()
                or os.getenv(name, "").strip()
            )

        tokens = {"mimo01": value("MIMO_API_KEY")}
        for index in range(2, 33):
            tokens[f"mimo{index:02d}"] = value(f"MIMO_API_KEY_{index}")
        MIMO_KEY_CACHE = {
            slot: token for slot, token in tokens.items() if token
        }
        MIMO_KEY_CACHE_SIGNATURE = signature
        return dict(MIMO_KEY_CACHE)


def ordered_mimo_slots(
    cfg: dict,
    max_attempts: int | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Round-robin healthy keys first and temporarily avoid a 429 key."""
    keys = mimo_keys()
    slots = list(keys)
    if not slots:
        return keys, []
    global MIMO_KEY_CURSOR
    with MIMO_KEY_LOCK:
        start = MIMO_KEY_CURSOR % len(slots)
        MIMO_KEY_CURSOR = (MIMO_KEY_CURSOR + 1) % len(slots)
    rotated = slots[start:] + slots[:start]
    now = time.monotonic()
    with MIMO_KEY_GATE_LOCK:
        healthy = [
            slot
            for slot in rotated
            if MIMO_KEY_COOLDOWNS.get(slot, 0.0) <= now
        ]
        cooling = sorted(
            (slot for slot in rotated if slot not in healthy),
            key=lambda slot: MIMO_KEY_COOLDOWNS.get(slot, 0.0),
        )
    attempts = max(
        1,
        int(
            max_attempts
            if max_attempts is not None
            else cfg.get("max_key_attempts", 3)
        ),
    )
    return keys, (healthy + cooling)[:attempts]


def configured_mimo_parallel_ceiling(cfg: dict) -> int:
    """Allow several independent in-flight requests per key without batching images."""
    key_count = max(1, len(mimo_keys()))
    per_key = max(1, int(cfg.get("parallel_per_key", 1)))
    provider_ceiling = key_count * per_key
    max_setting = cfg.get("max_parallel", "auto")
    if str(max_setting).casefold() == "auto":
        return min(
            provider_ceiling,
            max(1, int(cfg.get("parallel_cap", 6))),
        )
    return min(provider_ceiling, max(1, int(max_setting)))


def memory_aware_mimo_ceiling(cfg: dict) -> int:
    """Keep one request per key; memory only controls extra per-key pipelining."""
    configured = configured_mimo_parallel_ceiling(cfg)
    baseline = min(configured, max(1, len(mimo_keys())))
    if configured <= baseline:
        return configured
    _, available_gb = windows_memory_gb()
    available_gb = smoothed_available_memory_gb("mimo", available_gb, 3)
    floor_gb = max(0.25, float(cfg.get("memory_floor_gb", 0.8)))
    full_gb = max(
        floor_gb + 0.25,
        float(cfg.get("memory_full_parallel_gb", 1.8)),
    )
    if available_gb <= floor_gb:
        return baseline
    if available_gb >= full_gb:
        return configured
    fraction = (available_gb - floor_gb) / (full_gb - floor_gb)
    extra = int((configured - baseline) * fraction)
    return max(baseline, min(configured, baseline + extra))


def refresh_mimo_gate(cfg: dict) -> None:
    maximum = memory_aware_mimo_ceiling(cfg)
    global MIMO_GATE_INITIALIZED
    initial = None
    with MIMO_GATE_INIT_LOCK:
        if not MIMO_GATE_INITIALIZED:
            initial = min(
                maximum,
                max(1, int(cfg.get("initial_parallel", 2))),
            )
            MIMO_GATE_INITIALIZED = True
    MIMO_REQUEST_GATE.configure(
        1,
        maximum,
        initial=initial,
        increase_every=int(cfg.get("increase_every_successes", 8)),
    )


@contextmanager
def mimo_request_context(cfg: dict):
    """Bound MiMo calls while allowing success/429 feedback to change capacity."""
    refresh_mimo_gate(cfg)
    with MIMO_REQUEST_GATE.slot():
        yield


def note_mimo_success() -> None:
    MIMO_REQUEST_GATE.reward()


def note_mimo_rate_limit(seconds: int = 120) -> None:
    MIMO_REQUEST_GATE.penalize(seconds)


def configured_suffix(path: Path, suffixes: list[str]) -> str | None:
    name = path.name.casefold()
    matches = [
        suffix.casefold()
        for suffix in suffixes
        if name.endswith(suffix.casefold())
    ]
    return max(matches, key=len) if matches else None


def archive_suffix(path: Path, cfg: dict) -> str | None:
    return configured_suffix(path, cfg["archive_extensions"])


def is_appledouble_path(value: Path | str) -> bool:
    path = Path(value)
    return path.name.startswith("._") or any(
        part.casefold() == "__macosx" for part in path.parts
    )


def is_video(path: Path, cfg: dict) -> bool:
    return path.suffix.casefold() in {
        extension.casefold() for extension in cfg["video_extensions"]
    }


def is_transient_download(path: Path, cfg: dict) -> bool:
    return path.suffix.casefold() in {
        extension.casefold()
        for extension in cfg.get("transient_extensions", [])
    }


def safe_archive_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return False
    path = PurePosixPath(normalized)
    return ".." not in path.parts


def parse_archive_inventory_lines(
    lines: Iterable[str], cfg: dict
) -> tuple[int, int]:
    """Validate a 7-Zip -slt listing without retaining the whole listing."""
    max_files = int(cfg["max_archive_files"])
    max_bytes = int(float(cfg["max_expanded_gb"]) * 1024**3)
    count = 0
    expanded = 0
    current: dict[str, str] = {}

    def consume(record: dict[str, str]) -> None:
        nonlocal count, expanded
        if "Path" not in record:
            return
        # The first -slt record describes the archive itself rather than a
        # member.  It normally has Type/Physical Size but no member Size.
        if "Size" not in record and (
            "Type" in record or "Physical Size" in record
        ):
            return
        if record.get("Encrypted") == "+":
            raise RuntimeError("压缩包包含加密文件")
        if not safe_archive_member(record["Path"]):
            raise RuntimeError(f"压缩包包含异常路径: {record['Path']}")
        if record.get("Symbolic Link") or record.get("Hard Link"):
            raise RuntimeError(f"压缩包包含链接成员: {record['Path']}")
        count += 1
        if count > max_files:
            raise RuntimeError(f"压缩包条目数超过限制: {count} > {max_files}")
        folder = record.get("Folder") == "+" or record.get(
            "Attributes", ""
        ).startswith("D")
        if folder:
            return
        if "Size" not in record:
            raise RuntimeError("压缩包成员缺少可验证的文件大小")
        try:
            size = int(record["Size"])
        except ValueError as exc:
            raise RuntimeError("压缩包文件大小无法识别") from exc
        if size < 0:
            raise RuntimeError("压缩包文件大小不能为负数")
        expanded += size
        if expanded > max_bytes:
            raise RuntimeError(
                f"压缩包展开量超过限制: {expanded / 1024**3:.2f}GB"
            )

    for line in lines:
        if not line.strip():
            if current:
                consume(current)
                current = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key.strip()] = value.strip()
    if current:
        consume(current)
    return count, expanded


def archive_inventory(archive: Path, cfg: dict) -> tuple[int, int]:
    executable = cfg["archive_executable"]
    process = subprocess.Popen(
        [str(executable), "l", "-slt", "-sccUTF-8", str(archive)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:
        process.kill()
        raise RuntimeError("无法读取7-Zip压缩包清单")
    timed_out = threading.Event()

    def stop_listing() -> None:
        timed_out.set()
        try:
            process.kill()
        except OSError:
            pass

    timer = threading.Timer(600, stop_listing)
    timer.daemon = True
    timer.start()
    output_tail = ""

    def listing_lines() -> Iterable[str]:
        nonlocal output_tail
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffered = ""
        received = 0
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > MAX_ARCHIVE_LIST_BYTES:
                raise RuntimeError("压缩包清单输出超过64MB安全上限")
            decoded = decoder.decode(chunk)
            output_tail = (output_tail + decoded)[-4000:]
            buffered += decoded
            while "\n" in buffered:
                line, buffered = buffered.split("\n", 1)
                yield line.rstrip("\r")
        buffered += decoder.decode(b"", final=True)
        if buffered:
            yield buffered.rstrip("\r")

    try:
        inventory = parse_archive_inventory_lines(listing_lines(), cfg)
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise RuntimeError("7-Zip清单进程未正常退出") from exc
        if timed_out.is_set():
            raise RuntimeError("读取压缩包清单超过600秒")
        if returncode != 0:
            raise RuntimeError(
                "压缩包无法读取或可能已加密: " + output_tail.strip()[-500:]
            )
        return inventory
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        process.stdout.close()


def charge_archive_chain_budget(
    budget: dict[str, int], entries: int, expanded: int, cfg: dict
) -> None:
    """Limit cumulative work across all nested archives from one root archive."""
    proposed_entries = int(budget.get("entries", 0)) + max(0, int(entries))
    proposed_expanded = int(budget.get("expanded", 0)) + max(0, int(expanded))
    max_files = int(cfg["max_archive_files"])
    max_bytes = int(float(cfg["max_expanded_gb"]) * 1024**3)
    if proposed_entries > max_files:
        raise RuntimeError(
            f"嵌套压缩包累计条目数超过限制: {proposed_entries} > {max_files}"
        )
    if proposed_expanded > max_bytes:
        raise RuntimeError(
            "嵌套压缩包累计展开量超过限制: "
            f"{proposed_expanded / 1024**3:.2f}GB"
        )
    budget["entries"] = proposed_entries
    budget["expanded"] = proposed_expanded


def has_reparse_point(path: Path) -> bool:
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return path.is_symlink()
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def unique_unpack_target(archive: Path, suffix: str, digest: str) -> Path:
    stem = archive.name[:-len(suffix)] if suffix else archive.stem
    base = archive.parent / f"{stem}.unpacked-{digest[:8]}"
    candidate = base
    index = 2
    while candidate.exists():
        candidate = archive.parent / f"{base.name}-{index}"
        index += 1
    return candidate


def run_archive_extractor(
    command: list[str],
    *,
    timeout_seconds: int = 7200,
    heartbeat_seconds: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run 7-Zip while emitting sparse progress for the stall supervisor."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            stdout, stderr = process.communicate()
            detail = (stderr or stdout or "").strip()[-500:]
            raise RuntimeError(
                f"压缩包解压超过{timeout_seconds}秒，已停止7-Zip: {detail}"
            )
        try:
            stdout, stderr = process.communicate(
                timeout=min(float(heartbeat_seconds), remaining)
            )
            return subprocess.CompletedProcess(
                command,
                int(process.returncode or 0),
                stdout,
                stderr,
            )
        except subprocess.TimeoutExpired:
            print("压缩包仍在展开，7-Zip进程保持运行", flush=True)


def extract_archive(
    archive: Path,
    depth: int,
    cfg: dict,
    work_root: Path,
    chain_budget: dict[str, int] | None = None,
) -> Path:
    suffix = archive_suffix(archive, cfg)
    if not suffix:
        raise RuntimeError("无法识别压缩包扩展名")
    if depth > int(cfg["max_archive_depth"]):
        raise RuntimeError(f"压缩包嵌套超过{cfg['max_archive_depth']}层")
    wait_until_stable(archive)
    digest = sha256(archive)
    listed_entries, expanded_bytes = archive_inventory(archive, cfg)
    if chain_budget is not None:
        charge_archive_chain_budget(
            chain_budget, listed_entries, expanded_bytes, cfg
        )
    reserve_bytes = int(float(cfg.get("min_free_gb_after_extract", 0)) * 1024**3)
    available_bytes = shutil.disk_usage(work_root).free
    if expanded_bytes > max(0, available_bytes - reserve_bytes):
        raise RuntimeError(
            "压缩包展开后会使磁盘剩余空间低于"
            f"{cfg.get('min_free_gb_after_extract', 0)}GB"
        )
    stage = work_root / f"classify-{digest[:16]}"
    clean_job(stage, work_root)
    target = unique_unpack_target(archive, suffix, digest)
    try:
        result = run_archive_extractor(
            [
                str(cfg["archive_executable"]),
                "x",
                "-y",
                "-bd",
                "-bb0",
                "-sccUTF-8",
                f"-o{stage}",
                str(archive),
            ]
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-500:]
            raise RuntimeError(f"压缩包解压失败: {detail}")
        files = []
        total_size = 0
        for item in stage.rglob("*"):
            if has_reparse_point(item):
                raise RuntimeError(f"压缩包包含链接或重解析点: {item.name}")
            if item.is_file():
                files.append(item)
                total_size += item.stat().st_size
        if len(files) > int(cfg["max_archive_files"]):
            raise RuntimeError("压缩包实际文件数超过限制")
        if total_size > int(float(cfg["max_expanded_gb"]) * 1024**3):
            raise RuntimeError("压缩包实际展开量超过限制")
        if chain_budget is not None:
            charge_archive_chain_budget(
                chain_budget,
                max(0, len(files) - listed_entries),
                max(0, total_size - expanded_bytes),
                cfg,
            )
        if files:
            shutil.move(str(stage), str(target))
        else:
            shutil.rmtree(stage)
            target.mkdir(parents=False, exist_ok=False)
        if cfg.get("delete_archives_after_extract", False):
            guarded_unlink(
                archive,
                "删除已成功展开的原压缩包",
                expected_digest=digest,
            )
        else:
            archive_store = Path(cfg["archive_store"])
            archive_store.mkdir(parents=True, exist_ok=True)
            retained = archive_store / (
                f"{archive.stem}-{digest[:10]}{archive.suffix}"
            )
            counter = 2
            while retained.exists():
                retained = archive_store / (
                    f"{archive.stem}-{digest[:10]}-{counter}{archive.suffix}"
                )
                counter += 1
            shutil.move(str(archive), str(retained))
        return target
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def move_to_failed(path: Path, failed_root: Path) -> Path:
    digest = sha256(path)
    destination = unique_failed_path(path, digest, failed_root)
    shutil.move(str(path), str(destination))
    return destination


def move_reparse_to_failed(path: Path, failed_root: Path) -> Path:
    identity = f"{path}\0{path.resolve(strict=False)}".encode(
        "utf-8", errors="surrogatepass"
    )
    digest = hashlib.sha256(identity).hexdigest()
    destination = unique_failed_path(path, digest, failed_root)
    os.replace(path, destination)
    return destination


def remove_empty_inbox_directories(inbox: Path) -> int:
    removed = 0
    directories = sorted(
        [path for path in inbox.rglob("*") if path.is_dir()],
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def classify_inbox(cfg: dict) -> ClassificationStats:
    stats = ClassificationStats()
    inbox = cfg["folders"]["inbox"]
    failed = cfg["folders"]["failed"]
    work = cfg["folders"]["work"]
    classification = cfg["classification"]
    executable = classification["archive_executable"]
    reparse_items = sorted(
        [
            path
            for path in inbox.rglob("*")
            if path.name != ".gitkeep" and has_reparse_point(path)
        ],
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in reparse_items:
        try:
            destination = move_reparse_to_failed(path, failed)
            stats.unsupported_moved += 1
            print(f"链接或重解析点已保留到failed: {destination.name}")
        except Exception as exc:
            print(f"链接或重解析点无法移动，仍留在inbox: {path}: {exc}")
    queue = deque(
        (path, 0, str(path.resolve()))
        for path in inbox.rglob("*")
        if path.is_file()
        and path.name != ".gitkeep"
        and archive_suffix(path, classification)
    )
    if queue and not executable.is_file():
        raise RuntimeError(f"检测到压缩包但7-Zip不存在: {executable}")
    queued = {path.resolve() for path, _, _ in queue}
    chain_budgets: dict[str, dict[str, int]] = {
        root_key: {"entries": 0, "expanded": 0}
        for _, _, root_key in queue
    }
    while queue:
        archive, depth, root_key = queue.popleft()
        if not archive.exists():
            continue
        try:
            extracted = extract_archive(
                archive,
                depth,
                classification,
                work,
                chain_budget=chain_budgets[root_key],
            )
            stats.archives_extracted += 1
            for nested in extracted.rglob("*"):
                resolved = nested.resolve()
                if (
                    nested.is_file()
                    and archive_suffix(nested, classification)
                    and resolved not in queued
                ):
                    queue.append((nested, depth + 1, root_key))
                    queued.add(resolved)
            if classification.get("delete_archives_after_extract", False):
                print(f"压缩包已展开并永久删除原包: {archive.name}")
            else:
                print(f"压缩包已展开，原包已移至archives保留: {archive.name}")
        except Exception as exc:
            stats.archives_failed += 1
            try:
                destination = move_to_failed(archive, failed)
                print(
                    f"压缩包失败并保留: {archive.name} → "
                    f"{destination.name}: {exc}"
                )
            except Exception as move_error:
                print(
                    f"压缩包无法处理且仍留在inbox: {archive}: "
                    f"{exc}; 移动失败: {move_error}"
                )
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.name == ".gitkeep":
            continue
        if is_appledouble_path(path):
            try:
                destination = move_to_failed(path, failed)
                stats.unsupported_moved += 1
                print(
                    "macOS资源分叉文件不是题库正文，已保留到failed: "
                    f"{destination.name}"
                )
            except Exception as exc:
                print(f"macOS资源分叉文件无法移动，仍留在inbox: {path}: {exc}")
            continue
        if is_transient_download(path, classification):
            stats.transient_skipped += 1
            print(f"下载临时文件仍留在inbox等待完成: {path.name}")
            continue
        if is_video(path, classification):
            size = path.stat().st_size
            if classification.get("delete_videos", False):
                guarded_unlink(path, "删除不进行识别的视频文件")
                stats.videos_deleted += 1
                stats.video_bytes_deleted += size
                print(f"视频已永久删除且未识别: {path.name}")
            continue
        if path.suffix.casefold() in SUPPORTED:
            stats.supported_files += 1
            continue
        if classification.get("unsupported_to_failed", True):
            try:
                destination = move_to_failed(path, failed)
                stats.unsupported_moved += 1
                print(f"未知文件已保留到failed: {destination.name}")
            except Exception as exc:
                print(f"未知文件无法移动，仍留在inbox: {path}: {exc}")
    stats.empty_directories_removed = remove_empty_inbox_directories(inbox)
    return stats


def inbox_inventory(cfg: dict) -> dict[str, int]:
    counts = {
        "supported": 0,
        "videos": 0,
        "archives": 0,
        "unknown": 0,
        "transient": 0,
        "directories": 0,
    }
    classification = cfg["classification"]
    for path in cfg["folders"]["inbox"].rglob("*"):
        if path.is_dir():
            counts["directories"] += 1
        elif not path.is_file() or path.name == ".gitkeep":
            continue
        elif is_video(path, classification):
            counts["videos"] += 1
        elif is_transient_download(path, classification):
            counts["transient"] += 1
        elif archive_suffix(path, classification):
            counts["archives"] += 1
        elif path.suffix.casefold() in SUPPORTED:
            counts["supported"] += 1
        else:
            counts["unknown"] += 1
    return counts


def split_source(source: Path, job: Path, cfg: dict) -> list[SourcePart]:
    if source.suffix.lower() != ".pdf":
        if source.stat().st_size > cfg["max_mb"] * 1024 * 1024:
            raise RuntimeError("非PDF文件超过MinerU 200MB限制")
        target = job / source.name
        shutil.copy2(source, target)
        return [SourcePart(target, 0)]
    reader = PdfReader(str(source))
    pages = len(reader.pages)
    if pages <= cfg["max_pages"] and source.stat().st_size <= cfg["max_mb"] * 1024 * 1024:
        target = job / source.name
        shutil.copy2(source, target)
        return [SourcePart(target, 0)]
    limit = cfg["max_mb"] * 1024 * 1024
    parts, ranges = [], [
        (start, min(start + cfg["split_pages"], pages))
        for start in range(0, pages, cfg["split_pages"])
    ]
    while ranges:
        start, end = ranges.pop(0)
        out = job / f"{source.stem}.part-{start + 1}-{end}.pdf"
        writer = PdfWriter()
        for page in reader.pages[start:end]:
            writer.add_page(page)
        with out.open("wb") as f:
            writer.write(f)
        if out.stat().st_size > limit:
            out.unlink()
            if end - start == 1:
                raise RuntimeError("PDF单页超过MinerU 200MB限制")
            middle = start + (end - start) // 2
            ranges[:0] = [(start, middle), (middle, end)]
            continue
        parts.append(SourcePart(out, start))
    return parts


def mineru_failover_allowed(response: requests.Response, result: dict | None) -> bool:
    if response.status_code in {401, 403, 429}:
        return True
    if not result or result.get("code") == 0:
        return False
    message = json.dumps(result, ensure_ascii=False).casefold()
    markers = (
        "token", "auth", "unauthorized", "forbidden", "quota", "limit",
        "rate", "额度", "限流", "频率", "次数",
    )
    return any(marker in message for marker in markers)


def mineru_create_batch(
    group: list[SourcePart],
    tokens: dict[str, str],
    cfg: dict,
    preferred_slot: str,
) -> tuple[dict, str]:
    slots = [preferred_slot] + [
        slot for slot in tokens if slot != preferred_slot
    ]
    last_error = ""
    for index, slot in enumerate(slots):
        headers = {
            "Authorization": f"Bearer {tokens[slot]}",
            "Content-Type": "application/json",
        }
        payload = {
            "files": [
                {"name": part.path.name, "data_id": part_data_id(part)}
                for part in group
            ],
            "model_version": cfg["model_version"],
            "language": cfg["language"],
            "enable_formula": True,
            "enable_table": True,
        }
        try:
            response = requests.post(
                f"{cfg['base_url']}/file-urls/batch",
                headers=headers,
                json=payload,
                timeout=60,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise RuntimeError(
                "MinerU提交连接中断，无法确认任务是否已创建；"
                "为避免重复扣额度，本次不切换Key"
            ) from exc
        try:
            result = response.json()
        except requests.exceptions.JSONDecodeError:
            result = None
        has_fallback = index + 1 < len(slots)
        if has_fallback and mineru_failover_allowed(response, result):
            last_error = f"HTTP {response.status_code}"
            print(f"MinerU {slot} Key暂不可用，自动切换另一个Key")
            continue
        response.raise_for_status()
        if not result or result.get("code") != 0:
            raise RuntimeError(
                "MinerU提交失败: " + safe_provider_diagnostic(result)
            )
        data = result.get("data") or {}
        batch_id = str(data.get("batch_id") or "").strip()
        urls = data.get("file_urls")
        if not batch_id:
            raise RuntimeError("MinerU提交响应缺少batch_id，拒绝记录不可恢复任务")
        if (
            not isinstance(urls, list)
            or len(urls) != len(group)
            or any(not isinstance(url, str) or not url.strip() for url in urls)
        ):
            raise RuntimeError(
                "MinerU提交响应的上传地址数量或格式不正确，拒绝记录不完整任务"
            )
        return result, slot
    raise RuntimeError(f"所有MinerU Key都无法提交任务: {last_error}")


def mineru_submit(
    parts: list[SourcePart],
    tokens: dict[str, str],
    cfg: dict,
    on_batch_created=None,
    preferred_slot: str | None = None,
) -> list[MinerUBatch]:
    batches = []
    if preferred_slot not in tokens:
        preferred_slot = "primary" if "primary" in tokens else next(iter(tokens))
    for group_start in range(0, len(parts), 200):
        group = parts[group_start:group_start + 200]
        result, slot = mineru_create_batch(
            group, tokens, cfg, preferred_slot
        )
        data = result["data"]
        batch = MinerUBatch(str(data["batch_id"]), slot)
        batches.append(batch)
        if on_batch_created:
            on_batch_created(batch)
        urls = data["file_urls"]
        for part, url in zip(group, urls, strict=True):
            try:
                with part.path.open("rb") as f:
                    upload = requests.put(url, data=f, timeout=600)
                    upload.raise_for_status()
            except requests.RequestException as exc:
                raise MinerURetryLater(
                    "MinerU文件上传暂时失败，已保留任务等待自动重试: "
                    + safe_provider_diagnostic(
                        f"{type(exc).__name__}: {exc}"
                    )
                ) from None
        preferred_slot = slot
    return batches


def mineru_wait(
    batches: list[MinerUBatch], tokens: dict[str, str], cfg: dict
) -> list[dict]:
    """Poll every batch once so one cloud task cannot monopolize a worker.

    The outer processing loop revisits unfinished work after a bounded delay.
    This is important for a prequeued corpus: blocking inside one document for
    up to 24 hours would eventually occupy every local worker.
    """
    completed = []
    unfinished_states = Counter()
    for batch in batches:
        slot = batch.token_slot
        if slot not in tokens:
            raise RuntimeError(
                f"MinerU任务需要{slot} Key，但Key文件中该项为空"
            )
        headers = {"Authorization": f"Bearer {tokens[slot]}"}
        query_url = (
            f"{cfg['base_url']}/extract-results/batch/{batch.batch_id}"
        )
        last_connection_error: requests.RequestException | None = None
        r: requests.Response | None = None
        for attempt in range(1, 5):
            try:
                r = mineru_http_session().get(
                    query_url,
                    headers=headers,
                    timeout=60,
                )
                break
            except requests.RequestException as exc:
                last_connection_error = exc
                reset_mineru_http_session()
                if attempt < 4:
                    time.sleep(min(8, 2 ** (attempt - 1)))
        if r is None:
            raise MinerURetryLater(
                "MinerU历史任务轮询连接连续中断，"
                "已重建连接池并保留原任务等待下轮重试: "
                + safe_provider_diagnostic(
                    f"{type(last_connection_error).__name__}: "
                    f"{last_connection_error}"
                )
            )
        if r.status_code == 429:
            raise MinerURetryLater(
                f"MinerU {slot} Key轮询暂时限流，等待下一轮"
            )
        r.raise_for_status()
        result = r.json()
        if result.get("code") != 0:
            raise RuntimeError(
                "MinerU查询失败: " + safe_provider_diagnostic(result)
            )
        items = result["data"].get("extract_result", [])
        failed = [item for item in items if item.get("state") == "failed"]
        if failed:
            details = safe_provider_diagnostic(failed)
            if (
                "pages exceeds limit" in details.casefold()
                or "页数" in details and "超过" in details
            ):
                raise MinerURepartitionRequired(details)
            raise RuntimeError(f"MinerU解析失败: {details}")
        completed.extend(
            item for item in items if item.get("state") == "done"
        )
        unfinished_states.update(
            str(item.get("state") or "unknown")
            for item in items
            if item.get("state") != "done"
        )
        if not items:
            unfinished_states["empty"] += 1
    if not unfinished_states:
        return completed
    if set(unfinished_states) <= {"waiting-file"}:
        raise MinerUWaitingFile(
            f"MinerU等待文件上传: {dict(unfinished_states)}"
        )
    raise MinerURetryLater(
        f"MinerU云端尚未完成: {dict(unfinished_states)}"
    )


def encode_batch_ids(batches: list[MinerUBatch]) -> str:
    return json.dumps(
        [
            {"batch_id": batch.batch_id, "token_slot": batch.token_slot}
            for batch in batches
        ],
        ensure_ascii=False,
    )


def decode_batch_ids(value: str | None) -> list[MinerUBatch]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            return [MinerUBatch(batch_id, "primary") for batch_id in parsed]
        if isinstance(parsed, list) and all(isinstance(x, dict) for x in parsed):
            return [
                MinerUBatch(
                    str(item["batch_id"]),
                    str(item.get("token_slot") or "primary"),
                )
                for item in parsed
            ]
    except json.JSONDecodeError:
        pass
    return [MinerUBatch(value, "primary")]


def download_mineru_zip(
    url: str,
    target: Path,
    attempts: int = 4,
) -> None:
    """Download a MinerU archive with bounded retries and durable resume data."""
    cache_root = target.parent.parent / ".mineru-download-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(
        f"{target.parent.name}/{target.name}".encode("utf-8")
    ).hexdigest()
    partial = cache_root / f"{cache_key}.zip.part"
    failures: list[str] = []
    for attempt in range(1, max(1, attempts) + 1):
        resume_at = partial.stat().st_size if partial.is_file() else 0
        if resume_at > MAX_MINERU_RESULT_ZIP_BYTES:
            partial.unlink(missing_ok=True)
            raise RuntimeError("MinerU结果压缩包超过安全下载上限")
        headers = {"Accept-Encoding": "identity"}
        if resume_at:
            headers["Range"] = f"bytes={resume_at}-"
        try:
            with mineru_http_session().get(
                url,
                headers=headers,
                stream=True,
                timeout=(30, 120),
            ) as response:
                if response.status_code == 416:
                    partial.unlink(missing_ok=True)
                    failures.append("HTTP 416")
                    continue
                response.raise_for_status()
                append = bool(resume_at and response.status_code == 206)
                mode = "ab" if append else "wb"
                content_length = int(response.headers.get("Content-Length") or 0)
                expected_size = (resume_at if append else 0) + content_length
                if content_length and expected_size > MAX_MINERU_RESULT_ZIP_BYTES:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError("MinerU结果压缩包超过安全下载上限")
                written = resume_at if append else 0
                with partial.open(mode) as handle:
                    for block in response.iter_content(1024 * 1024):
                        if block:
                            written += len(block)
                            if written > MAX_MINERU_RESULT_ZIP_BYTES:
                                handle.close()
                                partial.unlink(missing_ok=True)
                                raise RuntimeError("MinerU结果压缩包超过安全下载上限")
                            handle.write(block)
            target.unlink(missing_ok=True)
            os.replace(partial, target)
            try:
                validate_mineru_zip(target)
            except RuntimeError:
                target.unlink(missing_ok=True)
                raise
            except (OSError, zipfile.BadZipFile) as exc:
                target.unlink(missing_ok=True)
                failures.append(f"invalid zip: {type(exc).__name__}")
                if attempt < attempts:
                    time.sleep(min(4, 2 ** (attempt - 1)))
                    continue
                raise
            return
        except (
            requests.RequestException,
            OSError,
            zipfile.BadZipFile,
        ) as exc:
            if isinstance(exc, requests.RequestException):
                reset_mineru_http_session()
            failures.append(
                safe_provider_diagnostic(f"{type(exc).__name__}: {exc}")
            )
            if attempt < attempts:
                time.sleep(min(8, 2 ** (attempt - 1)))
    raise MinerURetryLater(
        "MinerU结果下载连接中断，已保留断点并等待自动重试: "
        + " | ".join(failures[-2:])
    )


def validate_mineru_zip(archive_path: Path, output: Path | None = None) -> int:
    """Reject unsafe or unexpectedly large MinerU result archives."""
    expanded_bytes = 0
    root = output.resolve() if output is not None else None
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MINERU_RESULT_FILES:
            raise RuntimeError("MinerU结果压缩包文件数量超过安全上限")
        for member in members:
            raw_name = member.filename.replace("\\", "/")
            pure = PurePosixPath(raw_name)
            if (
                not raw_name
                or pure.is_absolute()
                or re.match(r"^[A-Za-z]:", raw_name)
                or ".." in pure.parts
            ):
                raise RuntimeError(f"MinerU结果包含不安全路径: {member.filename}")
            file_type = (member.external_attr >> 16) & 0o170000
            if file_type == stat.S_IFLNK:
                raise RuntimeError(f"MinerU结果包含符号链接: {member.filename}")
            expanded_bytes += max(0, int(member.file_size))
            if expanded_bytes > MAX_MINERU_RESULT_EXPANDED_BYTES:
                raise RuntimeError("MinerU结果压缩包展开大小超过安全上限")
            if root is not None:
                target = (output / Path(*pure.parts)).resolve()
                if target != root and root not in target.parents:
                    raise RuntimeError(
                        f"MinerU结果包含不安全路径: {member.filename}"
                    )
    return expanded_bytes


def extract_mineru_zip(archive_path: Path, output: Path) -> None:
    expanded_bytes = validate_mineru_zip(archive_path, output)
    free_bytes = shutil.disk_usage(output.parent).free
    if free_bytes - expanded_bytes < MIN_MINERU_RESULT_FREE_BYTES:
        raise RuntimeError(
            "MinerU结果解压后将使剩余空间低于512MB安全余量，拒绝解压"
        )
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(output)


def download_results(
    items: list[dict], parts: list[SourcePart], job: Path
) -> list[tuple[SourcePart, Path]]:
    by_data_id = {part_data_id(part): part for part in parts}
    by_name: dict[str, list[SourcePart]] = {}
    for part in parts:
        by_name.setdefault(part.path.name, []).append(part)
    markdowns = []
    seen_parts: set[str] = set()
    for index, item in enumerate(items, 1):
        data_id = item.get("data_id")
        matches = (
            [by_data_id[data_id]]
            if data_id in by_data_id
            else by_name.get(item.get("file_name"), [])
        )
        if len(matches) != 1:
            raise RuntimeError(
                "MinerU结果无法唯一对应源分卷: "
                + safe_provider_diagnostic(item)
            )
        part = matches[0]
        identity = part_data_id(part)
        if identity in seen_parts:
            raise RuntimeError(f"MinerU返回了重复分卷结果: {part.path.name}")
        seen_parts.add(identity)
        archive = job / f"result-{index}.zip"
        download_mineru_zip(item["full_zip_url"], archive)
        out = job / f"result-{index}"
        extract_mineru_zip(archive, out)
        found = list(out.rglob("full.md"))
        if len(found) != 1:
            raise RuntimeError(
                f"MinerU结果必须且只能包含一个full.md: {archive}"
            )
        markdowns.append((part, found[0]))
    expected_parts = {part_data_id(part) for part in parts}
    if len(markdowns) != len(parts) or seen_parts != expected_parts:
        raise RuntimeError(
            f"MinerU返回分卷数不一致: 期望{len(parts)}，实际{len(markdowns)}"
        )
    return sorted(markdowns, key=lambda pair: pair[0].start_page)


def download_recovered_results(
    items: list[dict],
    original_source: Path,
    job: Path,
) -> list[tuple[SourcePart, Path]]:
    """Download completed MinerU output when the historical source is missing."""
    mapped_items: list[tuple[dict, int]] = []
    seen_identities: set[str] = set()
    seen_explicit_start_pages: set[int] = set()
    for index, item in enumerate(items, 1):
        data_id = str(item.get("data_id") or "").strip()
        file_name = str(item.get("file_name") or original_source.name)
        result_url = str(item.get("full_zip_url") or "").strip()
        if not result_url:
            raise RuntimeError(
                "MinerU恢复结果缺少下载地址: "
                + safe_provider_diagnostic(item)
            )
        identity = f"data_id:{data_id}" if data_id else f"file_name:{file_name}"
        if identity in seen_identities:
            raise RuntimeError(f"MinerU恢复结果包含重复任务: {identity}")
        seen_identities.add(identity)
        page_match = re.search(r"\.part-(\d+)-\d+\.pdf$", file_name, re.I)
        start_page = max(0, int(page_match.group(1)) - 1) if page_match else index - 1
        if page_match and start_page in seen_explicit_start_pages:
            raise RuntimeError(
                f"MinerU恢复结果包含重复分卷起始页: {start_page + 1}"
            )
        if page_match:
            seen_explicit_start_pages.add(start_page)
        mapped_items.append((item, start_page))

    markdowns = []
    for index, (item, start_page) in enumerate(mapped_items, 1):
        archive = job / f"recovered-result-{index}.zip"
        download_mineru_zip(item["full_zip_url"], archive)
        out = job / f"recovered-result-{index}"
        extract_mineru_zip(archive, out)
        found = list(out.rglob("full.md"))
        if len(found) != 1:
            raise RuntimeError(
                f"MinerU恢复结果必须且只能包含一个full.md: {archive}"
            )
        markdowns.append((SourcePart(original_source, start_page), found[0]))
    return sorted(markdowns, key=lambda pair: pair[0].start_page)


def vision_cache_connection() -> sqlite3.Connection:
    """Use one lightweight SQLite reader/writer per image worker thread."""
    db = getattr(VISION_CACHE_LOCAL, "connection", None)
    if db is None:
        db = sqlite3.connect(ROOT / "state.db", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("""CREATE TABLE IF NOT EXISTS vision_description_cache(
            cache_key TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at INTEGER NOT NULL)""")
        db.commit()
        VISION_CACHE_LOCAL.connection = db
    return db


def vision_description_cache_key(
    image: Path,
    prompt: str,
    model: str,
) -> str:
    """Bind a cached description to exact pixels, context, model and prompt."""
    digest = hashlib.sha256()
    digest.update(
        f"question-bank-vision-description-v{MIMO_IMAGE_PROMPT_VERSION}\0".encode(
            "ascii"
        )
    )
    digest.update(model.encode("utf-8"))
    digest.update(b"\0")
    digest.update(prompt.encode("utf-8"))
    digest.update(b"\0")
    with image.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def vision_description_cache_get(cache_key: str) -> str:
    try:
        row = vision_cache_connection().execute(
            """SELECT description FROM vision_description_cache
            WHERE cache_key=?""",
            (cache_key,),
        ).fetchone()
        return str(row["description"]).strip() if row else ""
    except sqlite3.Error as exc:
        print(f"图片说明缓存读取跳过：{type(exc).__name__}")
        return ""


def vision_description_cache_put(
    cache_key: str,
    model: str,
    description: str,
) -> None:
    description = description.strip()
    if not description:
        return
    db: sqlite3.Connection | None = None
    try:
        db = vision_cache_connection()
        db.execute(
            """INSERT OR IGNORE INTO vision_description_cache(
                cache_key,model,description,created_at
            ) VALUES(?,?,?,?)""",
            (cache_key, model, description, int(time.time())),
        )
        db.commit()
    except sqlite3.Error as exc:
        if db is not None and db.in_transaction:
            db.rollback()
        print(f"图片说明缓存写入跳过：{type(exc).__name__}")


def clean_model_description(response: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json|markdown|text)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            text = str(decoded.get("description", "")).strip()
        elif isinstance(decoded, str):
            text = decoded.strip()
    except json.JSONDecodeError:
        match = re.search(
            r'["\']description["\']\s*:\s*["\'](?P<value>.+?)["\']\s*[},]?',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if match:
            text = match.group("value").replace(r"\"", '"').strip()
    return re.sub(r"\s+", " ", text).strip()


def ollama_description(
    model: str, prompt: str, base_url: str, image: Path | None = None
) -> str:
    """让小视觉模型直接返回文字，避免0.8B模型因JSON格式失败而误伤文件。"""
    last_error: Exception | None = None
    total_gb, available_gb = windows_memory_gb()
    keep_alive = (
        "0s" if available_gb < max(1.5, total_gb * 0.10) else "30s"
    )
    with LOCAL_MODEL_LOCK:
        for _ in range(2):
            payload = {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "keep_alive": keep_alive,
                "options": {"temperature": 0, "num_predict": 160},
            }
            if image:
                payload["images"] = [base64.b64encode(image.read_bytes()).decode()]
            try:
                r = requests.post(
                    f"{base_url}/api/generate", json=payload, timeout=600
                )
                r.raise_for_status()
                description = clean_model_description(str(r.json()["response"]))
                if description:
                    return description
            except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                time.sleep(1)
            prompt += "\n上一次没有给出说明。请直接输出一段非空中文说明。"
    if isinstance(last_error, requests.RequestException):
        raise RuntimeError(f"Ollama临时失败，等待自动重试: {last_error}")
    raise RuntimeError(f"视觉模型连续两次未返回非空说明: {model}")


def mimo_description(
    prompt: str, image: Path, cfg: dict, metrics: dict
) -> str:
    """使用MiMo-V2.5多模态API，多Key轮换并自动故障转移。"""
    keys, ordered = ordered_mimo_slots(cfg)
    if not keys:
        raise RuntimeError("尚未填写MiMo API Key")
    mime = {
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(image.suffix.casefold(), "image/jpeg")
    data_url = (
        f"data:{mime};base64,"
        + base64.b64encode(image.read_bytes()).decode("ascii")
    )
    payload = {
        "model": cfg["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是题库图片转写助手。只忠实描述图片内容，不解题，"
                    "不补造图片中不存在的信息。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            },
        ],
        "max_completion_tokens": int(cfg.get("max_completion_tokens", 256)),
        "temperature": 0,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    failures = []
    for slot in ordered:
        try:
            with mimo_key_request_context(slot, cfg) as session:
                response = session.post(
                    f"{cfg['base_url'].rstrip('/')}/chat/completions",
                    headers={
                        "api-key": keys[slot],
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=int(cfg.get("timeout_seconds", 180)),
                )
            if (
                response.status_code in {401, 403, 429}
                or response.status_code >= 500
            ):
                if response.status_code == 429:
                    cooldown = int(
                        cfg.get("rate_limit_cooldown_seconds", 120)
                    )
                    note_mimo_key_rate_limit(slot, cooldown)
                    note_mimo_rate_limit(
                        cooldown
                    )
                failures.append(f"{slot}: HTTP {response.status_code}")
                continue
            response.raise_for_status()
            result = response.json()
            description = clean_model_description(
                str(result["choices"][0]["message"]["content"])
            )
            if not description:
                failures.append(f"{slot}: 返回内容为空")
                continue
            usage = result.get("usage") or {}
            prompt_details = usage.get("prompt_tokens_details") or {}
            metrics["mimo_prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            metrics["mimo_completion_tokens"] += int(
                usage.get("completion_tokens") or 0
            )
            metrics["mimo_image_tokens"] += int(
                prompt_details.get("image_tokens") or 0
            )
            metrics["mimo_key_slots"] = sorted(
                set(metrics.get("mimo_key_slots") or []) | {slot}
            )
            note_mimo_key_success(slot)
            note_mimo_success()
            return description
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            failures.append(f"{slot}: {type(exc).__name__}")
    raise RuntimeError("MiMo全部已配置Key暂不可用: " + "; ".join(failures))


def merge_mimo_usage(metrics: dict, result: dict, slot: str) -> None:
    usage = result.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    metrics["mimo_prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
    metrics["mimo_completion_tokens"] += int(
        usage.get("completion_tokens") or 0
    )
    metrics["mimo_image_tokens"] += int(
        prompt_details.get("image_tokens") or 0
    )
    metrics["mimo_key_slots"] = sorted(
        set(metrics.get("mimo_key_slots") or []) | {slot}
    )


def parse_mimo_batch_descriptions(
    content: str, expected_ids: list[str]
) -> dict[str, str]:
    """Accept a batch only when every expected image has one exact description."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE
        )
        text = re.sub(r"\s*```$", "", text)
    decoded = json.loads(text)
    items = (
        decoded.get("descriptions")
        if isinstance(decoded, dict)
        else decoded
    )
    if not isinstance(items, list):
        raise ValueError("批量结果不是descriptions数组")
    descriptions: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("批量结果项目不是对象")
        image_id = str(item.get("image_id") or "").strip()
        description = clean_model_description(
            str(item.get("description") or "")
        )
        if (
            not image_id
            or image_id in descriptions
            or not description
        ):
            raise ValueError("批量结果存在空值或重复编号")
        descriptions[image_id] = description
    if set(descriptions) != set(expected_ids):
        raise ValueError("批量结果编号与请求不完全一致")
    return {image_id: descriptions[image_id] for image_id in expected_ids}


def mimo_batch_descriptions(
    tasks: list[dict], cfg: dict, metrics: dict
) -> dict[str, str]:
    """Describe 2-4 images in one request; never accept partial/misaligned output."""
    if not 2 <= len(tasks) <= 4:
        raise ValueError("MiMo批量图片数必须为2至4")
    keys, ordered = ordered_mimo_slots(cfg)
    if not keys:
        raise RuntimeError("尚未填写MiMo API Key")
    expected_ids = [str(task["batch_image_id"]) for task in tasks]
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "下面按编号提供多张彼此独立的题库图片。逐张忠实描述，"
                "不要解题、不要合并图片内容。最后只能输出指定JSON。"
            ),
        }
    ]
    for task in tasks:
        image = task["model_image"]
        mime = {
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(image.suffix.casefold(), "image/jpeg")
        data_url = (
            f"data:{mime};base64,"
            + base64.b64encode(image.read_bytes()).decode("ascii")
        )
        content.extend(
            [
                {
                    "type": "text",
                    "text": (
                        f"IMAGE_ID={task['batch_image_id']}\n"
                        f"{task['prompt']}"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                },
            ]
        )
    content.append(
        {
            "type": "text",
            "text": (
                '严格输出：{"descriptions":['
                '{"image_id":"原编号","description":"不超过150字的中文说明"}'
                "]}。每个输入编号必须且只能出现一次，不得输出其他文字。"
            ),
        }
    )
    payload = {
        "model": cfg["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是题库图片转写助手。图片相互独立。只忠实描述，"
                    "不补造信息；严格保持IMAGE_ID与对应图片关系。"
                ),
            },
            {"role": "user", "content": content},
        ],
        "max_completion_tokens": min(
            int(cfg.get("image_batch_max_completion_tokens", 1024)),
            int(cfg.get("max_completion_tokens", 256)) * len(tasks),
        ),
        "temperature": 0,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    failures = []
    for slot in ordered:
        try:
            with mimo_key_request_context(slot, cfg) as session:
                response = session.post(
                    f"{cfg['base_url'].rstrip('/')}/chat/completions",
                    headers={
                        "api-key": keys[slot],
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=int(cfg.get("timeout_seconds", 180)),
                )
            if response.status_code == 429:
                cooldown = int(
                    cfg.get("rate_limit_cooldown_seconds", 120)
                )
                note_mimo_key_rate_limit(slot, cooldown)
                note_mimo_rate_limit(
                    cooldown
                )
                MIMO_BATCH_SIZER.penalize("检测到429限流")
                failures.append(f"{slot}: HTTP 429")
                continue
            if response.status_code in {400, 413}:
                MIMO_BATCH_SIZER.penalize(
                    f"批量请求HTTP {response.status_code}"
                )
                failures.append(f"{slot}: HTTP {response.status_code}")
                break
            if response.status_code in {401, 403} or response.status_code >= 500:
                failures.append(f"{slot}: HTTP {response.status_code}")
                continue
            response.raise_for_status()
            result = response.json()
            merge_mimo_usage(metrics, result, slot)
            descriptions = parse_mimo_batch_descriptions(
                str(result["choices"][0]["message"]["content"]),
                expected_ids,
            )
            note_mimo_key_success(slot)
            note_mimo_success()
            MIMO_BATCH_SIZER.reward(len(tasks))
            return descriptions
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            MIMO_BATCH_SIZER.penalize("返回编号校验未通过")
            failures.append(f"{slot}: {type(exc).__name__}")
        except requests.RequestException as exc:
            MIMO_BATCH_SIZER.penalize("批量请求超时或连接失败")
            failures.append(f"{slot}: {type(exc).__name__}")
    raise RuntimeError("MiMo批量描述未通过安全校验: " + "; ".join(failures))


def vision_description(
    prompt: str, image: Path, cfg: dict, metrics: dict
) -> str:
    mimo_cfg = cfg.get("mimo") or {}
    fallback = bool(mimo_cfg.get("fallback_to_ollama", False))
    if mimo_cfg.get("enabled", True):
        if not mimo_keys():
            if not fallback:
                raise RuntimeError(
                    "MiMo是唯一图片理解模型，但尚未配置有效Key"
                )
        else:
            try:
                with mimo_request_context(mimo_cfg):
                    return mimo_description(prompt, image, mimo_cfg, metrics)
            except RuntimeError as exc:
                if not fallback:
                    raise
                print(f"MiMo暂不可用，自动回退本地视觉模型: {exc}")
    if fallback:
        return ollama_description(
            cfg["vision_model"], prompt, cfg["base_url"], image
        )
    raise RuntimeError(
        "MiMo图片理解未启用，且本地视觉模型回退已禁用"
    )


def normalize_image_ref(ref: str) -> str:
    ref = unquote(ref.strip().strip("<>")).replace("\\", "/")
    return ref.removeprefix("./")


def image_entries(result_dir: Path) -> dict[str, dict | None]:
    entries: dict[str, dict | None] = {}
    content_files = list(result_dir.rglob("*content_list.json"))
    for path in content_files:
        try:
            content = json.loads(path.read_text("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"MinerU内容列表无法读取: {path}") from exc
        for item in content if isinstance(content, list) else []:
            ref = item.get("img_path") or item.get("image_path")
            if ref:
                normalized = normalize_image_ref(ref)
                entries[normalized] = item
                basename = Path(normalized).name
                if basename not in entries:
                    entries[basename] = item
                elif entries[basename] is not item:
                    entries[basename] = None
    return entries


def render_pdf_page(
    pdf: Path, page_index: int, output: Path, dpi: int, bbox: list[float] | None = None
) -> Path:
    document = pdfium.PdfDocument(str(pdf))
    try:
        pages = len(document)
        if not 0 <= page_index < pages:
            raise RuntimeError(
                f"MinerU图片页码超出分卷范围: page_idx={page_index}, pages={pages}"
            )
        page = document[page_index]
        try:
            page_width, page_height = page.get_size()
            scale = max(1, dpi) / 72
            bitmap = page.render(scale=scale)
            try:
                rendered = bitmap.to_pil().copy()
            finally:
                bitmap.close()
        finally:
            page.close()
        try:
            if bbox and len(bbox) == 4:
                x0, y0, x1, y1 = map(float, bbox)
                if max(bbox) <= 1.5:
                    x0, x1 = x0 * page_width, x1 * page_width
                    y0, y1 = y0 * page_height, y1 * page_height
                elif max(bbox) <= 1000.5:
                    x0, x1 = x0 * page_width / 1000, x1 * page_width / 1000
                    y0, y1 = y0 * page_height / 1000, y1 * page_height / 1000
                else:
                    raise RuntimeError(f"无法识别MinerU图片坐标范围: {bbox}")
                if not (
                    x1 - x0 > 1
                    and y1 - y0 > 1
                    and x0 >= 0
                    and y0 >= 0
                    and x1 <= page_width * 1.25
                    and y1 <= page_height * 1.25
                ):
                    raise RuntimeError(f"MinerU图片坐标无效: {bbox}")
                padding = 8
                left = max(0, round((x0 - padding) * scale))
                top = max(0, round((y0 - padding) * scale))
                right = min(rendered.width, round((x1 + padding) * scale))
                bottom = min(rendered.height, round((y1 + padding) * scale))
                if right - left <= 1 or bottom - top <= 1:
                    raise RuntimeError(f"MinerU图片坐标裁剪后为空: {bbox}")
                cropped = rendered.crop((left, top, right, bottom))
                rendered.close()
                rendered = cropped
            output.parent.mkdir(parents=True, exist_ok=True)
            rendered.save(output)
        finally:
            rendered.close()
    finally:
        document.close()
    return output


def prepare_model_image(image: Path, job: Path, max_side: int) -> tuple[Path, bool]:
    """限制OCR和视觉模型输入尺寸，避免超大图片浪费CPU、显存和时间。"""
    if max_side <= 0:
        return image, False
    with Image.open(image) as source_image:
        longest = max(source_image.size)
        if longest <= max_side:
            return image, False
        resized = source_image.copy()
        resized.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if resized.mode != "RGB":
            converted = resized.convert("RGB")
            resized.close()
            resized = converted
        output = job / (
            "model-"
            + hashlib.sha256(str(image).encode("utf-8")).hexdigest()[:12]
            + ".jpg"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            resized.save(output, format="JPEG", quality=90)
        finally:
            resized.close()
    return output, True


def recover_non_pdf_image(
    source: Path,
    ref: str,
    job: Path,
) -> tuple[Path | None, bool]:
    image_extensions = {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".jp2",
    }
    if source.suffix.casefold() in image_extensions:
        return source, False
    if source.suffix.casefold() not in {".docx", ".pptx"}:
        return None, False
    if not zipfile.is_zipfile(source):
        return None, False
    prefix = "word/media/" if source.suffix.casefold() == ".docx" else "ppt/media/"
    with zipfile.ZipFile(source) as archive:
        members = [
            item
            for item in archive.infolist()
            if not item.is_dir()
            and item.filename.casefold().startswith(prefix)
            and Path(item.filename).suffix.casefold() in image_extensions
        ]
        wanted = Path(ref).name.casefold()
        exact = [
            item for item in members
            if Path(item.filename).name.casefold() == wanted
        ]
        selected = exact[0] if len(exact) == 1 else (
            members[0] if len(members) == 1 else None
        )
        if not selected:
            return None, False
        output = job / (
            "embedded-" + hashlib.sha256(
                selected.filename.encode("utf-8")
            ).hexdigest()[:10] + Path(selected.filename).suffix.casefold()
        )
        with archive.open(selected) as source_file, output.open("wb") as target:
            shutil.copyfileobj(source_file, target)
    return output, True


def image_rule_decision(context: str) -> bool | None:
    normalized = context.casefold()
    if any(term.casefold() in normalized for term in IMPORTANT_IMAGE_TERMS):
        return True
    if any(term.casefold() in normalized for term in IGNORED_IMAGE_TERMS):
        return False
    return None


def ocr_image(image: Path, cfg: dict) -> tuple[str, float]:
    """使用现成PP-OCRv5 Mobile提取图片文字；模型按需加载一次。"""
    if not cfg.get("ocr_enabled", True):
        return "", 0.0
    started = time.perf_counter()
    global OCR_ENGINE
    with OCR_ENGINE_LOCK:
        if OCR_ENGINE is None:
            from rapidocr import (
                EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR,
            )

            OCR_ENGINE = RapidOCR(
                params={
                    "Det.engine_type": EngineType.ONNXRUNTIME,
                    "Det.lang_type": LangDet.CH,
                    "Det.model_type": ModelType.MOBILE,
                    "Det.ocr_version": OCRVersion.PPOCRV5,
                    "Rec.engine_type": EngineType.ONNXRUNTIME,
                    "Rec.lang_type": LangRec.CH,
                    "Rec.model_type": ModelType.MOBILE,
                    "Rec.ocr_version": OCRVersion.PPOCRV5,
                }
            )
        result = OCR_ENGINE(str(image))
    texts = [
        str(text).strip()
        for text in (getattr(result, "txts", None) or [])
        if str(text).strip()
    ]
    return "\n".join(texts), time.perf_counter() - started


def ocr_has_enough_text(text: str, minimum: int) -> bool:
    meaningful = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    return len(meaningful) >= minimum


def repair_images(
    md: str,
    md_file: Path,
    source_pdf: Path,
    job: Path,
    cfg: dict,
    metrics: dict,
) -> str:
    entries = image_entries(md_file.parent)
    result_root = md_file.parent.resolve()
    matches = list(IMAGE_RE.finditer(md))
    replacements: dict[int, str] = {}
    vision_tasks: list[dict] = []

    def cleanup_task_images(task: dict) -> None:
        model_image = task["model_image"]
        source_image = task["source_image"]
        if task["resized"]:
            model_image.unlink(missing_ok=True)
        if task["rendered"]:
            source_image.unlink(missing_ok=True)

    try:
        for image_index, match in enumerate(matches):
            metrics["image_placeholders"] += 1
            raw_ref = match.group(1).strip()
            ref = (
                raw_ref[1:raw_ref.find(">")]
                if raw_ref.startswith("<") and ">" in raw_ref
                else raw_ref.split(maxsplit=1)[0]
            )
            ref = normalize_image_ref(ref.split("?")[0])
            actual = (md_file.parent / ref).resolve()
            actual_safe = actual == result_root or result_root in actual.parents
            context = md[
                max(0, match.start() - cfg["context_chars"]):
                min(len(md), match.end() + cfg["context_chars"])
            ]
            important = image_rule_decision(context)
            if important is not None:
                metrics["rule_decisions"] += 1
            replacements[image_index] = ""
            if important is None:
                # 极速模式：题目前后没有“如图”等依赖信号时不做OCR或视觉推理，
                # 避免把页眉、装饰和普通插图扩大成数十万次模型调用。
                metrics["ignored_images"] += 1
                continue
            if not important:
                metrics["ignored_images"] += 1
                continue

            image = actual if actual_safe and actual.is_file() else None
            rendered = False
            if image is None and not source_pdf.exists():
                metrics["important_images"] += 1
                replacements[image_index] = (
                    "\n> 题目图片说明：原始源文件历史缺失，"
                    "MinerU结果未包含可恢复的对应图片。\n"
                )
                continue
            if image is None and source_pdf.suffix.lower() != ".pdf":
                image, rendered = recover_non_pdf_image(
                    source_pdf, ref, job
                )
            if image is None and source_pdf.suffix.lower() == ".pdf":
                entry = entries.get(ref)
                if entry is None:
                    entry = entries.get(Path(ref).name)
                if not entry:
                    raise RuntimeError(
                        f"重要图片缺少页码或坐标元数据: {ref}"
                    )
                page_value = entry.get("page_idx", entry.get("page_index"))
                if page_value is None:
                    raise RuntimeError(f"重要图片缺少页码元数据: {ref}")
                page_index = int(page_value)
                # 同一页可能出现多个不同裁剪框。文件名必须包含占位序号，
                # 否则并行准备时后一个裁剪会覆盖前一个。
                image = render_pdf_page(
                    source_pdf,
                    page_index,
                    job / f"page-{page_index + 1}-{image_index + 1}.jpg",
                    cfg["render_dpi"],
                    entry.get("bbox"),
                )
                rendered = True
            if image is None:
                raise RuntimeError(f"重要图片无法从原文件取回: {ref}")
            model_image, resized = prepare_model_image(
                image, job, int(cfg.get("max_image_side", 1600))
            )
            ocr_text = ""
            if cfg.get("ocr_enabled", False) and model_image is not None:
                try:
                    ocr_text, elapsed = ocr_image(model_image, cfg)
                    metrics["ocr_images"] += 1
                    metrics["ocr_seconds"] = round(
                        metrics["ocr_seconds"] + elapsed, 3
                    )
                except Exception as exc:
                    print(f"轻量OCR跳过，将按图片规则继续: {ref}: {exc}")
            text_image = ocr_has_enough_text(
                ocr_text, int(cfg.get("ocr_min_chars", 12))
            )
            diagram = any(
                term.casefold() in context.casefold()
                for term in DIAGRAM_IMAGE_TERMS
            )
            if text_image and not diagram:
                metrics["important_images"] += 1
                replacements[image_index] = f"\n> 题目图片文字：{ocr_text}\n"
                cleanup_task_images(
                    {
                        "model_image": model_image,
                        "source_image": image,
                        "resized": resized,
                        "rendered": rendered,
                    }
                )
                continue

            metrics["important_images"] += 1
            prompt = (
                "这是题库中的必要图形。结合题目前后文字，忠实描述图中的文字、数字、公式、"
                "几何关系、坐标、电路或实验装置；不要解题。"
                "用不超过150个汉字直接输出说明，不要JSON，不要Markdown标题。\n上下文："
                + context
            )
            if ocr_text:
                prompt += "\nOCR参考文字（可能有误）：" + ocr_text
            mimo_cfg = cfg.get("mimo") or {}
            cache_model = (
                str(mimo_cfg.get("model") or "mimo")
                if mimo_cfg.get("enabled", True)
                else str(cfg.get("vision_model") or "local-vision")
            )
            if mimo_cfg.get("fallback_to_ollama", False):
                cache_model += (
                    "|fallback:" + str(cfg.get("vision_model") or "")
                )
            cache_key = vision_description_cache_key(
                model_image,
                prompt,
                cache_model,
            )
            cached_description = vision_description_cache_get(cache_key)
            if cached_description:
                metrics["vision_cache_hits"] += 1
                replacements[image_index] = (
                    f"\n> 题目图片说明：{cached_description}\n"
                )
                cleanup_task_images(
                    {
                        "model_image": model_image,
                        "source_image": image,
                        "resized": resized,
                        "rendered": rendered,
                    }
                )
                continue
            metrics["vision_cache_misses"] += 1
            metrics["vision_images"] += 1
            vision_tasks.append(
                {
                    "image_index": image_index,
                    "batch_image_id": f"img-{image_index + 1:06d}",
                    "ref": ref,
                    "prompt": prompt,
                    "model_image": model_image,
                    "source_image": image,
                    "resized": resized,
                    "rendered": rendered,
                    "cache_key": cache_key,
                    "cache_model": cache_model,
                }
            )

        if vision_tasks:
            mimo_cfg = cfg.get("mimo") or {}
            parallel_cap = configured_mimo_parallel_ceiling(mimo_cfg)
            batch_size = max(
                1,
                min(
                    4,
                    MIMO_BATCH_SIZER.current(),
                    int(mimo_cfg.get("image_batch_max", 4)),
                ),
            )
            task_batches = [
                vision_tasks[index:index + batch_size]
                for index in range(0, len(vision_tasks), batch_size)
            ]
            workers = max(1, min(len(task_batches), parallel_cap))
            if workers > 1:
                print(
                    f"MiMo图片独立队列：重要图片{len(vision_tasks)}｜"
                    f"单请求{batch_size}张｜弹性并发上限{workers}｜"
                    f"每Key最多约{max(1, int(mimo_cfg.get('parallel_per_key', 1)))}个在途请求｜"
                    "严格编号校验后按原位置写回"
                )

            def describe_batch(
                batch: list[dict],
            ) -> tuple[dict[str, str], float, dict]:
                batch_metrics = file_metrics(None)
                started = time.perf_counter()
                descriptions = {}
                # Another request may have reduced the global batch size after
                # this future was queued. Re-split immediately before sending
                # so queued four-image work cannot ignore a 4→2→1 penalty.
                allowed = max(1, min(4, MIMO_BATCH_SIZER.current()))
                sub_batches = [
                    batch[index:index + allowed]
                    for index in range(0, len(batch), allowed)
                ]
                for sub_batch in sub_batches:
                    if len(sub_batch) == 1:
                        task = sub_batch[0]
                        descriptions[
                            task["batch_image_id"]
                        ] = vision_description(
                            task["prompt"],
                            task["model_image"],
                            cfg,
                            batch_metrics,
                        )
                        continue
                    try:
                        with mimo_request_context(mimo_cfg):
                            descriptions.update(
                                mimo_batch_descriptions(
                                    sub_batch, mimo_cfg, batch_metrics
                                )
                            )
                    except RuntimeError as exc:
                        # No batch output has been written yet. Retry each image
                        # independently so malformed or misaligned responses can
                        # never contaminate another placeholder.
                        print(
                            "MiMo批量安全回退逐图处理："
                            f"{len(sub_batch)}张｜{exc}"
                        )
                        for task in sub_batch:
                            descriptions[
                                task["batch_image_id"]
                            ] = vision_description(
                                task["prompt"],
                                task["model_image"],
                                cfg,
                                batch_metrics,
                            )
                return (
                    descriptions,
                    time.perf_counter() - started,
                    batch_metrics,
                )

            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="mimo-image",
            ) as vision_executor:
                future_batches = [
                    (vision_executor.submit(describe_batch, batch), batch)
                    for batch in task_batches
                ]
                # Batches execute concurrently, but consuming them in source
                # order keeps Markdown output deterministic.
                for future, batch in future_batches:
                    descriptions, elapsed, task_metrics = future.result()
                    for task in batch:
                        desc = descriptions.get(task["batch_image_id"], "")
                        if not desc:
                            raise RuntimeError(
                                f"视觉模型未返回已校验描述: {task['ref']}"
                            )
                        vision_description_cache_put(
                            task["cache_key"],
                            task["cache_model"],
                            desc,
                        )
                        replacements[task["image_index"]] = (
                            f"\n> 题目图片说明：{desc}\n"
                        )
                    metrics["vision_seconds"] = round(
                        metrics["vision_seconds"] + elapsed, 3
                    )
                    for key in (
                        "mimo_prompt_tokens",
                        "mimo_completion_tokens",
                        "mimo_image_tokens",
                    ):
                        metrics[key] += int(task_metrics.get(key) or 0)
                    metrics["mimo_key_slots"] = sorted(
                        set(metrics.get("mimo_key_slots") or [])
                        | set(task_metrics.get("mimo_key_slots") or [])
                    )
    finally:
        for task in vision_tasks:
            cleanup_task_images(task)

    pieces = []
    cursor = 0
    for image_index, match in enumerate(matches):
        pieces.append(md[cursor:match.start()])
        pieces.append(replacements.get(image_index, ""))
        cursor = match.end()
    pieces.append(md[cursor:])
    return "".join(pieces)


ROLE_TERMS_RE = re.compile(
    r"(参考答案|答案解析|答案|解析|详解|试题|题目|练习题|练习|习题|试卷"
    r"|questions?|solutions?|answers?|problems?|texts?)",
    re.IGNORECASE,
)
ANSWER_FILE_RE = re.compile(
    r"(参考答案|答案解析|答案|解析|详解|solutions?|answers?)",
    re.IGNORECASE,
)
COMBINED_FILE_RE = re.compile(
    r"(含答案|带答案|附答案|及答案|真题解析|试题答案汇编"
    r"|(?:试题|题目|试卷|真题).{0,8}(?:及|和|与|\+).{0,4}(?:参考答案|答案|解析)"
    r"|(?:problems?|questions?).{0,8}(?:and|&).{0,8}(?:solutions?|answers?)"
    r"|(?:solutions?|answers?).{0,8}(?:and|&).{0,8}(?:problems?|questions?)"
    r"|讲评(?:版|版本)?)",
    re.IGNORECASE,
)
NUMBERED_UNIT_RE = re.compile(
    r"(?m)^(?P<prefix>[ \t]{0,3}(?:#{1,6}[ \t]+)?)"
    r"(?P<label>(?:第[ \t]*)?(?P<number>\d{1,4})[ \t]*(?:题|[.．、]))"
    r"[ \t]*(?P<tail>[^\n]*)$"
)
PAREN_UNIT_RE = re.compile(
    r"(?m)^(?P<prefix>[ \t]{0,3}(?:#{1,6}[ \t]+)?)"
    r"(?P<label>[（(][ \t]*(?P<number>\d{1,4})[ \t]*[）)])"
    r"[ \t]*(?P<tail>[^\n]*)$"
)
ANSWER_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:【)?答案(?:】)?[ \t]*[:：]?[ \t]*"
)
EXPLANATION_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:【)?(?:解析|详解)(?:】)?[ \t]*[:：]?[ \t]*"
)


def normalized_source_stem(path: Path) -> str:
    original_stem = path.stem
    stem = ROLE_TERMS_RE.sub("", original_stem)
    stem = re.sub(r"\.unpacked-[0-9a-f]{8}(?:-\d+)?$", "", stem, flags=re.I)
    stem = re.sub(r"[\s_\-—（）()\[\]【】]+", "", stem)
    catalog = re.match(
        r"^(?:[a-z]\d{2,3}|\d{2,3})(?P<rest>.+)$",
        stem,
        re.IGNORECASE,
    )
    if (
        ROLE_TERMS_RE.search(original_stem)
        and catalog
        and len(catalog.group("rest")) >= 4
    ):
        stem = catalog.group("rest")
    return stem.casefold() or path.stem.casefold()


def source_group_key(source: Path, inbox: Path) -> tuple[str, str]:
    relative = source.relative_to(inbox)
    normalized_parent_parts = [
        part.casefold() for part in relative.parts[:-1]
    ]
    base = normalized_source_stem(source)
    parent_identity = "/".join(normalized_parent_parts)
    identity = f"path:{parent_identity}\0stem:{base}"
    parent_name = (
        re.sub(
            r"\.unpacked-[0-9a-f]{8}(?:-\d+)?$",
            "",
            relative.parts[-2],
            flags=re.I,
        )
        if len(relative.parts) > 1
        else ""
    )
    display = f"{parent_name}-{base}".strip("-") or source.stem
    group_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return group_id, display


DOCUMENT_TYPE_PRIORITY = ("试卷", "习题册", "教材", "讲义")
MODULE_ORDER = (
    "数学基础", "力学", "电磁学", "光学", "热学", "近代物理"
)
CLASSIFICATION_TYPES = set(DOCUMENT_TYPE_PRIORITY) | {"其他资料", "待分类"}
CLASSIFICATION_MODULES = set(MODULE_ORDER) | {"综合"}


def classification_to_dict(result: DocumentClassification) -> dict:
    return {
        "document_type": result.document_type,
        "institution": result.institution,
        "primary_module": result.primary_module,
        "module_tags": list(result.module_tags),
        "classification_method": result.method,
        "classification_confidence": round(result.confidence, 4),
        "evidence": list(result.evidence),
        "classification_version": result.version,
    }


def classification_from_dict(value: dict) -> DocumentClassification:
    return DocumentClassification(
        str(value["document_type"]),
        str(value.get("institution") or "未知机构"),
        str(value.get("primary_module") or "综合"),
        tuple(str(item) for item in value.get("module_tags") or []),
        str(value.get("classification_method") or "cached"),
        float(value.get("classification_confidence") or 0),
        tuple(str(item) for item in value.get("evidence") or []),
        int(value.get("classification_version") or 1),
    )


def classification_line(result: DocumentClassification) -> str:
    institution = (
        "不适用" if result.document_type == "教材" else result.institution
    )
    tags = "、".join(result.module_tags) or result.primary_module
    return (
        f"分类：{result.document_type}｜机构：{institution}｜"
        f"主模块：{result.primary_module}｜模块标签：{tags}"
    )


def safe_path_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" .")
    return cleaned or fallback


def classification_directory(
    markdown_root: Path,
    result: DocumentClassification,
    create: bool = True,
) -> Path:
    if result.document_type == "教材":
        destination = markdown_root / "教材" / result.primary_module
    elif result.document_type in {"试卷", "习题册", "讲义"}:
        institution = safe_path_component(result.institution, "未知机构")
        module = (
            "综合"
            if result.primary_module == "数学基础"
            else result.primary_module
        )
        destination = markdown_root / result.document_type / institution / module
    elif result.document_type == "其他资料":
        institution = safe_path_component(result.institution, "未知机构")
        destination = markdown_root / "其他资料" / institution
    else:
        destination = markdown_root / "待分类"
    if create:
        destination.mkdir(parents=True, exist_ok=True)
    return destination


def classification_frontmatter(
    group_id: str,
    display: str,
    result: DocumentClassification,
    index_layer: str,
) -> str:
    metadata = {
        "group_id": group_id,
        "source_group": display,
        "document_type": result.document_type,
        "institution": (
            "不适用" if result.document_type == "教材" else result.institution
        ),
        "primary_module": result.primary_module,
        "module_tags": list(result.module_tags),
        "classification_method": result.method,
        "classification_confidence": round(result.confidence, 4),
        "classification_version": result.version,
        "content_structure_version": CONTENT_STRUCTURE_VERSION,
        "child_chunk_version": CHILD_CHUNK_VERSION,
        "index_layer": index_layer,
    }
    return "---\n" + yaml.safe_dump(
        metadata, allow_unicode=True, sort_keys=False
    ).strip() + "\n---\n\n"


def markdown_frontmatter(path: Path) -> dict:
    raw = path.read_text("utf-8")
    if not raw.startswith("---"):
        return {}
    pieces = raw.split("---", 2)
    if len(pieces) != 3:
        return {}
    try:
        value = yaml.safe_load(pieces[1]) or {}
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def classification_from_markdown(
    path: Path,
) -> DocumentClassification | None:
    metadata = markdown_frontmatter(path)
    if not metadata.get("document_type"):
        return None
    try:
        return classification_from_dict(metadata)
    except (KeyError, TypeError, ValueError):
        return None


def classification_evidence(
    group_name: str,
    parsed: list[tuple[Path, Path]],
    max_chars: int,
) -> tuple[str, str]:
    names = [group_name]
    bodies = []
    for source, markdown in parsed:
        names.extend([str(source), source.name])
        body = markdown_body(markdown)
        if body:
            share = max(1000, max_chars // max(1, len(parsed)))
            if len(body) <= share:
                bodies.append(body)
            else:
                head = int(share * 0.7)
                tail = share - head
                bodies.append(body[:head] + "\n" + body[-tail:])
    return "\n".join(dict.fromkeys(names)), "\n\n".join(bodies)[:max_chars]


def keyword_counts(text: str, terms: list[str]) -> int:
    normalized = text.casefold()
    return sum(normalized.count(str(term).casefold()) for term in terms)


def alias_present(text: str, alias: str) -> bool:
    normalized_text = text.casefold()
    normalized_alias = alias.casefold()
    if re.fullmatch(r"[a-z0-9_-]+", normalized_alias):
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])",
                normalized_text,
            )
        )
    return normalized_alias in normalized_text


def rule_classification(
    group_name: str,
    parsed: list[tuple[Path, Path]],
    cfg: dict,
) -> tuple[DocumentClassification, bool, list[str], list[str]]:
    taxonomy = cfg["taxonomy"]
    names, body = classification_evidence(
        group_name, parsed, int(cfg.get("max_mimo_chars", 12000))
    )
    name_text = names.casefold()
    scope_names = "\n".join(
        [group_name, *(source.name for source, _ in parsed)]
    )
    scope = taxonomy.get("subject_scope") or {}
    scope_include_hits = keyword_counts(
        scope_names, scope.get("include_terms") or []
    )
    scope_exclude_hits = keyword_counts(
        scope_names, scope.get("strong_exclude_terms") or []
    )
    strong_non_subject = (
        scope_exclude_hits > 0 and scope_include_hits == 0
    )
    type_name_scores = {
        kind: keyword_counts(names, terms)
        for kind, terms in taxonomy["document_types"].items()
    }
    type_body_scores = {
        kind: keyword_counts(body, terms)
        for kind, terms in taxonomy["document_types"].items()
    }
    other_name_evidence = type_name_scores.get("其他资料", 0) > 0
    other_body_evidence = type_body_scores.get("其他资料", 0) > 0
    explicit_other = (
        strong_non_subject
        or (other_name_evidence and other_body_evidence)
    )
    evidence = []
    if strong_non_subject:
        document_type = "其他资料"
        type_confidence = 0.97
        evidence.append(
            "路径或文件名含明确非学科资料词，且未命中数学物理范围"
        )
    elif explicit_other:
        document_type = "其他资料"
        type_confidence = 0.96
        evidence.append("路径或文件名与正文均含明确非学科资料证据")
    elif other_name_evidence:
        # A filename alone is useful evidence, but source deletion is
        # irreversible.  Keep it below the deletion threshold and ask MiMo for
        # an independent confirmation; if MiMo is unavailable it stays pending.
        document_type = "其他资料"
        type_confidence = 0.89
        evidence.append("路径或文件名含非学科资料词，等待第二项证据")
    else:
        document_type = ""
        for kind in DOCUMENT_TYPE_PRIORITY:
            if type_name_scores.get(kind, 0):
                document_type = kind
                evidence.append(f"路径或文件名命中{kind}关键词")
                break
        if document_type:
            type_confidence = 0.92
        else:
            chapter_hits = len(
                re.findall(
                    r"(?im)^\s*(?:#{1,6}\s*)?"
                    r"(?:第[一二三四五六七八九十百0-9]+章|"
                    r"chapter\s+[0-9ivxlcdm]+)\b",
                    body,
                )
            )
            publication_hits = keyword_counts(
                body,
                ["isbn", "出版社", "出版发行", "版权", "前言", "序言"],
            )
            if (
                type_body_scores.get("教材", 0) > 0
                and chapter_hits >= 2
                and publication_hits >= 1
            ):
                document_type = "教材"
                type_confidence = 0.86
                evidence.append("正文同时具有章节体系和出版信息，按教材处理")
            else:
                ranked_types = sorted(
                    DOCUMENT_TYPE_PRIORITY,
                    key=lambda kind: type_body_scores.get(kind, 0),
                    reverse=True,
                )
                top_kind = ranked_types[0]
                top_score = type_body_scores.get(top_kind, 0)
                second_score = type_body_scores.get(ranked_types[1], 0)
                if top_score >= 2 and top_score >= max(
                    2, second_score * 1.5
                ):
                    document_type = top_kind
                    type_confidence = 0.78
                    evidence.append(f"正文结构主要符合{top_kind}")
                else:
                    document_type = "待分类"
                    type_confidence = 0.45
                    evidence.append("文档类型证据不足或冲突")

    module_scores = {}
    explicit_modules = []
    for module, terms in taxonomy["modules"].items():
        name_hits = keyword_counts(names, terms)
        body_hits = keyword_counts(body, terms)
        module_scores[module] = name_hits * 5 + body_hits
        if name_hits:
            explicit_modules.append(module)
    if "热光近" in name_text:
        for module in ("热学", "光学", "近代物理"):
            if module not in explicit_modules:
                explicit_modules.append(module)
            module_scores[module] = module_scores.get(module, 0) + 10
    if re.search(r"(?:^|[^一-龥])电(?:学|磁|路|动力)", names):
        if "电磁学" not in explicit_modules:
            explicit_modules.append("电磁学")
        module_scores["电磁学"] = module_scores.get("电磁学", 0) + 8
    positive = {key: value for key, value in module_scores.items() if value > 0}
    total = sum(positive.values())
    if len(explicit_modules) == 1:
        primary_module = explicit_modules[0]
        module_confidence = 0.94
    elif len(explicit_modules) > 1:
        primary_module = "综合"
        module_confidence = 0.94
    elif positive:
        top_module, top_score = max(positive.items(), key=lambda item: item[1])
        if top_score >= 3 and top_score / total >= 0.60:
            primary_module = top_module
            module_confidence = 0.80
        else:
            primary_module = "综合"
            module_confidence = 0.76
    else:
        primary_module = "综合"
        module_confidence = 0.50
    tags = [
        module
        for module in MODULE_ORDER
        if module in explicit_modules
        or (total and module_scores.get(module, 0) / total >= 0.20)
    ]
    if not tags and primary_module != "综合":
        tags = [primary_module]
    if primary_module == "综合" and not tags:
        tags = ["综合"]
    evidence.append(
        "模块依据：" + "、".join(tags if tags != ["综合"] else ["综合"])
    )

    institution_candidates = []
    for canonical, aliases in taxonomy["institution_aliases"].items():
        if any(alias_present(names, str(alias)) for alias in aliases):
            institution_candidates.append(canonical)
    if (
        document_type != "教材"
        and any(
            alias_present(names, str(term))
            for term in taxonomy.get("official_competition_terms", [])
        )
        and "竞赛官方" not in institution_candidates
    ):
        institution_candidates.append("竞赛官方")
    if document_type != "教材" and not institution_candidates:
        school = re.search(
            r"([\u4e00-\u9fff]{2,16}(?:大学附属中学|附属中学|附中|中学|学校))",
            names,
        )
        if school:
            institution_candidates.append(school.group(1))
    if document_type == "教材":
        institution = "不适用"
    elif len(institution_candidates) == 1:
        institution = institution_candidates[0]
        evidence.append(f"机构证据：{institution}")
    else:
        institution = "未知机构"
        if len(institution_candidates) > 1:
            evidence.append("检测到多个机构候选")

    confidence = (
        type_confidence
        if document_type == "其他资料"
        else min(type_confidence, module_confidence)
    )
    needs_mimo = (
        document_type == "待分类"
        or (
            document_type == "其他资料"
            and confidence < float(cfg.get("other_min_confidence", 0.90))
        )
        or (
            document_type != "其他资料"
            and module_confidence < 0.75
        )
        or len(institution_candidates) > 1
    )
    result = DocumentClassification(
        document_type,
        institution,
        primary_module,
        tuple(tags),
        "rule",
        confidence,
        tuple(evidence[:4]),
        int(taxonomy.get("version") or cfg.get("version") or 1),
    )
    return result, needs_mimo, institution_candidates, [names, body]


def parse_json_object(text: str) -> dict:
    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I
    )
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型未返回JSON对象")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型返回值不是JSON对象")
    return value


def mimo_classification(
    rule_result: DocumentClassification,
    candidates: list[str],
    evidence_parts: list[str],
    cfg: dict,
    local_cfg: dict,
) -> DocumentClassification:
    classification_attempts = max(
        1,
        int(
            cfg.get(
                "classification_max_key_attempts",
                cfg.get("max_key_attempts", 3),
            )
        ),
    )
    keys, ordered = ordered_mimo_slots(cfg, classification_attempts)
    remote_reachable = False
    names, body = evidence_parts
    allowed_institutions = sorted(set(candidates) | {"未知机构", "不适用"})
    prompt = (
        "请只根据给定证据分类题库资料，禁止猜测机构。只返回JSON对象。"
        "\n允许document_type：教材、试卷、习题册、讲义、其他资料、待分类。"
        "\n允许primary_module：数学基础、力学、电磁学、光学、热学、近代物理、综合。"
        f"\n允许institution：{json.dumps(allowed_institutions, ensure_ascii=False)}"
        "\nmodule_tags只能使用上述模块名。evidence最多3条且必须摘自输入证据。"
        "\n输出字段：document_type,institution,primary_module,module_tags,"
        "confidence,evidence。"
        f"\n规则初判：{json.dumps(classification_to_dict(rule_result), ensure_ascii=False)}"
        f"\n路径和文件名：\n{names}\n代表性正文：\n{body}"
    )
    payload = {
        "model": cfg["model"],
        "messages": [
            {
                "role": "system",
                "content": "你是保守的题库资料分类器，只输出合法JSON。",
            },
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": 512,
        "temperature": 0,
        "stream": False,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }
    for slot in ordered:
        try:
            with mimo_request_context(cfg):
                with mimo_key_request_context(slot, cfg) as session:
                    response = session.post(
                        f"{cfg['base_url'].rstrip('/')}/chat/completions",
                        headers={
                            "api-key": keys[slot],
                            "Content-Type": "application/json",
                        },
                        json=payload,
                        timeout=int(
                            cfg.get(
                                "classification_timeout_seconds",
                                cfg.get("timeout_seconds", 180),
                            )
                        ),
                    )
            if (
                response.status_code in {401, 403, 429}
                or response.status_code >= 500
            ):
                if response.status_code == 429:
                    cooldown = int(
                        cfg.get("rate_limit_cooldown_seconds", 120)
                    )
                    note_mimo_key_rate_limit(slot, cooldown)
                    note_mimo_rate_limit(
                        cooldown
                    )
                continue
            response.raise_for_status()
            remote_reachable = True
            value = parse_json_object(
                str(response.json()["choices"][0]["message"]["content"])
            )
            note_mimo_key_success(slot)
            note_mimo_success()
            document_type = str(value.get("document_type") or "待分类")
            institution = str(value.get("institution") or "未知机构")
            primary = str(value.get("primary_module") or "综合")
            tags = tuple(
                dict.fromkeys(
                    str(item)
                    for item in value.get("module_tags") or []
                    if str(item) in CLASSIFICATION_MODULES
                )
            )
            confidence = max(0.0, min(1.0, float(value.get("confidence") or 0)))
            type_locked = any(
                marker in item
                for item in rule_result.evidence
                for marker in (
                    "路径或文件名命中",
                    "路径或文件名含明确非学科资料词",
                )
            )
            if type_locked:
                document_type = rule_result.document_type
            if len(candidates) == 1:
                institution = candidates[0]
            model_evidence = tuple(
                str(item).strip()
                for item in value.get("evidence") or []
                if str(item).strip()
            )[:3]
            evidence_text = f"{names}\n{body}".casefold()
            evidence_valid = any(
                len(item) >= 4 and item.casefold() in evidence_text
                for item in model_evidence
            )
            valid = (
                document_type in CLASSIFICATION_TYPES
                and institution in allowed_institutions
                and primary in CLASSIFICATION_MODULES
                and confidence >= float(cfg.get("mimo_min_confidence", 0.75))
                and evidence_valid
            )
            explicit_other = rule_result.document_type == "其他资料"
            if document_type == "其他资料" and (
                confidence < float(cfg.get("other_min_confidence", 0.90))
                or not explicit_other
            ):
                valid = False
            if not valid:
                continue
            if document_type == "教材":
                institution = "不适用"
            if document_type != "教材" and primary == "数学基础":
                primary = "综合"
            if not tags:
                tags = (primary,)
            return DocumentClassification(
                document_type,
                institution,
                primary,
                tags,
                "rule+mimo",
                confidence,
                model_evidence,
                rule_result.version,
            )
        except (requests.RequestException, KeyError, TypeError, ValueError):
            continue
    fallback_setting = cfg.get(
        "classification_fallback_to_ollama", "auto"
    )
    if str(fallback_setting).casefold() == "auto":
        use_local_fallback = not ordered or not remote_reachable
    else:
        use_local_fallback = bool(fallback_setting)
    if use_local_fallback:
        try:
            total_gb, available_gb = windows_memory_gb()
            if str(
                local_cfg.get("classification_context_chars", "auto")
            ).casefold() == "auto":
                local_prompt_chars = (
                    12000 if total_gb >= 24 else 7000 if total_gb >= 15 else 4000
                )
            else:
                local_prompt_chars = int(
                    local_cfg["classification_context_chars"]
                )
            keep_alive_setting = local_cfg.get(
                "classification_keep_alive", "auto"
            )
            if str(keep_alive_setting).casefold() == "auto":
                reserve_gb = max(1.5, total_gb * 0.10)
                local_keep_alive = (
                    "0s" if available_gb < reserve_gb else "30s"
                )
            else:
                local_keep_alive = str(keep_alive_setting)
            with LOCAL_MODEL_LOCK:
                response = requests.post(
                    f"{local_cfg['base_url'].rstrip('/')}/api/generate",
                    json={
                        "model": local_cfg.get(
                            "classification_model",
                            local_cfg["vision_model"],
                        ),
                        "prompt": prompt[:local_prompt_chars],
                        "stream": False,
                        "think": False,
                        "keep_alive": local_keep_alive,
                        "format": "json",
                        "options": {
                            "temperature": 0,
                            "num_ctx": 4096,
                            "num_predict": 384,
                        },
                    },
                    timeout=600,
                )
            response.raise_for_status()
            value = parse_json_object(str(response.json()["response"]))
            document_type = str(value.get("document_type") or "待分类")
            institution = str(value.get("institution") or "未知机构")
            primary = str(value.get("primary_module") or "综合")
            tags = tuple(
                dict.fromkeys(
                    str(item)
                    for item in value.get("module_tags") or []
                    if str(item) in CLASSIFICATION_MODULES
                )
            )
            confidence = max(
                0.0, min(1.0, float(value.get("confidence") or 0))
            )
            model_evidence = tuple(
                str(item).strip()
                for item in value.get("evidence") or []
                if str(item).strip()
            )[:3]
            type_locked = any(
                marker in item
                for item in rule_result.evidence
                for marker in (
                    "路径或文件名命中",
                    "路径或文件名含明确非学科资料词",
                )
            )
            if type_locked:
                document_type = rule_result.document_type
            if len(candidates) == 1:
                institution = candidates[0]
            evidence_text = f"{names}\n{body}".casefold()
            evidence_valid = any(
                len(item) >= 4 and item.casefold() in evidence_text
                for item in model_evidence
            )
            if (
                document_type in CLASSIFICATION_TYPES
                and institution in allowed_institutions
                and primary in CLASSIFICATION_MODULES
                and confidence
                >= float(cfg.get("mimo_min_confidence", 0.75))
                and evidence_valid
                and not (
                    document_type == "其他资料"
                    and (
                        confidence
                        < float(cfg.get("other_min_confidence", 0.90))
                        or rule_result.document_type != "其他资料"
                    )
                )
            ):
                if document_type == "教材":
                    institution = "不适用"
                if not tags:
                    tags = (primary,)
                return DocumentClassification(
                    document_type,
                    institution,
                    primary,
                    tags,
                    "rule+local",
                    confidence,
                    model_evidence,
                    rule_result.version,
                )
        except (requests.RequestException, KeyError, TypeError, ValueError):
            pass
    return rule_result


def classify_group(
    group_name: str,
    parsed: list[tuple[Path, Path]],
    cfg: dict,
    cached_json: str | None = None,
) -> DocumentClassification:
    class_cfg = cfg["document_classification"]
    taxonomy_version = int(
        class_cfg["taxonomy"].get("version") or class_cfg.get("version") or 1
    )
    if cached_json:
        try:
            cached = classification_from_dict(json.loads(cached_json))
            if cached.version == taxonomy_version:
                return cached
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    rule_result, needs_mimo, candidates, evidence_parts = rule_classification(
        group_name, parsed, class_cfg
    )
    result = rule_result
    if needs_mimo and bool(cfg["ollama"]["mimo"].get("enabled", False)):
        result = mimo_classification(
            rule_result,
            candidates,
            evidence_parts,
            cfg["ollama"]["mimo"],
            cfg["ollama"],
        )
    if result.document_type == "待分类":
        return result
    if result.document_type == "其他资料" and result.confidence < float(
        class_cfg.get("other_min_confidence", 0.90)
    ):
        return DocumentClassification(
            "待分类",
            result.institution,
            result.primary_module,
            result.module_tags,
            result.method,
            result.confidence,
            result.evidence + ("其他资料置信度不足，安全保留",),
            result.version,
        )
    if (
        result.document_type != "教材"
        and result.primary_module == "数学基础"
    ):
        tags = tuple(dict.fromkeys((*result.module_tags, "数学基础")))
        result = DocumentClassification(
            result.document_type,
            result.institution,
            "综合",
            tags,
            result.method,
            result.confidence,
            result.evidence,
            result.version,
        )
    return result


def stable_sha256(source: Path) -> str:
    before = source.stat()
    digest = sha256(source)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError("文件在计算摘要时仍被写入，请稍后自动重试")
    return digest


def source_path_token(source: Path) -> str:
    """Short stable path identity used to isolate equal-byte source instances."""
    normalized = str(source.resolve()).casefold().encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(normalized).hexdigest()[:10]


def recorded_source_digest(
    db: sqlite3.Connection,
    source: Path,
    group_id: str = "",
) -> str:
    """Return the immutable recorded digest even when a historical source is gone."""
    row = None
    if group_id:
        row = db.execute(
            """SELECT sha256 FROM group_files
            WHERE group_id=? AND source_path=?""",
            (group_id, str(source)),
        ).fetchone()
        if row is not None:
            return str(row["sha256"])
    if source.exists():
        return stable_sha256(source)
    if row is None:
        row = db.execute(
            """SELECT sha256 FROM files
            WHERE source_path=? ORDER BY updated_at DESC LIMIT 1""",
            (str(source),),
        ).fetchone()
    return str(row["sha256"]) if row else ""


def delete_source_with_audit(
    db: sqlite3.Connection,
    source: Path,
    expected_digest: str,
    canonical: Path,
    reason: str,
    group_id: str = "",
) -> None:
    """Permanently delete only after content and retained Markdown are verified."""
    if not source.exists():
        return
    # Keep the irreversible safety interlock at the deletion primitive as well
    # as at configuration/CLI entry points.  This prevents a newly added call
    # path from silently bypassing the user's explicit confirmation.
    require_permanent_delete_confirmation(reason)
    if not canonical.is_file() or not markdown_body(canonical).strip():
        raise RuntimeError("保留Markdown缺失或正文为空，拒绝删除源文件")
    markdown_digest = stable_sha256(canonical)
    source_digest = stable_sha256(source)
    if not expected_digest or source_digest != expected_digest:
        raise RuntimeError("源文件摘要与登记不一致，拒绝删除")
    cursor = db.execute(
        """INSERT INTO deletion_audit(
            source_path,sha256,group_id,reason,markdown_path,markdown_sha256,
            requested_at,success,error
        ) VALUES(?,?,?,?,?,?,?,0,'')""",
        (
            str(source),
            expected_digest,
            group_id,
            reason,
            str(canonical),
            markdown_digest,
            int(time.time()),
        ),
    )
    audit_id = int(cursor.lastrowid)
    db.commit()
    try:
        source.unlink()
        if source.exists():
            raise OSError("删除调用结束后源文件仍存在")
    except OSError as exc:
        db.execute(
            """UPDATE deletion_audit SET completed_at=?,error=?
            WHERE id=?""",
            (int(time.time()), str(exc), audit_id),
        )
        db.commit()
        raise
    db.execute(
        """UPDATE deletion_audit
        SET completed_at=?,success=1,error='' WHERE id=?""",
        (int(time.time()), audit_id),
    )
    db.commit()


def content_group_id(
    logical_group_id: str,
    sources: list[Path],
    digests: dict[Path, str] | None = None,
) -> str:
    digests = digests or {source: stable_sha256(source) for source in sources}
    manifest = []
    for source in sorted(sources, key=lambda path: str(path).casefold()):
        manifest.append(f"{source.name.casefold()}\0{digests[source]}")
    payload = f"{logical_group_id}\0" + "\0".join(manifest)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


PERMANENT_GROUP_EXCLUSION_STATES = {
    "user_delete_pending",
    "user_deleted",
    "excluded_completed",
}


def permanent_group_exclusion_state(
    db: sqlite3.Connection,
    group_id: str,
) -> str:
    row = db.execute(
        "SELECT state FROM groups WHERE group_id=?",
        (group_id,),
    ).fetchone()
    state = str(row["state"] or "") if row else ""
    return state if state in PERMANENT_GROUP_EXCLUSION_STATES else ""


def register_group_files(
    db: sqlite3.Connection,
    group_id: str,
    sources: list[Path],
    digests: dict[Path, str] | None = None,
) -> bool:
    digests = digests or {source: stable_sha256(source) for source in sources}
    # Serialize the duplicate check with registration. Two group workers may
    # discover equal bytes at the same time; a check outside this transaction
    # would let both attach mutable state to the legacy SHA primary key.
    db.execute("BEGIN IMMEDIATE")
    try:
        # File-level state cannot protect a group that the user deleted. Refuse
        # to recreate its links even if a shared historical source remains.
        if permanent_group_exclusion_state(db, group_id):
            db.rollback()
            return False
        reject_active_duplicate_source_instances(db, sources, digests)
        db.execute(
            """INSERT OR IGNORE INTO groups(group_id,group_name,state,updated_at)
            VALUES(?,?,?,?)""",
            (group_id, group_id, "queued", int(time.time())),
        )
        for source in sources:
            db.execute(
                """INSERT OR IGNORE INTO files(
                    sha256,source_path,state,error,updated_at,metrics_json
                ) VALUES(?,?,?,?,?,?)""",
                (
                    digests[source],
                    str(source),
                    "queued",
                    "",
                    int(time.time()),
                    "{}",
                ),
            )
            db.execute(
                """INSERT INTO group_files(group_id,sha256,source_path)
                VALUES(?,?,?) ON CONFLICT(group_id,source_path)
                DO UPDATE SET sha256=excluded.sha256""",
                (group_id, digests[source], str(source)),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True


def reject_active_duplicate_source_instances(
    db: sqlite3.Connection,
    sources: list[Path],
    digests: dict[Path, str],
) -> None:
    """Fail safely until the legacy SHA-primary-key schema is migrated."""
    current_by_digest: dict[str, list[Path]] = {}
    for source in sources:
        current_by_digest.setdefault(digests[source], []).append(source)
    for digest, paths in current_by_digest.items():
        if len(paths) > 1:
            raise RuntimeError(
                "同一资料组包含字节完全相同的重复源文件；请保留一份后重试: "
                + ", ".join(path.name for path in paths)
            )
        row = db.execute(
            "SELECT source_path FROM files WHERE sha256=?",
            (digest,),
        ).fetchone()
        if not row:
            continue
        registered = Path(row["source_path"])
        if (
            registered.exists()
            and registered.resolve() != paths[0].resolve()
        ):
            raise RuntimeError(
                "发现两个仍存在但字节完全相同的源文件；当前数据库版本不能安全并发追踪，"
                f"请保留一份后重试: {registered} | {paths[0]}"
            )


def pending_group_for_sources(
    db: sqlite3.Connection,
    sources: list[Path],
    digests: dict[Path, str] | None = None,
) -> sqlite3.Row | None:
    digests = digests or {source: stable_sha256(source) for source in sources}
    expected = Counter(digests.values())
    matches = []
    for row in db.execute(
        "SELECT * FROM groups WHERE state='cleanup_pending'"
    ).fetchall():
        recorded = Counter(
            item["sha256"]
            for item in db.execute(
                "SELECT sha256,source_path FROM group_files WHERE group_id=?",
                (row["group_id"],),
            ).fetchall()
            if Path(item["source_path"]).exists()
        )
        if recorded == expected:
            matches.append(row)
    return matches[0] if len(matches) == 1 else None


def markdown_body(path: Path) -> str:
    raw = path.read_text("utf-8")
    if raw.startswith("---"):
        pieces = raw.split("---", 2)
        if len(pieces) == 3:
            raw = pieces[2]
    return raw.strip()


def source_role(path: Path, text: str) -> str:
    name = path.stem.casefold()
    if COMBINED_FILE_RE.search(name):
        return "mixed"
    answer_marks = len(ANSWER_HEADING_RE.findall(text))
    question_marks = len(NUMBERED_UNIT_RE.findall(text))
    if ANSWER_FILE_RE.search(name):
        first_answer = ANSWER_HEADING_RE.search(text)
        if (
            answer_marks
            and question_marks
            and first_answer
            and len(text[:first_answer.start()].strip()) >= 80
        ):
            return "mixed"
        return "answer"
    if answer_marks and answer_marks >= max(2, question_marks // 2):
        return "mixed"
    return "question"


def split_numbered_units(text: str) -> list[tuple[str, int, str]]:
    matches = list(NUMBERED_UNIT_RE.finditer(text))
    if not matches:
        # Parenthesised numbering is commonly used for either an entire
        # worksheet or subquestions. Only promote it to top-level questions
        # when no standard top-level numbering exists.
        matches = list(PAREN_UNIT_RE.finditer(text))
    if not matches:
        return []
    occurrences: dict[str, int] = {}
    units = []
    for index, match in enumerate(matches):
        number = match.group("number")
        occurrences[number] = occurrences.get(number, 0) + 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        units.append((number, occurrences[number], text[match.start():end].strip()))
    return units


def remove_number_prefix(text: str) -> str:
    for pattern in (NUMBERED_UNIT_RE, PAREN_UNIT_RE):
        cleaned, count = pattern.subn(
            lambda match: match.group("tail").strip(), text, count=1
        )
        if count:
            return cleaned.strip()
    return text.strip()


def split_sections(text: str) -> tuple[str, str, str]:
    answer_match = ANSWER_HEADING_RE.search(text)
    explanation_match = EXPLANATION_HEADING_RE.search(text)
    boundaries = [
        (match.start(), kind, match.end())
        for match, kind in (
            (answer_match, "answer"),
            (explanation_match, "explanation"),
        )
        if match
    ]
    boundaries.sort()
    if not boundaries:
        return text.strip(), "", ""
    stem = text[:boundaries[0][0]].strip()
    answer = ""
    explanation = ""
    for index, (_, kind, content_start) in enumerate(boundaries):
        content_end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        content = text[content_start:content_end].strip()
        if kind == "answer":
            answer = content
        else:
            explanation = content
    return stem, answer, explanation


def build_question_units(parsed: list[tuple[Path, Path]]) -> tuple[list[QuestionUnit], int]:
    question_records: list[tuple[Path, str, list[tuple[str, int, str]]]] = []
    answer_records: list[tuple[Path, str, list[tuple[str, int, str]]]] = []
    for source, markdown in parsed:
        body = markdown_body(markdown)
        role = source_role(source, body)
        numbered = split_numbered_units(body)
        if role == "answer":
            answer_records.append((source, body, numbered))
        else:
            question_records.append((source, body, numbered))

    answers_by_exact: dict[tuple[str, str, int], tuple[str, str]] = {}
    answers_by_number: dict[tuple[str, int], list[tuple[str, str]]] = {}
    whole_answers: list[tuple[Path, tuple[str, str]]] = []
    for source, body, numbered in answer_records:
        base = normalized_source_stem(source)
        if not numbered and body:
            leading, answer, explanation = split_sections(body)
            if not answer and leading:
                answer = leading
            whole_answers.append(
                (source, (answer.strip(), explanation.strip()))
            )
        for number, occurrence, content in numbered:
            payload = remove_number_prefix(content)
            leading, answer, explanation = split_sections(payload)
            if not answer and leading:
                answer = leading
            if not answer and not explanation:
                answer = payload
            value = (answer.strip(), explanation.strip())
            answers_by_exact[(base, number, occurrence)] = value
            answers_by_number.setdefault((number, occurrence), []).append(value)

    questions: list[QuestionUnit] = []
    matched_answers = 0
    allow_number_fallback = len(question_records) == 1 and len(answer_records) == 1
    used_whole_answers: set[Path] = set()

    def whole_answer_for(source: Path) -> tuple[str, str] | None:
        available = [
            item for item in whole_answers if item[0] not in used_whole_answers
        ]
        if not available:
            return None
        if len(question_records) == 1 and len(available) == 1:
            used_whole_answers.add(available[0][0])
            return available[0][1]
        query = normalized_source_stem(source)
        ranked = sorted(
            [
                (
                difflib.SequenceMatcher(
                    None, query, normalized_source_stem(answer_source)
                ).ratio(),
                answer_source,
                value,
                )
                for answer_source, value in available
            ],
            key=lambda item: item[0],
        )
        score, answer_source, value = ranked[-1]
        runner_up = ranked[-2][0] if len(ranked) > 1 else 0.0
        if score < 0.62 or score - runner_up < 0.08:
            return None
        used_whole_answers.add(answer_source)
        return value

    for source, body, numbered in question_records:
        base = normalized_source_stem(source)
        if not numbered and body:
            stem, answer, explanation = split_sections(body)
            external = whole_answer_for(source)
            if external:
                matched_answers += 1
                answer = answer or external[0]
                explanation = explanation or external[1]
            questions.append(
                QuestionUnit(
                    number="",
                    occurrence=1,
                    source_stem=base,
                    stem=stem,
                    answer=answer,
                    explanation=explanation,
                )
            )
            continue
        for number, occurrence, content in numbered:
            payload = remove_number_prefix(content)
            stem, answer, explanation = split_sections(payload)
            external = answers_by_exact.get((base, number, occurrence))
            if (
                external is None
                and allow_number_fallback
                and len(answers_by_number.get((number, occurrence), [])) == 1
            ):
                external = answers_by_number[(number, occurrence)][0]
            if external:
                matched_answers += 1
                answer = answer or external[0]
                explanation = explanation or external[1]
            questions.append(
                QuestionUnit(
                    number=number,
                    occurrence=occurrence,
                    source_stem=base,
                    stem=stem,
                    answer=answer,
                    explanation=explanation,
                )
            )
    unmatched = max(
        0,
        len(answers_by_exact) + len(whole_answers) - matched_answers,
    )
    return questions, unmatched


def without_image_links(text: str) -> str:
    """Final text layers keep descriptions, never disposable image paths."""
    return re.sub(r"!\[[^\]]*]\([^)]+\)", "", text)


def split_document_sections(
    source: Path,
    body: str,
    target_chars: int = 6000,
) -> list[QuestionUnit]:
    """Preserve textbooks and lectures by headings instead of fake questions."""
    body = without_image_links(body).strip()
    if not body:
        return []
    headings = list(
        re.finditer(r"(?m)^(?P<marks>#{1,4})[ \t]+(?P<title>[^\n]+)\s*$", body)
    )
    raw_sections: list[tuple[str, str]] = []
    if headings:
        preamble = body[:headings[0].start()].strip()
        if preamble:
            raw_sections.append((source.stem, preamble))
        for index, heading in enumerate(headings):
            end = (
                headings[index + 1].start()
                if index + 1 < len(headings)
                else len(body)
            )
            title = heading.group("title").strip()
            content = body[heading.end():end].strip()
            raw_sections.append((title, content or title))
    else:
        pieces = split_child_content(body, max(800, target_chars))
        raw_sections.extend(
            (
                source.stem if len(pieces) == 1 else f"{source.stem}·第{index}部分",
                piece,
            )
            for index, piece in enumerate(pieces, 1)
        )

    merged: list[tuple[str, str]] = []
    for title, content in raw_sections:
        if merged and len(content) < 120:
            previous_title, previous_content = merged[-1]
            merged[-1] = (
                previous_title,
                f"{previous_content}\n\n## {title}\n\n{content}".strip(),
            )
        else:
            merged.append((title, content))
    base = normalized_source_stem(source)
    return [
        QuestionUnit(
            number="",
            occurrence=index,
            source_stem=base,
            stem=content,
            label=title,
            kind="section",
        )
        for index, (title, content) in enumerate(merged, 1)
        if content.strip()
    ]


def build_document_units(
    parsed: list[tuple[Path, Path]],
    classification: DocumentClassification,
) -> tuple[list[QuestionUnit], int, str]:
    """Choose question pairing or lossless section mode by document type."""
    if classification.document_type in {"教材", "讲义"}:
        sections: list[QuestionUnit] = []
        unmatched_answers = 0
        for source, markdown in parsed:
            body = markdown_body(markdown)
            if source_role(source, body) == "answer":
                unmatched_answers += 1
                continue
            sections.extend(split_document_sections(source, body))
        return sections, unmatched_answers, "section"
    questions, unmatched_answers = build_question_units(parsed)
    if questions:
        return questions, unmatched_answers, "question"
    sections = []
    for source, markdown in parsed:
        body = markdown_body(markdown)
        if source_role(source, body) != "answer":
            sections.extend(split_document_sections(source, body))
    return sections, unmatched_answers, "section" if sections else "question"


def write_raw_document(
    raw_path: Path,
    group_id: str,
    display: str,
    parsed: list[tuple[Path, Path]],
    classification: DocumentClassification,
) -> None:
    sections = []
    for source, markdown in parsed:
        body = without_image_links(markdown_body(markdown)).strip()
        if not body:
            continue
        sections.extend(
            [
                f"# 原始文件：{source.name}",
                classification_line(classification),
                body,
            ]
        )
    if not sections:
        raise RuntimeError("解析结果没有可保存的原文正文")
    metadata = classification_frontmatter(
        group_id, display, classification, "raw"
    )
    atomic_write(raw_path, metadata + "\n\n".join(sections).strip() + "\n")


def clean_heading_text(text: str) -> str:
    return re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", "", text).strip()


def split_child_content(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text.strip()] if text.strip() else []
    pieces = []
    remaining = text.strip()
    boundary = re.compile(r"(?<=[。！？；.!?;])|\n\n+|\n")
    units = [unit.strip() for unit in boundary.split(remaining) if unit.strip()]
    current = ""
    for unit in units:
        if len(unit) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(
                unit[start:start + max_chars].strip()
                for start in range(0, len(unit), max_chars)
                if unit[start:start + max_chars].strip()
            )
        elif not current:
            current = unit
        elif len(current) + 1 + len(unit) <= max_chars:
            current += "\n" + unit
        else:
            pieces.append(current)
            current = unit
    if current:
        pieces.append(current)
    return pieces


def adaptive_child_char_limit(
    configured: int,
    classification: DocumentClassification | None,
    unit_kind: str,
    field_name: str,
    content: str,
) -> int:
    """Use larger semantic child windows while keeping memory use bounded."""
    baseline = max(800, min(1600, int(configured or 1200)))
    document_type = classification.document_type if classification else ""
    if document_type in {"教材", "讲义"} or unit_kind == "section":
        target = max(baseline, 1400)
    elif document_type in {"试卷", "习题册"}:
        target = min(baseline, 1200)
    else:
        target = baseline
    if field_name in {"答案", "解析"} and len(content) > target:
        target += 200
    return max(800, min(1600, target))


def group_documents(
    group_id: str,
    group_name: str,
    parsed: list[tuple[Path, Path]],
    markdown_root: Path,
    work_root: Path,
    child_chars: int,
    classification: DocumentClassification,
) -> tuple[Path, Path, Path, int, int, str]:
    units, unmatched_answers, document_mode = build_document_units(
        parsed, classification
    )
    if not units:
        raise RuntimeError("整组资料解析完成，但没有识别到可入库的题目正文")
    display = re.sub(r'[<>:"/\\|?*]+', "_", group_name).strip(" .") or "题库资料"
    destination = classification_directory(markdown_root, classification)
    parent_path = destination / (
        f"{display}-{group_id}-结构v{CONTENT_STRUCTURE_VERSION}"
        f"-分类v{classification.version}.md"
    )
    child_dir = work_root / "indexes"
    child_dir.mkdir(parents=True, exist_ok=True)
    child_path = child_dir / (
        f"{display}-{group_id}-结构v{CONTENT_STRUCTURE_VERSION}"
        f"-子块v{CHILD_CHUNK_VERSION}.children.md"
    )
    raw_path = destination / (
        f"{display}-{group_id}-原文-结构v{CONTENT_STRUCTURE_VERSION}"
        f"-分类v{classification.version}.md"
    )
    parent_sections = []
    child_sections = []
    for index, unit in enumerate(units, 1):
        label = (
            unit.label
            or (f"第{unit.number}题" if unit.number else f"资料{index}")
        )
        if unit.occurrence > 1 and unit.number:
            label += f"（第{unit.occurrence}组）"
        stem = without_image_links(clean_heading_text(unit.stem))
        answer = clean_heading_text(unit.answer)
        explanation = clean_heading_text(unit.explanation)
        content_heading = "正文" if unit.kind == "section" else "题目"
        parent_sections.extend(
            [
                 f"# {label}",
                 f"资料：{display}",
                 classification_line(classification),
                 f"**{content_heading}**",
                stem,
                "**答案**",
                answer,
                "**解析**",
                explanation,
            ]
        )
        for field_name, content in (
            (content_heading, stem),
            ("答案", answer),
            ("解析", explanation),
        ):
            if not content:
                continue
            stem_context = ""
            if field_name != "题目" and stem:
                stem_preview = re.sub(r"\s+", " ", stem)[:120]
                stem_context = f"题干：{stem_preview}\n\n"
            child_limit = adaptive_child_char_limit(
                child_chars,
                classification,
                unit.kind,
                field_name,
                content,
            )
            visible = classification_line(classification)
            payload_limit = max(
                80,
                child_limit
                - len(stem_context)
                - len(visible)
                - len(label)
                - len(field_name)
                - 4,
            )
            pieces = split_child_content(content, payload_limit)
            for piece_index, piece in enumerate(pieces, 1):
                suffix = f"·{piece_index}" if len(pieces) > 1 else ""
                child_sections.extend(
                    [
                         f"# {label}｜{field_name}{suffix}",
                         (
                             f"{visible}\n"
                             f"{label} {field_name}\n\n{stem_context}{piece}"
                         ),
                     ]
                 )
    metadata = classification_frontmatter(
        group_id, display, classification, "parent"
    )
    child_metadata = classification_frontmatter(
        group_id, display, classification, "child"
    )
    atomic_write(parent_path, metadata + "\n\n".join(parent_sections).strip() + "\n")
    atomic_write(
        child_path,
        child_metadata + "\n\n".join(child_sections).strip() + "\n",
    )
    write_raw_document(
        raw_path,
        group_id,
        display,
        parsed,
        classification,
    )
    return (
        parent_path,
        child_path,
        raw_path,
        len(units),
        unmatched_answers,
        document_mode,
    )


def child_index_from_parent(
    parent_path: Path, work_root: Path, child_chars: int
) -> Path:
    """从已完成的父题Markdown重建临时子块，不重新调用MinerU。"""
    body = markdown_body(parent_path)
    classification = classification_from_markdown(parent_path)
    visible_classification = (
        classification_line(classification) if classification else ""
    )
    headings = list(re.finditer(r"(?m)^# (?P<label>[^\n]+)\s*$", body))
    child_sections: list[str] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        section = body[heading.end():end].strip()
        fields = re.split(
            r"(?m)^\*\*(题目|正文|答案|解析)\*\*\s*$", section
        )
        payloads = {
            fields[pos].strip(): fields[pos + 1].strip()
            for pos in range(1, len(fields) - 1, 2)
        }
        label = heading.group("label").strip()
        stem = payloads.get("题目", "") or payloads.get("正文", "")
        for field_name in ("题目", "正文", "答案", "解析"):
            content = payloads.get(field_name, "")
            if not content:
                continue
            stem_context = ""
            if field_name != "题目" and stem:
                stem_preview = re.sub(r"\s+", " ", stem)[:120]
                stem_context = f"题干：{stem_preview}\n\n"
            child_limit = adaptive_child_char_limit(
                child_chars,
                classification,
                "section" if field_name == "正文" else "question",
                field_name,
                content,
            )
            payload_limit = max(
                80,
                child_limit
                - len(stem_context)
                - len(visible_classification)
                - len(label)
                - len(field_name)
                - 4,
            )
            pieces = split_child_content(content, payload_limit)
            for piece_index, piece in enumerate(pieces, 1):
                suffix = f"·{piece_index}" if len(pieces) > 1 else ""
                child_sections.extend(
                    [
                        f"# {label}｜{field_name}{suffix}",
                        (
                            (visible_classification + "\n")
                            if visible_classification
                            else ""
                        )
                        + f"{label} {field_name}\n\n{stem_context}{piece}",
                    ]
                )
    if not child_sections:
        raise RuntimeError(f"父题Markdown无法重建子块: {parent_path}")
    child_dir = work_root / "indexes"
    child_dir.mkdir(parents=True, exist_ok=True)
    child_path = child_dir / (
        f"{parent_path.stem}-子块v{CHILD_CHUNK_VERSION}.children.md"
    )
    if classification:
        parent_metadata = markdown_frontmatter(parent_path)
        metadata = classification_frontmatter(
            str(parent_metadata.get("group_id") or ""),
            str(parent_metadata.get("source_group") or parent_path.stem),
            classification,
            "child",
        )
    else:
        metadata = ""
    atomic_write(
        child_path, metadata + "\n\n".join(child_sections).strip() + "\n"
    )
    return child_path


def raw_index_from_parent(parent_path: Path) -> Path:
    """Best-effort historical raw layer when the MinerU intermediate is gone."""
    classification = classification_from_markdown(parent_path)
    parent_metadata = markdown_frontmatter(parent_path)
    group_id = str(parent_metadata.get("group_id") or "")
    source_group = str(
        parent_metadata.get("source_group") or parent_path.stem
    )
    raw_path = parent_path.with_name(f"{parent_path.stem}-原文.md")
    metadata = (
        classification_frontmatter(
            group_id,
            source_group,
            classification,
            "raw",
        )
        if classification
        else ""
    )
    body = without_image_links(markdown_body(parent_path)).strip()
    if not body:
        raise RuntimeError(f"父块Markdown没有可恢复的原文内容: {parent_path}")
    atomic_write(raw_path, metadata + body + "\n")
    return raw_path


def save_group_state(
    db: sqlite3.Connection,
    group_id: str,
    group_name: str,
    state: str,
    markdown_path: Path | None = None,
    parent_doc_id: str | None = None,
    child_doc_id: str | None = None,
    error: str = "",
    classification: DocumentClassification | dict | None = None,
    raw_path: Path | None = None,
    raw_doc_id: str | None = None,
    commit: bool = True,
) -> None:
    existing_exclusion = permanent_group_exclusion_state(db, group_id)
    if existing_exclusion and state not in PERMANENT_GROUP_EXCLUSION_STATES:
        # Concurrent/stale indexing work cannot revive a group after a user
        # deletion or a permanent non-RAG classification decision.
        return
    classification_json = None
    if isinstance(classification, DocumentClassification):
        classification_json = json.dumps(
            classification_to_dict(classification), ensure_ascii=False
        )
    elif isinstance(classification, dict):
        classification_json = json.dumps(classification, ensure_ascii=False)
    db.execute(
        """INSERT INTO groups(
            group_id,group_name,state,markdown_path,parent_doc_id,child_doc_id,error,
            raw_path,raw_doc_id,updated_at,classification_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(group_id) DO UPDATE SET group_name=excluded.group_name,
        state=excluded.state,
        markdown_path=COALESCE(excluded.markdown_path,groups.markdown_path),
        parent_doc_id=COALESCE(excluded.parent_doc_id,groups.parent_doc_id),
        child_doc_id=COALESCE(excluded.child_doc_id,groups.child_doc_id),
        raw_path=COALESCE(excluded.raw_path,groups.raw_path),
        raw_doc_id=COALESCE(excluded.raw_doc_id,groups.raw_doc_id),
        classification_json=COALESCE(
            excluded.classification_json,groups.classification_json
        ),
        error=excluded.error,updated_at=excluded.updated_at""",
        (
            group_id,
            group_name,
            state,
            str(markdown_path) if markdown_path else None,
            parent_doc_id,
            child_doc_id,
            error,
            str(raw_path) if raw_path else None,
            raw_doc_id,
            int(time.time()),
            classification_json,
        ),
    )
    if commit:
        db.commit()


def run_command(
    template: list[str],
    values: dict,
    cwd: Path,
    timeout_seconds: int = 600,
    api_key: str | None = None,
) -> dict:
    command = []
    for original in template:
        part = original
        for key, value in values.items():
            part = part.replace("{" + key + "}", str(value))
        command.append(part)
    env = os.environ.copy()
    ingest_key = (
        api_key
        or os.getenv("WEKNORA_INGEST_API_KEY")
        or os.getenv("WEKNORA_API_KEY")
    )
    if ingest_key and "--profile" not in command:
        env["WEKNORA_API_KEY"] = ingest_key
    try:
        # WeKnora itself schedules embedding requests.  Limiting every CLI
        # process with LOCAL_MODEL_LOCK made the parent/child and
        # vector/BM25 checks effectively serial.  Keep a bounded command pool
        # instead; direct visual-model calls continue to use LOCAL_MODEL_LOCK.
        _, command_active, command_waiters = (
            WEKNORA_COMMAND_GATE.snapshot()
        )
        command_pressure = command_active + command_waiters + 1
        WEKNORA_COMMAND_GATE.set_limit(
            adaptive_weknora_command_count(command_pressure),
            "按内存和命令压力调整",
            float(
                RUNTIME_RESOURCE_CONFIG.get(
                    "weknora_concurrency_increase_hold_seconds", 30
                )
            ),
        )
        with WEKNORA_COMMAND_GATE.slot():
            result = subprocess.run(
                command,
                cwd=cwd,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=timeout_seconds,
            )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"命令等待超时: {command}") from exc
    if result.returncode != 0:
        detail = "\n".join(
            text.strip() for text in (result.stdout, result.stderr) if text.strip()
        )
        raise RuntimeError(f"命令失败 {command}: {detail}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def run_hidden_process(command: list[str], timeout_seconds: int = 120) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        creationflags=flags,
    )
    if result.returncode != 0:
        detail = "\n".join(
            item.strip()
            for item in (result.stdout, result.stderr)
            if item.strip()
        )
        raise RuntimeError(f"后台命令失败 {command}: {detail}")
    return result.stdout.strip()


def stop_temporary_neo4j() -> None:
    global NEO4J_STARTED_FOR_DELETE
    if not NEO4J_STARTED_FOR_DELETE:
        return
    try:
        run_hidden_process(
            [
                "wsl.exe", "-d", NEO4J_RUNTIME_CONFIG["wsl_distro"], "--",
                "docker", "stop", NEO4J_RUNTIME_CONFIG["container"],
            ],
            timeout_seconds=90,
        )
    except Exception as exc:
        print(f"Neo4j临时容器未能自动停止，请稍后检查: {exc}")
        return
    NEO4J_STARTED_FOR_DELETE = False
    print("旧索引删除阶段结束，Neo4j已重新停止以释放内存")


def ensure_neo4j_for_delete() -> None:
    global NEO4J_STARTED_FOR_DELETE
    running = run_hidden_process(
        [
            "wsl.exe", "-d", NEO4J_RUNTIME_CONFIG["wsl_distro"], "--",
            "docker", "inspect", "-f", "{{.State.Running}}",
            NEO4J_RUNTIME_CONFIG["container"],
        ]
    ).casefold() == "true"
    if running:
        return
    run_hidden_process(
        [
            "wsl.exe", "-d", NEO4J_RUNTIME_CONFIG["wsl_distro"], "--",
            "docker", "start", NEO4J_RUNTIME_CONFIG["container"],
        ],
        timeout_seconds=120,
    )
    NEO4J_STARTED_FOR_DELETE = True
    atexit.register(stop_temporary_neo4j)
    deadline = time.time() + 90
    while time.time() < deadline:
        state = run_hidden_process(
            [
                "wsl.exe", "-d", NEO4J_RUNTIME_CONFIG["wsl_distro"], "--",
                "docker", "inspect",
                "-f",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}running{{end}}",
                NEO4J_RUNTIME_CONFIG["container"],
            ]
        ).casefold()
        if state in {"healthy", "running"}:
            time.sleep(15 if state == "running" else 2)
            print("删除旧索引前已临时启动Neo4j")
            return
        if state == "unhealthy":
            break
        time.sleep(2)
    raise RuntimeError("Neo4j未能在90秒内进入可用状态")


def weknora_values(
    md_path: Path,
    source: Path,
    cfg: dict,
    doc_id: str = "",
    knowledge_base: str | None = None,
) -> dict:
    raw = md_path.read_text("utf-8").replace("\x00", "")
    body = raw.split("---", 2)[-1] if raw.startswith("---") else raw
    probe = re.sub(r"\s+", " ", body).strip()
    return {
        "root": str(ROOT),
        "file": str(md_path),
        "kb": knowledge_base or cfg["knowledge_base"],
        "profile": cfg["profile"],
        "doc_id": doc_id,
        "query": probe[:160] or source.stem,
    }


def weknora_upload(
    md_path: Path,
    source: Path,
    cfg: dict,
    knowledge_base: str | None = None,
    layer: str = "",
    group_id: str = "",
    classification: DocumentClassification | None = None,
) -> str:
    values = weknora_values(md_path, source, cfg, knowledge_base=knowledge_base)
    command = list(cfg["upload_command"])
    if layer:
        command.extend(["--metadata", f"index_layer={layer}"])
    if group_id:
        command.extend(["--metadata", f"group_id={group_id}"])
    if classification:
        for key, value in (
            ("document_type", classification.document_type),
            (
                "institution",
                "不适用"
                if classification.document_type == "教材"
                else classification.institution,
            ),
            ("primary_module", classification.primary_module),
            ("module_tags", "、".join(classification.module_tags)),
            ("classification_version", str(classification.version)),
            ("content_structure_version", str(CONTENT_STRUCTURE_VERSION)),
            ("child_chunk_version", str(CHILD_CHUNK_VERSION)),
        ):
            command.extend(["--metadata", f"{key}={value}"])
    uploaded = run_command(command, values, md_path.parent)
    data = uploaded.get("data", uploaded)
    doc_id = data.get("id") or data.get("document_id") or data.get("knowledge_id")
    if not doc_id:
        raise RuntimeError(f"无法从WeKnora上传结果取得文档ID: {uploaded}")
    return doc_id


def weknora_find_existing(
    md_path: Path,
    source: Path,
    cfg: dict,
    knowledge_base: str | None = None,
    layer: str = "",
    group_id: str = "",
    classification: DocumentClassification | None = None,
    expected_file_name: str = "",
) -> str | None:
    remote_file_name = expected_file_name or md_path.name
    values = weknora_values(
        md_path, source, cfg, knowledge_base=knowledge_base
    )
    executable = cfg["upload_command"][0]
    response = run_command(
        [
            executable,
            "search",
            "docs",
            remote_file_name,
            "--kb",
            "{kb}",
            "--format",
            "json",
            "--profile",
            "{profile}",
        ],
        values,
        md_path.parent,
    )
    records = response.get("data") or []
    exact = [
        item
        for item in records
        if item.get("file_name") == remote_file_name
    ]
    if len(exact) > 1:
        raise RuntimeError(
            f"WeKnora中已有多个同名文档，拒绝继续上传: {remote_file_name}"
        )
    if not exact:
        return None
    doc_id = str(exact[0].get("id") or "")
    if not doc_id:
        raise RuntimeError(f"WeKnora同名文档缺少ID: {remote_file_name}")
    document = weknora_document(
        doc_id,
        md_path,
        source,
        cfg,
        knowledge_base or cfg["knowledge_base"],
    )
    metadata = document.get("metadata") or {}
    expected = {
        "group_id": group_id,
        "index_layer": layer,
        "content_structure_version": str(CONTENT_STRUCTURE_VERSION),
        "child_chunk_version": str(CHILD_CHUNK_VERSION),
    }
    if classification is not None:
        expected["classification_version"] = str(classification.version)
    mismatches = [
        f"{key}={metadata.get(key)!r}"
        for key, value in expected.items()
        if value and str(metadata.get(key) or "") != value
    ]
    remote_hash = str(document.get("file_hash") or "").casefold()
    local_hash = md5_digest(md_path)
    if not remote_hash:
        mismatches.append("file_hash缺失")
    elif remote_hash != local_hash:
        mismatches.append("file_hash不匹配")
    actual_kb = str(document.get("knowledge_base_id") or "")
    expected_kb = str(knowledge_base or cfg["knowledge_base"])
    if not actual_kb:
        mismatches.append("knowledge_base_id缺失")
    elif actual_kb != expected_kb:
        mismatches.append("knowledge_base_id不匹配")
    if mismatches:
        raise RuntimeError(
            "WeKnora同名文档的归属或内容不一致，拒绝复用："
            f"{remote_file_name}｜" + "、".join(mismatches)
        )
    return doc_id


def weknora_document(
    doc_id: str,
    md_path: Path,
    source: Path,
    cfg: dict,
    knowledge_base: str,
) -> dict:
    values = weknora_values(
        md_path,
        source,
        cfg,
        doc_id=doc_id,
        knowledge_base=knowledge_base,
    )
    viewed = run_command(
        [
            cfg["upload_command"][0],
            "doc",
            "view",
            "{doc_id}",
            "--format",
            "json",
            "--profile",
            "{profile}",
        ],
        values,
        md_path.parent,
    )
    return viewed.get("data", viewed)


def repair_storage_quota_failed_document(
    doc_id: str | None,
    md_path: Path,
    source: Path,
    cfg: dict,
    knowledge_base: str,
    layer: str,
    group_id: str,
    classification: DocumentClassification | None,
) -> str | None:
    """Replace an old quota-failed document after storage has been freed.

    WeKnora keeps a failed document ID in the knowledge base. Waiting on that
    same ID can never recover, so an idempotent resume must remove the failed
    object and upload the preserved local index layer again.
    """
    if not doc_id:
        return doc_id
    document = weknora_document(
        str(doc_id), md_path, source, cfg, knowledge_base
    )
    if (
        str(document.get("parse_status") or "").casefold() != "failed"
        or "存储空间不足" not in str(document.get("error_message") or "")
    ):
        return doc_id
    print(
        f"WeKnora旧配额失败文档自愈：{md_path.name}｜"
        f"删除失败ID后重传{layer}层"
    )
    weknora_delete(str(doc_id), source, cfg, knowledge_base)
    return weknora_upload(
        md_path,
        source,
        cfg,
        knowledge_base,
        layer,
        group_id,
        classification,
    )


def weknora_delete(
    doc_id: str,
    source: Path,
    cfg: dict,
    knowledge_base: str,
) -> None:
    if not doc_id:
        return
    ensure_neo4j_for_delete()
    # Deletion must remain possible after the user has intentionally removed
    # the local Markdown. Unlike upload/search, it does not need file content.
    values = {
        "root": str(ROOT),
        "file": str(source),
        "kb": knowledge_base,
        "profile": cfg["profile"],
        "doc_id": doc_id,
        "query": source.stem,
    }
    run_command(
        [
            cfg["upload_command"][0],
            "doc",
            "delete",
            "{doc_id}",
            "--yes",
            "--format",
            "json",
            "--profile",
            "{profile}",
        ],
        values,
        ROOT,
    )


def enqueue_index_cleanup(
    db: sqlite3.Connection,
    group_id: str,
    doc_id: str,
    knowledge_base_id: str,
    markdown_path: Path,
    commit: bool = True,
) -> None:
    """Persist an index cleanup and make a prior completed row retryable."""
    if not doc_id or not knowledge_base_id:
        return
    now = int(time.time())
    db.execute(
        """INSERT INTO index_cleanup_queue(
            doc_id,knowledge_base_id,group_id,markdown_path,state,
            attempts,last_error,created_at,updated_at
        ) VALUES(?,?,?,?, 'pending',0,'',?,?)
        ON CONFLICT(doc_id,knowledge_base_id) DO UPDATE SET
            group_id=excluded.group_id,
            markdown_path=excluded.markdown_path,
            state='pending',
            last_error='',
            updated_at=excluded.updated_at""",
        (
            doc_id,
            knowledge_base_id,
            group_id,
            str(markdown_path),
            now,
            now,
        ),
    )
    if commit:
        db.commit()


def drain_index_cleanup_queue(
    db: sqlite3.Connection,
    cfg: dict,
    group_id: str | None = None,
    limit: int = 0,
    allow_unresolved_journal_group: str | None = None,
) -> dict[str, int]:
    """Retry persisted old-index deletions idempotently across restarts."""
    parameters: list[object] = []
    where = "state='pending'"
    if group_id:
        where += " AND group_id=?"
        parameters.append(group_id)
    sql = (
        "SELECT * FROM index_cleanup_queue WHERE "
        + where
        + " ORDER BY created_at,doc_id"
    )
    if limit > 0:
        sql += " LIMIT ?"
        parameters.append(limit)
    rows = db.execute(sql, parameters).fetchall()
    result = {"completed": 0, "pending": 0, "adopted": 0}
    for row in rows:
        doc_id = str(row["doc_id"])
        knowledge_base_id = str(row["knowledge_base_id"])
        cleanup_group_id = str(row["group_id"] or "")
        unresolved_journal = (
            cfg["folders"]["work"]
            / "content-migration"
            / cleanup_group_id
            / "migration-journal.json"
        )
        if (
            cleanup_group_id != allow_unresolved_journal_group
            and unresolved_journal.is_file()
        ):
            db.execute(
                """UPDATE index_cleanup_queue
                SET last_error='迁移中断日志尚未解除，暂缓删除索引',
                    updated_at=?
                WHERE doc_id=? AND knowledge_base_id=?""",
                (int(time.time()), doc_id, knowledge_base_id),
            )
            db.commit()
            result["pending"] += 1
            continue
        markdown_path = Path(row["markdown_path"] or ROOT / "markdown")
        active = db.execute(
            """SELECT group_id FROM groups
            WHERE state NOT IN ('user_delete_pending','user_deleted')
            AND (
                parent_doc_id=? OR child_doc_id=? OR raw_doc_id=?
            )
            LIMIT 1""",
            (doc_id, doc_id, doc_id),
        ).fetchone()
        if active:
            db.execute(
                """UPDATE index_cleanup_queue
                SET state='adopted',
                    last_error='清理已取消：索引正被活动资料组引用',
                    updated_at=?
                WHERE doc_id=? AND knowledge_base_id=?""",
                (int(time.time()), doc_id, knowledge_base_id),
            )
            db.commit()
            result["adopted"] += 1
            continue
        try:
            weknora_delete(
                doc_id,
                markdown_path,
                cfg["weknora"],
                knowledge_base_id,
            )
        except Exception as exc:
            message = str(exc)
            lowered = message.casefold()
            if not (
                "not found" in lowered
                or "404" in lowered
                or "不存在" in message
            ):
                db.execute(
                    """UPDATE index_cleanup_queue
                    SET attempts=attempts+1,last_error=?,updated_at=?
                    WHERE doc_id=? AND knowledge_base_id=?""",
                    (
                        message[:2000],
                        int(time.time()),
                        doc_id,
                        knowledge_base_id,
                    ),
                )
                db.commit()
                result["pending"] += 1
                continue
        db.execute(
            """UPDATE index_cleanup_queue
            SET state='completed',attempts=attempts+1,last_error='',updated_at=?
            WHERE doc_id=? AND knowledge_base_id=?""",
            (int(time.time()), doc_id, knowledge_base_id),
        )
        db.commit()
        result["completed"] += 1
    return result


def group_local_markdown_paths(
    db: sqlite3.Connection,
    group: sqlite3.Row | dict,
) -> set[Path]:
    """Return every local Markdown path currently attached to one group."""
    paths: set[Path] = set()
    for field in ("markdown_path", "raw_path"):
        value = group[field]
        if value:
            paths.add(Path(str(value)))
    for row in db.execute(
        """SELECT DISTINCT f.markdown_path
        FROM group_files gf
        LEFT JOIN files f ON f.sha256=gf.sha256
        WHERE gf.group_id=? AND coalesce(f.markdown_path,'')!=''""",
        (str(group["group_id"]),),
    ).fetchall():
        paths.add(Path(str(row["markdown_path"])))
    return paths


def expand_group_ids_by_sha(
    db: sqlite3.Connection,
    group_ids: set[str],
) -> set[str]:
    """Return the transitive group closure for immutable source digests."""
    expanded = {group_id for group_id in group_ids if group_id}
    while expanded:
        selected = sorted(expanded)
        placeholders = ",".join("?" for _ in selected)
        digests = {
            str(row["sha256"])
            for row in db.execute(
                f"""SELECT DISTINCT sha256 FROM group_files
                WHERE group_id IN ({placeholders})""",
                selected,
            ).fetchall()
        }
        if not digests:
            break
        digest_values = sorted(digests)
        digest_placeholders = ",".join("?" for _ in digest_values)
        related = {
            str(row["group_id"])
            for row in db.execute(
                f"""SELECT DISTINCT group_id FROM group_files
                WHERE sha256 IN ({digest_placeholders})""",
                digest_values,
            ).fetchall()
        }
        if related <= expanded:
            break
        expanded |= related
    return expanded


def detect_manual_deletions(
    cfg: dict,
    db: sqlite3.Connection,
    output_path: Path,
    grace_seconds: int = 60,
) -> dict[str, int | str]:
    """Detect Explorer deletions from current database paths.

    A non-final group with a recorded final Markdown path is expected to keep
    that file.  Sources are expected to remain until a group is completed or
    explicitly enters a historical source-missing recovery state.  This live
    check avoids relying on an old spreadsheet snapshot.
    """
    now = int(time.time())
    markdown_root = cfg["folders"]["markdown"].resolve()
    source_roots = (
        cfg["folders"]["inbox"].resolve(),
        cfg["folders"]["failed"].resolve(),
    )

    def under(candidate: Path, roots: tuple[Path, ...]) -> bool:
        resolved = candidate.resolve()
        return any(resolved != root and root in resolved.parents for root in roots)

    markdown_missing: list[dict[str, str]] = []
    rows = db.execute(
        """SELECT group_id,group_name,state,markdown_path,raw_path,updated_at
        FROM groups
        WHERE state NOT IN (
            'user_delete_pending','user_deleted','excluded_completed'
        )
        AND markdown_path IS NOT NULL
        AND markdown_path != ''"""
    ).fetchall()
    for row in rows:
        if now - int(row["updated_at"] or 0) < max(0, grace_seconds):
            continue
        missing_layers = []
        for layer, value in (
            ("parent", row["markdown_path"]),
            ("raw", row["raw_path"]),
        ):
            if not value:
                continue
            candidate = Path(value)
            if under(candidate, (markdown_root,)) and not candidate.is_file():
                missing_layers.append((layer, candidate))
        if not missing_layers:
            continue
        markdown_missing.append(
            {
                "group_id": str(row["group_id"]),
                "group_name": str(row["group_name"] or ""),
                "markdown_path": str(missing_layers[0][1]),
                "missing_layers": ",".join(
                    layer for layer, _ in missing_layers
                ),
                "state": str(row["state"] or ""),
                "group_updated_at": str(row["updated_at"] or 0),
            }
        )

    source_missing_by_group: dict[str, dict[str, str]] = {}
    source_rows = db.execute(
        """SELECT
            g.group_id,g.group_name,g.state,g.updated_at,
            g.markdown_path,g.raw_path,
            g.parent_doc_id,g.child_doc_id,g.raw_doc_id,
            gf.source_path AS group_source,
            f.source_path AS canonical_source,
            f.state AS file_state
        FROM groups g
        JOIN group_files gf ON gf.group_id=g.group_id
        LEFT JOIN files f ON f.sha256=gf.sha256
        WHERE g.state IN (
            'classified','classification_pending','retry_wait','failed','completed'
        )"""
    ).fetchall()
    ignored_file_states = {
        "excluded_completed",
        "user_delete_pending",
        "user_deleted",
    }
    if cfg["cleanup"].get("permanently_delete_source_after_search", False):
        ignored_file_states.add("completed")
    for row in source_rows:
        if str(row["file_state"] or "") in ignored_file_states:
            continue
        if now - int(row["updated_at"] or 0) < max(0, grace_seconds):
            continue
        candidates: list[Path] = []
        for value in (row["canonical_source"], row["group_source"]):
            if not value:
                continue
            candidate = Path(value)
            if under(candidate, source_roots) and candidate not in candidates:
                candidates.append(candidate)
        if not candidates or any(candidate.is_file() for candidate in candidates):
            continue
        group_id = str(row["group_id"])
        source_missing_by_group.setdefault(
            group_id,
            {
                "group_id": group_id,
                "group_name": str(row["group_name"] or ""),
                "source_path": str(candidates[0]),
                "state": str(row["state"] or ""),
                "group_updated_at": str(row["updated_at"] or 0),
            },
        )

    # A legacy one-file group can remain retry_wait even though that exact file
    # was completed and its source was removed by another historical group.
    # Treat it as abandoned only when *every* member is completed, every source
    # path is gone, and the group has no Markdown or index of its own.  Checking
    # the whole group avoids deleting a retained answer/PPT merely because its
    # paired question was successfully indexed elsewhere.
    orphan_rows = db.execute(
        """SELECT
            g.group_id,g.group_name,g.state,g.updated_at,
            gf.source_path AS group_source,
            f.source_path AS canonical_source,
            f.state AS file_state
        FROM groups g
        JOIN group_files gf ON gf.group_id=g.group_id
        LEFT JOIN files f ON f.sha256=gf.sha256
        WHERE g.state IN (
            'classified','classification_pending','retry_wait','failed'
        )
        AND coalesce(g.markdown_path,'')=''
        AND coalesce(g.raw_path,'')=''
        AND coalesce(g.parent_doc_id,'')=''
        AND coalesce(g.child_doc_id,'')=''
        AND coalesce(g.raw_doc_id,'')=''
        ORDER BY g.group_id"""
    ).fetchall()
    orphan_groups: dict[str, list[sqlite3.Row]] = {}
    for row in orphan_rows:
        orphan_groups.setdefault(str(row["group_id"]), []).append(row)
    for group_id, members in orphan_groups.items():
        if group_id in source_missing_by_group:
            continue
        if now - int(members[0]["updated_at"] or 0) < max(0, grace_seconds):
            continue
        if not members or any(
            str(member["file_state"] or "") != "completed"
            for member in members
        ):
            continue
        candidates: list[Path] = []
        for member in members:
            for value in (
                member["canonical_source"],
                member["group_source"],
            ):
                if not value:
                    continue
                candidate = Path(value)
                if under(candidate, source_roots) and candidate not in candidates:
                    candidates.append(candidate)
        if not candidates or any(candidate.is_file() for candidate in candidates):
            continue
        source_missing_by_group[group_id] = {
            "group_id": group_id,
            "group_name": str(members[0]["group_name"] or ""),
            "source_path": str(candidates[0]),
            "state": str(members[0]["state"] or ""),
            "group_updated_at": str(members[0]["updated_at"] or 0),
        }

    affected_group_ids = expand_group_ids_by_sha(db, {
        *(item["group_id"] for item in markdown_missing),
        *source_missing_by_group,
    })
    markdown_snapshots: list[dict[str, str]] = []
    group_snapshots: list[dict[str, str]] = []
    snapshot_roots = (markdown_root, cfg["folders"]["work"].resolve())
    for group_id in sorted(affected_group_ids):
        group = db.execute(
            "SELECT * FROM groups WHERE group_id=?", (group_id,)
        ).fetchone()
        if group is None:
            continue
        group_snapshots.append(
            {
                "group_id": group_id,
                "updated_at": str(group["updated_at"] or 0),
            }
        )
        for candidate in sorted(
            group_local_markdown_paths(db, group),
            key=lambda path: str(path).casefold(),
        ):
            if not candidate.is_file() or not under(candidate, snapshot_roots):
                continue
            markdown_snapshots.append(
                {
                    "group_id": group_id,
                    "path": str(candidate.resolve()),
                    "sha256": stable_sha256(candidate),
                }
            )

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "detection": "live_state_db_and_filesystem",
        "grace_seconds": max(0, grace_seconds),
        "newly_missing_markdown": markdown_missing,
        "newly_missing_sources": list(source_missing_by_group.values()),
        "related_group_snapshots": group_snapshots,
        "existing_markdown_snapshots": markdown_snapshots,
    }
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        output_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return {
        "output_path": str(output_path),
        "missing_markdown": len(markdown_missing),
        "missing_sources": len(source_missing_by_group),
        "affected_groups": len(affected_group_ids),
    }


def sync_manual_deletions(
    cfg: dict,
    db: sqlite3.Connection,
    selection_path: Path,
    dry_run: bool = False,
) -> dict[str, int]:
    """Synchronize explicit Explorer deletions across local and WeKnora layers."""
    selection_path = selection_path.resolve()
    if not selection_path.is_file():
        raise RuntimeError(f"手动删除差异文件不存在: {selection_path}")
    if not dry_run:
        require_manual_deletion_sync_confirmation()
    payload = json.loads(selection_path.read_text("utf-8"))
    detection_kind = str(payload.get("detection") or "")
    supported_detection_kinds = {
        "live_state_db_and_filesystem",
        "pending_manual_deletion_resume",
        "historical_manual_deletion_sha_closure",
    }
    if detection_kind not in supported_detection_kinds:
        raise RuntimeError(
            "手动删除差异文件类型无法验证，拒绝执行"
        )

    def strictly_under(candidate: Path, roots: tuple[Path, ...]) -> bool:
        resolved = candidate.resolve()
        return any(resolved != root and root in resolved.parents for root in roots)

    if detection_kind == "live_state_db_and_filesystem":
        for collection, path_key in (
            (payload.get("newly_missing_markdown", []), "markdown_path"),
            (payload.get("newly_missing_sources", []), "source_path"),
        ):
            for item in collection:
                group_id = str(item.get("group_id") or "")
                snapshot = int(item.get("group_updated_at") or 0)
                raw_trigger_path = str(item.get(path_key) or "").strip()
                if not raw_trigger_path:
                    raise RuntimeError(
                        f"手动删除差异缺少触发路径: {group_id or '(empty)'}"
                    )
                trigger_path = Path(raw_trigger_path).resolve()
                current = db.execute(
                    """SELECT updated_at,markdown_path,raw_path
                    FROM groups WHERE group_id=?""",
                    (group_id,),
                ).fetchone()
                if not current or snapshot <= 0:
                    raise RuntimeError(
                        f"手动删除差异缺少可验证快照: {group_id or '(empty)'}"
                    )
                if int(current["updated_at"] or 0) != snapshot:
                    raise RuntimeError(
                        f"资料组在检测后已变化，拒绝使用过期删除差异: {group_id}"
                    )
                if path_key == "markdown_path":
                    allowed_trigger_paths = {
                        Path(value).resolve()
                        for value in (current["markdown_path"], current["raw_path"])
                        if value
                    }
                    allowed_roots_for_trigger = (
                        cfg["folders"]["markdown"].resolve(),
                    )
                else:
                    source_rows = db.execute(
                        """SELECT gf.source_path,
                        f.source_path AS canonical_source
                        FROM group_files gf
                        LEFT JOIN files f ON f.sha256=gf.sha256
                        WHERE gf.group_id=?""",
                        (group_id,),
                    ).fetchall()
                    allowed_trigger_paths = {
                        Path(value).resolve()
                        for row in source_rows
                        for value in (row["source_path"], row["canonical_source"])
                        if value
                    }
                    allowed_roots_for_trigger = (
                        cfg["folders"]["inbox"].resolve(),
                        cfg["folders"]["failed"].resolve(),
                    )
                if (
                    trigger_path not in allowed_trigger_paths
                    or not strictly_under(trigger_path, allowed_roots_for_trigger)
                ):
                    raise RuntimeError(
                        "删除触发路径不属于所选资料组，拒绝级联删除: "
                        f"{trigger_path}"
                    )
                if trigger_path.exists():
                    raise RuntimeError(
                        f"删除触发路径已恢复或被替换，拒绝级联删除: {trigger_path}"
                    )
    markdown_selected = {
        str(item.get("group_id") or "")
        for item in payload.get("newly_missing_markdown", [])
        if str(item.get("group_id") or "")
    }
    source_selected = {
        str(item.get("group_id") or "")
        for item in payload.get("newly_missing_sources", [])
        if str(item.get("group_id") or "")
    }
    # One immutable source may have been attached to an old group and a later
    # recovered group.  The user's deletion rule applies to the whole material,
    # so expand the selection across every group sharing that exact SHA-256.
    direct_selected = markdown_selected | source_selected
    selected_set = expand_group_ids_by_sha(db, direct_selected)
    group_ids = sorted(selected_set)
    if not group_ids:
        raise RuntimeError("手动删除差异文件中没有资料组")

    placeholders = ",".join("?" for _ in group_ids)
    groups = db.execute(
        f"""SELECT * FROM groups
        WHERE group_id IN ({placeholders})
        ORDER BY group_id""",
        group_ids,
    ).fetchall()
    if len(groups) != len(group_ids):
        found = {str(row["group_id"]) for row in groups}
        missing = sorted(set(group_ids) - found)
        raise RuntimeError(
            "手动删除差异包含数据库中不存在的资料组: "
            + "、".join(missing[:10])
        )

    if detection_kind == "live_state_db_and_filesystem":
        group_snapshots = {
            str(item.get("group_id") or ""): int(item.get("updated_at") or 0)
            for item in payload.get("related_group_snapshots", [])
            if str(item.get("group_id") or "")
        }
        for group in groups:
            group_id = str(group["group_id"])
            expected_updated_at = group_snapshots.get(group_id, 0)
            if expected_updated_at <= 0:
                raise RuntimeError(
                    f"手动删除差异缺少关联资料组快照: {group_id}"
                )
            if int(group["updated_at"] or 0) != expected_updated_at:
                raise RuntimeError(
                    f"关联资料组在检测后已变化，拒绝级联删除: {group_id}"
                )

    # The transitive expansion above includes every exact-content membership.
    # Nothing with the same SHA is retained under a second stale group name.
    shared_shas: set[str] = set()

    allowed_roots = [
        cfg["folders"]["inbox"].resolve(),
        cfg["folders"]["failed"].resolve(),
        cfg["folders"]["markdown"].resolve(),
        cfg["folders"]["work"].resolve(),
    ]

    def allowed_file(candidate: Path) -> bool:
        resolved = candidate.resolve()
        return any(
            resolved != root and root in resolved.parents
            for root in allowed_roots
        )

    payload_snapshots: dict[tuple[str, str], str] = {}
    for item in payload.get("existing_markdown_snapshots", []):
        group_id = str(item.get("group_id") or "")
        raw_path = str(item.get("path") or "")
        digest = str(item.get("sha256") or "").casefold()
        if group_id and raw_path and re.fullmatch(r"[0-9a-f]{64}", digest):
            payload_snapshots[(group_id, str(Path(raw_path).resolve()))] = digest

    audit_snapshots: dict[tuple[str, str], str] = {}
    audit_selections: dict[str, dict] = {}
    if group_ids:
        for row in db.execute(
            f"""SELECT group_id,selection_json FROM manual_deletion_audit
            WHERE group_id IN ({placeholders})""",
            group_ids,
        ).fetchall():
            try:
                saved = json.loads(str(row["selection_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(saved, dict):
                continue
            audit_selections[str(row["group_id"])] = saved
            for item in saved.get("markdown_snapshots", []):
                raw_path = str(item.get("path") or "")
                digest = str(item.get("sha256") or "").casefold()
                if raw_path and re.fullmatch(r"[0-9a-f]{64}", digest):
                    audit_snapshots[
                        (str(row["group_id"]), str(Path(raw_path).resolve()))
                    ] = digest

    group_markdown_manifests: dict[str, dict[str, str]] = {}
    group_expected_markdown_manifests: dict[str, dict[str, str]] = {}
    manifest_errors: dict[str, list[str]] = {}
    for group in groups:
        group_id = str(group["group_id"])
        manifest: dict[str, str] = {}
        expected_manifest: dict[str, str] = {}
        errors_for_group: list[str] = []
        for candidate in sorted(
            group_local_markdown_paths(db, group),
            key=lambda path: str(path).casefold(),
        ):
            if not candidate.exists():
                continue
            if not candidate.is_file() or not allowed_file(candidate):
                errors_for_group.append(
                    f"Markdown路径不在允许目录或不是普通文件: {candidate}"
                )
                continue
            resolved = str(candidate.resolve())
            expected = payload_snapshots.get((group_id, resolved)) or audit_snapshots.get(
                (group_id, resolved)
            )
            strict_existing_snapshot = detection_kind in {
                "live_state_db_and_filesystem",
                "pending_manual_deletion_resume",
            }
            if not expected and strict_existing_snapshot:
                errors_for_group.append(
                    f"缺少删除检测时的Markdown摘要快照，拒绝删除: {candidate}"
                )
                continue
            if expected:
                expected_manifest[resolved] = expected
            try:
                current_digest = stable_sha256(candidate)
            except (OSError, RuntimeError) as exc:
                errors_for_group.append(
                    f"Markdown摘要无法确认 {candidate}: {exc}"
                )
                continue
            if expected and current_digest != expected:
                errors_for_group.append(
                    f"Markdown在检测后已被替换，保留新内容: {candidate}"
                )
                continue
            manifest[resolved] = expected or current_digest
            expected_manifest.setdefault(resolved, current_digest)
        group_markdown_manifests[group_id] = manifest
        group_expected_markdown_manifests[group_id] = expected_manifest
        manifest_errors[group_id] = errors_for_group

    local_source_paths: set[Path] = set()
    local_markdown_paths: set[Path] = set()
    remote_index_count = 0
    for group in groups:
        remote_index_count += sum(
            bool(group[field])
            for field in ("parent_doc_id", "child_doc_id", "raw_doc_id")
        )
        if group["markdown_path"]:
            local_markdown_paths.add(Path(group["markdown_path"]))
        if group["raw_path"]:
            local_markdown_paths.add(Path(group["raw_path"]))
        members = db.execute(
            """SELECT gf.sha256,gf.source_path,
            f.source_path AS canonical_source,
            f.markdown_path AS file_markdown
            FROM group_files gf
            LEFT JOIN files f ON f.sha256=gf.sha256
            WHERE gf.group_id=?""",
            (group["group_id"],),
        ).fetchall()
        for member in members:
            digest = str(member["sha256"])
            if digest not in shared_shas:
                if member["source_path"]:
                    local_source_paths.add(Path(member["source_path"]))
                if member["canonical_source"]:
                    local_source_paths.add(Path(member["canonical_source"]))
            if member["file_markdown"]:
                local_markdown_paths.add(Path(member["file_markdown"]))

    existing_sources = {
        path for path in local_source_paths
        if path.is_file() and allowed_file(path)
    }
    existing_markdown = {
        path for path in local_markdown_paths
        if path.is_file() and allowed_file(path)
    }
    summary = {
        "groups": len(groups),
        "selected_by_markdown": len(markdown_selected),
        "selected_by_source": len(source_selected),
        "existing_sources_to_delete": len(existing_sources),
        "existing_markdown_to_delete": len(existing_markdown),
        "remote_indexes_to_delete": remote_index_count,
        "shared_files_retained": len(shared_shas),
        "groups_completed": 0,
        "groups_pending": 0,
        "sources_deleted": 0,
        "markdown_deleted": 0,
        "indexes_deleted": 0,
    }
    if dry_run:
        return summary

    now = int(time.time())
    for group in groups:
        group_id = str(group["group_id"])
        previous_selection = audit_selections.get(group_id, {})
        selection = dict(previous_selection)
        if detection_kind != "pending_manual_deletion_resume":
            selection["markdown_deleted_by_user"] = bool(
                previous_selection.get("markdown_deleted_by_user")
            ) or group_id in markdown_selected
            selection["source_deleted_by_user"] = bool(
                previous_selection.get("source_deleted_by_user")
            ) or group_id in source_selected
        selection.setdefault("difference_file", str(selection_path))
        if (
            detection_kind == "historical_manual_deletion_sha_closure"
            or (
                detection_kind == "live_state_db_and_filesystem"
                and group_id not in direct_selected
            )
        ):
            selection["exact_sha_duplicate"] = True
        selection["markdown_snapshots"] = [
                {"path": path, "sha256": digest}
                for path, digest in sorted(
                    group_expected_markdown_manifests[group_id].items(),
                    key=lambda item: str(item[0]).casefold(),
                )
            ]
        db.execute(
            """INSERT INTO manual_deletion_audit(
                group_id,requested_at,state,selection_json,error
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(group_id) DO UPDATE SET
                requested_at=manual_deletion_audit.requested_at,
                state=excluded.state,
                selection_json=excluded.selection_json,
                error=''""",
            (
                group_id,
                now,
                "user_delete_pending",
                json.dumps(selection, ensure_ascii=False),
                "",
            ),
        )
        db.execute(
            """UPDATE groups
            SET state='user_delete_pending',
                error='用户在资源管理器中明确删除；正在同步永久排除',
                updated_at=?
            WHERE group_id=?""",
            (now, group_id),
        )
        member_shas = [
            str(row["sha256"])
            for row in db.execute(
                "SELECT DISTINCT sha256 FROM group_files WHERE group_id=?",
                (group_id,),
            ).fetchall()
        ]
        for digest in member_shas:
            if digest in shared_shas:
                continue
            db.execute(
                """UPDATE files
                SET state='user_delete_pending',
                    error='用户在资源管理器中明确删除；正在同步永久排除',
                    updated_at=?
                WHERE sha256=?""",
                (now, digest),
            )
    db.commit()

    wc = cfg["weknora"]
    for index, group in enumerate(groups, 1):
        group_id = str(group["group_id"])
        errors: list[str] = list(manifest_errors.get(group_id, []))
        group_sources_deleted = 0
        group_markdown_deleted = 0
        group_indexes_deleted = 0

        doc_fields = () if errors else (
            ("parent_doc_id", wc["parent_knowledge_base"]),
            ("child_doc_id", wc["child_knowledge_base"]),
            ("raw_doc_id", wc.get("raw_knowledge_base") or ""),
        )
        for field, knowledge_base in doc_fields:
            doc_id = str(group[field] or "")
            if not doc_id:
                continue
            if not knowledge_base:
                errors.append(f"{field}存在但对应知识库未配置")
                continue
            try:
                weknora_delete(
                    doc_id,
                    Path(group["markdown_path"] or ROOT / "markdown"),
                    wc,
                    knowledge_base,
                )
                db.execute(
                    f"UPDATE groups SET {field}=NULL,updated_at=? WHERE group_id=?",
                    (int(time.time()), group_id),
                )
                db.commit()
                group_indexes_deleted += 1
            except Exception as exc:
                message = str(exc)
                lowered = message.casefold()
                if (
                    "not found" in lowered
                    or "404" in lowered
                    or "不存在" in message
                ):
                    db.execute(
                        f"UPDATE groups SET {field}=NULL,updated_at=? WHERE group_id=?",
                        (int(time.time()), group_id),
                    )
                    db.commit()
                    group_indexes_deleted += 1
                else:
                    errors.append(f"{field}: {message}")

        members = db.execute(
            """SELECT gf.sha256,gf.source_path,
            f.source_path AS canonical_source,
            f.markdown_path AS file_markdown
            FROM group_files gf
            LEFT JOIN files f ON f.sha256=gf.sha256
            WHERE gf.group_id=?""",
            (group_id,),
        ).fetchall()
        paths_to_delete: dict[tuple[Path, str], set[str]] = {}

        def add_delete_path(candidate: Path, kind: str, digest: str = "") -> None:
            paths_to_delete.setdefault((candidate, kind), set())
            if digest:
                paths_to_delete[(candidate, kind)].add(digest)

        markdown_manifest = group_markdown_manifests.get(group_id, {})
        if group["markdown_path"]:
            candidate = Path(group["markdown_path"])
            add_delete_path(
                candidate,
                "markdown",
                markdown_manifest.get(str(candidate.resolve()), ""),
            )
        if group["raw_path"]:
            candidate = Path(group["raw_path"])
            add_delete_path(
                candidate,
                "markdown",
                markdown_manifest.get(str(candidate.resolve()), ""),
            )
        for member in members:
            digest = str(member["sha256"])
            if digest not in shared_shas:
                if member["source_path"]:
                    add_delete_path(Path(member["source_path"]), "source", digest)
                if member["canonical_source"]:
                    add_delete_path(
                        Path(member["canonical_source"]), "source", digest
                    )
            if member["file_markdown"]:
                candidate = Path(member["file_markdown"])
                add_delete_path(
                    candidate,
                    "markdown",
                    markdown_manifest.get(str(candidate.resolve()), ""),
                )
        # Do not destroy the recoverable local copy while a remote index still
        # failed to delete.  The pending audit can then retry idempotently.
        if errors:
            paths_to_delete.clear()
        for (candidate, kind), expected_digests in sorted(
            paths_to_delete.items(), key=lambda item: str(item[0][0]).casefold()
        ):
            if not candidate.exists():
                continue
            if not candidate.is_file() or not allowed_file(candidate):
                errors.append(f"拒绝删除允许目录外路径: {candidate}")
                continue
            if kind in {"source", "markdown"}:
                if len(expected_digests) != 1:
                    errors.append(
                        f"{kind}文件缺少唯一摘要，拒绝删除: {candidate}"
                    )
                    continue
                try:
                    current_digest = stable_sha256(candidate)
                except (OSError, RuntimeError) as exc:
                    errors.append(f"{kind}文件删除前摘要无法确认 {candidate}: {exc}")
                    continue
                expected_digest = next(iter(expected_digests))
                if current_digest != expected_digest:
                    errors.append(
                        f"{kind}文件在检测后已被替换，保留新内容: {candidate}"
                    )
                    continue
            try:
                candidate.unlink()
                if kind == "source":
                    group_sources_deleted += 1
                else:
                    group_markdown_deleted += 1
            except OSError as exc:
                errors.append(f"本地删除失败 {candidate}: {exc}")

        remaining = db.execute(
            """SELECT parent_doc_id,child_doc_id,raw_doc_id
            FROM groups WHERE group_id=?""",
            (group_id,),
        ).fetchone()
        final_state = (
            "user_delete_pending"
            if errors
            or any(
                remaining[field]
                for field in ("parent_doc_id", "child_doc_id", "raw_doc_id")
            )
            else "user_deleted"
        )
        finished_at = int(time.time()) if final_state == "user_deleted" else None
        db.execute(
            """UPDATE groups
            SET state=?,error=?,updated_at=?
            WHERE group_id=?""",
            (
                final_state,
                "；".join(errors)
                if errors
                else "用户手动删除；本地与三层索引已永久排除",
                int(time.time()),
                group_id,
            ),
        )
        for member in members:
            digest = str(member["sha256"])
            if digest in shared_shas:
                continue
            db.execute(
                """UPDATE files SET state=?,error=?,markdown_path=NULL,
                weknora_doc_id=NULL,updated_at=? WHERE sha256=?""",
                (
                    final_state,
                    "；".join(errors)
                    if errors
                    else "用户手动删除；已永久排除",
                    int(time.time()),
                    digest,
                ),
            )
        db.execute(
            """UPDATE manual_deletion_audit
            SET completed_at=?,state=?,
                deleted_sources=deleted_sources+?,
                deleted_markdown=deleted_markdown+?,
                deleted_indexes=deleted_indexes+?,
                error=?
            WHERE group_id=?""",
            (
                finished_at,
                final_state,
                group_sources_deleted,
                group_markdown_deleted,
                group_indexes_deleted,
                "；".join(errors),
                group_id,
            ),
        )
        db.commit()
        summary["sources_deleted"] += group_sources_deleted
        summary["markdown_deleted"] += group_markdown_deleted
        summary["indexes_deleted"] += group_indexes_deleted
        if final_state == "user_deleted":
            summary["groups_completed"] += 1
        else:
            summary["groups_pending"] += 1
        if index == 1 or index % 25 == 0 or index == len(groups):
            print(
                f"手动删除同步：{index}/{len(groups)}｜"
                f"完成{summary['groups_completed']}｜"
                f"待重试{summary['groups_pending']}｜"
                f"索引删除{summary['indexes_deleted']}"
            )

    for root in allowed_roots:
        if not root.is_dir():
            continue
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass
    return summary


def reconcile_user_deleted_file_states(db: sqlite3.Connection) -> int:
    """Close stale file rows once every linked group is permanently deleted."""
    rows = db.execute(
        """SELECT f.sha256 FROM files f
        WHERE f.state != 'user_deleted'
        AND EXISTS (
            SELECT 1 FROM group_files gf WHERE gf.sha256=f.sha256
        )
        AND NOT EXISTS (
            SELECT 1 FROM group_files gf
            JOIN groups g ON g.group_id=gf.group_id
            WHERE gf.sha256=f.sha256 AND g.state != 'user_deleted'
        )"""
    ).fetchall()
    if not rows:
        return 0
    now = int(time.time())
    db.executemany(
        """UPDATE files SET state='user_deleted',
        error='用户手动删除；全部关联资料组均已永久排除',
        markdown_path=NULL,weknora_doc_id=NULL,
        updated_at=? WHERE sha256=?""",
        [(now, str(row["sha256"])) for row in rows],
    )
    db.commit()
    return len(rows)


def reconcile_appledouble_history(db: sqlite3.Connection) -> int:
    """Close historical macOS resource-fork rows that can never be submitted."""
    rows = db.execute(
        """SELECT sha256,source_path FROM files
        WHERE state='queued' AND coalesce(batch_id,'')=''
        AND NOT EXISTS (
            SELECT 1 FROM group_files gf WHERE gf.sha256=files.sha256
        )"""
    ).fetchall()
    stale = []
    for row in rows:
        source = Path(str(row["source_path"] or ""))
        if source.is_file():
            continue
        if is_appledouble_path(source):
            stale.append(str(row["sha256"]))
    if not stale:
        return 0
    now = int(time.time())
    db.executemany(
        """UPDATE files SET state='excluded_completed',
        error='macOS AppleDouble资源分叉历史记录；源文件不存在且不属于题库正文',
        batch_id=NULL,markdown_path=NULL,weknora_doc_id=NULL,
        updated_at=? WHERE sha256=?""",
        [(now, digest) for digest in stale],
    )
    db.commit()
    return len(stale)


def auto_sync_manual_deletions(
    cfg: dict,
    db: sqlite3.Connection,
) -> dict[str, int]:
    """Apply the user's any-side deletion rule at safe round boundaries."""
    if not bool((cfg.get("manual_deletions") or {}).get("auto_sync", False)):
        return {
            "detected_groups": 0,
            "historical_closure_groups": 0,
            "groups_completed": 0,
            "groups_pending": 0,
            "reconciled_files": 0,
        }
    require_manual_deletion_sync_confirmation()
    selection_path = (
        cfg["folders"]["work"] / "manual-deletion-auto-current.json"
    )
    pending_ids = [
        str(row["group_id"])
        for row in db.execute(
            "SELECT group_id FROM groups WHERE state='user_delete_pending'"
        ).fetchall()
    ]
    pending_result = {"groups_completed": 0, "groups_pending": 0}
    if pending_ids:
        atomic_write(
            selection_path,
            json.dumps(
                {
                    "generated_at": datetime.now().astimezone().isoformat(),
                    "detection": "pending_manual_deletion_resume",
                    "newly_missing_markdown": [],
                    "newly_missing_sources": [
                        {"group_id": group_id} for group_id in pending_ids
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        with single_instance(".manual-deletion-sync.lock"):
            pending_result = sync_manual_deletions(
                cfg, db, selection_path, dry_run=False
            )
    detected = detect_manual_deletions(
        cfg, db, selection_path, grace_seconds=60
    )
    result = {
        "detected_groups": int(detected["affected_groups"]),
        "historical_closure_groups": 0,
        "groups_completed": int(pending_result["groups_completed"]),
        "groups_pending": int(pending_result["groups_pending"]),
        "reconciled_files": 0,
    }
    if result["detected_groups"]:
        with single_instance(".manual-deletion-sync.lock"):
            synced = sync_manual_deletions(
                cfg, db, selection_path, dry_run=False
            )
        result["groups_completed"] += int(synced["groups_completed"])
        result["groups_pending"] += int(synced["groups_pending"])
    closure_rows = db.execute(
        """SELECT DISTINCT active.group_id
        FROM groups deleted
        JOIN group_files deleted_member
            ON deleted_member.group_id=deleted.group_id
        JOIN group_files active_member
            ON active_member.sha256=deleted_member.sha256
            AND active_member.group_id!=deleted.group_id
        JOIN groups active ON active.group_id=active_member.group_id
        LEFT JOIN manual_deletion_audit audit
            ON audit.group_id=deleted.group_id
        WHERE deleted.state='user_deleted'
        AND deleted.error LIKE '用户手动删除%'
        AND coalesce(audit.selection_json,'')
            NOT LIKE '%exact_sha_duplicate%'
        AND active.state NOT IN (
            'user_delete_pending','user_deleted'
        )"""
    ).fetchall()
    closure_ids = sorted({str(row["group_id"]) for row in closure_rows})
    if closure_ids:
        payload = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "detection": "historical_manual_deletion_sha_closure",
            "grace_seconds": 0,
            "newly_missing_markdown": [],
            "newly_missing_sources": [
                {"group_id": group_id} for group_id in closure_ids
            ],
        }
        atomic_write(
            selection_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        with single_instance(".manual-deletion-sync.lock"):
            closure_sync = sync_manual_deletions(
                cfg,
                db,
                selection_path,
                dry_run=False,
            )
        result["historical_closure_groups"] = len(closure_ids)
        result["groups_completed"] += int(
            closure_sync["groups_completed"]
        )
        result["groups_pending"] += int(
            closure_sync["groups_pending"]
        )
    result["reconciled_files"] = reconcile_user_deleted_file_states(db)
    selection_path.unlink(missing_ok=True)
    return result


def weknora_verify(
    md_path: Path,
    source: Path,
    cfg: dict,
    doc_id: str,
    wait: bool = True,
    knowledge_base: str | None = None,
    mode: str = "hybrid",
) -> None:
    values = weknora_values(
        md_path, source, cfg, doc_id, knowledge_base=knowledge_base
    )
    if wait:
        run_command(
            cfg["wait_command"], values, md_path.parent, timeout_seconds=3900
        )
    viewed = run_command(
        [
            cfg["upload_command"][0],
            "doc",
            "view",
            "{doc_id}",
            "--format",
            "json",
            "--profile",
            "{profile}",
        ],
        values,
        md_path.parent,
    )
    document = viewed.get("data", viewed)
    if document.get("parse_status") != "completed":
        raise RuntimeError(
            f"WeKnora文档状态尚未完成: {document.get('parse_status')}"
        )
    expected_hash = md5_digest(md_path)
    remote_hash = str(document.get("file_hash") or "").casefold()
    if not remote_hash or remote_hash != expected_hash:
        raise RuntimeError(
            "WeKnora目标文档内容摘要缺失或不匹配，拒绝把陈旧文档当作验收成功"
        )
    expected_kb = str(knowledge_base or cfg["knowledge_base"])
    actual_kb = str(document.get("knowledge_base_id") or "")
    if not actual_kb or actual_kb != expected_kb:
        raise RuntimeError(
            "WeKnora目标文档知识库归属缺失或不匹配，拒绝跨库验收"
        )
    chunks = run_command(
        [
            cfg["upload_command"][0],
            "chunk",
            "list",
            "--doc",
            "{doc_id}",
            "--limit",
            "1",
            "--format",
            "json",
            "--profile",
            "{profile}",
        ],
        values,
        md_path.parent,
    ).get("data") or []
    if not chunks or not str(chunks[0].get("content") or "").strip():
        raise RuntimeError("WeKnora目标文档没有可读取分块")
    body = markdown_body(md_path)
    group_match = re.search(
        r"资料：(.+?)(?:\s+分类：|\r?\n|$)",
        body,
    )
    queries = [values["query"]]
    if group_match:
        queries.append(group_match.group(1).strip())
    normalized_stem = re.sub(
        r"-[0-9a-f]{16}(?:-原文)?(?:-结构v\d+)?"
        r"(?:-分类v\d+)?(?:\.children)?$",
        "",
        md_path.stem,
        flags=re.IGNORECASE,
    )
    queries.extend((normalized_stem, source.stem))
    for query in dict.fromkeys(item for item in queries if item):
        current_values = {**values, "query": query}
        command = list(cfg["search_command"])
        command.extend(
            ["--limit", str(int(cfg.get("verification_limit", 50)))]
        )
        if mode == "vector":
            command.append("--no-keyword")
        elif mode == "keyword":
            command.append("--no-vector")
        searched = run_command(command, current_values, md_path.parent)
        hits = searched.get("data") or []
        if any(hit.get("knowledge_id") == doc_id for hit in hits):
            return
    raise RuntimeError(
        f"WeKnora目标分块存在，但{mode}检索未命中当前文档，拒绝把其他文档的命中当作成功"
    )


def full_route_check_due(cfg: dict) -> bool:
    """Check every retrieval route at startup and then periodically."""
    global VERIFICATION_COUNTER
    interval = max(1, int(cfg.get("full_route_check_every", 50)))
    with VERIFICATION_COUNTER_LOCK:
        VERIFICATION_COUNTER += 1
        current = VERIFICATION_COUNTER
    return current == 1 or current % interval == 0


def verify_two_level_indexes(
    parent_path: Path,
    child_path: Path,
    source: Path,
    cfg: dict,
    parent_doc_id: str,
    child_doc_id: str,
    classification: DocumentClassification | None = None,
    full_check: bool = True,
    raw_path: Path | None = None,
    raw_doc_id: str = "",
) -> None:
    parent_kb = cfg["parent_knowledge_base"]
    child_kb = cfg["child_knowledge_base"]
    raw_kb = str(cfg.get("raw_knowledge_base") or "")
    initial_specs = [
        (parent_path, parent_doc_id, parent_kb),
        (child_path, child_doc_id, child_kb),
    ]
    if raw_path is not None and raw_doc_id and raw_kb:
        initial_specs.append((raw_path, raw_doc_id, raw_kb))
    with ThreadPoolExecutor(max_workers=len(initial_specs)) as executor:
        initial = [
            executor.submit(
                weknora_verify,
                path,
                source,
                cfg,
                doc_id,
                True,
                kb,
                "hybrid",
            )
            for path, doc_id, kb in initial_specs
        ]
        for future in initial:
            future.result()
    if not full_check:
        return
    checks = (
        (parent_path, parent_doc_id, parent_kb, "vector"),
        (parent_path, parent_doc_id, parent_kb, "keyword"),
        (child_path, child_doc_id, child_kb, "vector"),
        (child_path, child_doc_id, child_kb, "keyword"),
    )
    if raw_path is not None and raw_doc_id and raw_kb:
        checks += (
            (raw_path, raw_doc_id, raw_kb, "vector"),
            (raw_path, raw_doc_id, raw_kb, "keyword"),
        )
    with ThreadPoolExecutor(max_workers=len(checks)) as executor:
        futures = [
            executor.submit(
                weknora_verify,
                path,
                source,
                cfg,
                doc_id,
                False,
                kb,
                mode,
            )
            for path, doc_id, kb, mode in checks
        ]
        for future in futures:
            future.result()


def parallel_hybrid_search(query: str, cfg: dict) -> dict[str, list[dict]]:
    def search(knowledge_base: str) -> list[dict]:
        values = {
            "root": str(ROOT),
            "query": query,
            "kb": knowledge_base,
            "profile": cfg["profile"],
        }
        result = run_command(cfg["search_command"], values, ROOT)
        return result.get("data") or []

    knowledge_bases = {
        "parent": cfg["parent_knowledge_base"],
        "child": cfg["child_knowledge_base"],
    }
    if cfg.get("raw_knowledge_base"):
        knowledge_bases["raw"] = cfg["raw_knowledge_base"]
    with ThreadPoolExecutor(max_workers=len(knowledge_bases)) as executor:
        futures = {
            layer: executor.submit(search, knowledge_base)
            for layer, knowledge_base in knowledge_bases.items()
        }
        layers = {
            layer: future.result() for layer, future in futures.items()
        }

    explicit_type = next(
        (
            document_type
            for document_type in ("试卷", "习题册", "讲义", "教材")
            if document_type in query
        ),
        "",
    )
    type_weights = cfg.get("document_type_weights") or {
        "试卷": 1.28,
        "习题册": 1.24,
        "讲义": 1.14,
        "教材": 0.86,
    }
    layer_weights = cfg.get("layer_weights") or {
        "parent": 1.06,
        "child": 1.10,
        "raw": 0.94,
    }

    def document_type_of(hit: dict) -> str:
        metadata = hit.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        value = str(metadata.get("document_type") or "")
        if value:
            return value
        content = str(hit.get("content") or hit.get("matched_content") or "")
        match = re.search(r"分类：(教材|试卷|习题册|讲义)", content)
        return match.group(1) if match else ""

    fused: dict[str, dict] = {}
    for layer, hits in layers.items():
        for rank, hit in enumerate(hits, 1):
            content = re.sub(
                r"\s+", " ", str(hit.get("content") or hit.get("matched_content") or "")
            ).strip()
            key = hashlib.sha256(content.encode("utf-8")).hexdigest() if content else str(
                hit.get("id") or f"{layer}:{rank}"
            )
            if key not in fused:
                fused[key] = {**hit, "index_layer": layer, "fusion_score": 0.0}
            document_type = document_type_of(hit)
            type_weight = (
                1.35
                if explicit_type and document_type == explicit_type
                else (0.92 if explicit_type and document_type else type_weights.get(
                    document_type, 1.0
                ))
            )
            fused[key]["document_type"] = document_type
            fused[key]["fusion_score"] += (
                layer_weights.get(layer, 1.0)
                * type_weight
                / (60 + rank)
            )
    merged = sorted(
        fused.values(), key=lambda item: item["fusion_score"], reverse=True
    )
    return {**layers, "merged": merged}


def verify_embedding_model(cfg: dict, db: sqlite3.Connection) -> None:
    wc = cfg["weknora"]
    executable = wc["upload_command"][0]
    views = []
    chunk_sizes = wc.get("chunk_sizes") or {}
    knowledge_bases = [
        ("父块", wc["parent_knowledge_base"], int(chunk_sizes.get("parent", 0))),
        ("子块", wc["child_knowledge_base"], int(chunk_sizes.get("child", 0))),
    ]
    if wc.get("raw_knowledge_base"):
        knowledge_bases.append(
            ("原文", wc["raw_knowledge_base"], int(chunk_sizes.get("raw", 0)))
        )
    for layer, kb_id, expected_size in knowledge_bases:
        kb_view = run_command(
            [
                executable, "kb", "view", "{kb}", "--format", "json",
                "--profile", wc["profile"],
            ],
            {"root": str(ROOT), "kb": kb_id},
            ROOT,
        )
        kb_data = kb_view.get("data", kb_view)
        if not kb_data.get("embedding_model_id"):
            raise RuntimeError(f"WeKnora{layer}知识库没有绑定Embedding模型")
        chunking = kb_data.get("chunking_config") or {}
        if chunking.get("enable_parent_child"):
            raise RuntimeError(f"WeKnora{layer}知识库错误启用了原生父子模式")
        if expected_size > 0 and int(chunking.get("chunk_size") or 0) != expected_size:
            raise RuntimeError(
                f"WeKnora{layer}知识库块大小错误: "
                f"{chunking.get('chunk_size')} != {expected_size}"
            )
        views.append(kb_data)
    model_ids = {view["embedding_model_id"] for view in views}
    if len(model_ids) != 1:
        raise RuntimeError("父块、子块和原文知识库没有使用同一个Embedding模型")
    model_id = next(iter(model_ids))
    fingerprint = ":".join(
        [
            str(model_id),
            str(wc["models"]["provider"]),
            str(wc["models"]["embedding"]),
            str(wc["models"]["embedding_dimension"]),
            *(str(kb_id) for _, kb_id, _ in knowledge_bases),
        ]
    )
    # A persisted success cannot prove that Ollama, its model, or the
    # Docker-to-host route still works after either process restarts. Cache the
    # expensive probe only inside this Python process; every fresh invocation
    # must perform one real embedding call before it can ingest or delete data.
    with EMBEDDING_VERIFICATION_LOCK:
        if fingerprint in EMBEDDING_VERIFIED_THIS_PROCESS:
            print("Embedding已在本次进程中真实调用通过，跳过重复测试")
            return
        local = wc["models"]["provider"] == "ollama"
        request_body = {
            "modelId": model_id,
            "modelName": wc["models"]["embedding"],
            "source": "local" if local else "remote",
            "provider": wc["models"]["provider"],
            "baseUrl": "",
            "dimension": wc["models"]["embedding_dimension"],
            "supportsDimensionOverride": local,
        }
        checked = run_command(
            [
                executable,
                "api",
                "/api/v1/initialization/embedding/test",
                "-d",
                json.dumps(request_body, ensure_ascii=False),
                "--format",
                "json",
                "--profile",
                wc["setup_profile"],
            ],
            {"root": str(ROOT)},
            ROOT,
            timeout_seconds=180,
        )
        server_data = checked.get("data", checked)
        result = server_data.get("data", server_data)
        expected = wc["models"]["embedding_dimension"]
        if not result.get("available") or result.get("dimension") != expected:
            raise RuntimeError(f"Embedding实际调用未通过: {result}")
        EMBEDDING_VERIFIED_THIS_PROCESS.add(fingerprint)
        db.execute(
            """INSERT INTO metadata(key,value) VALUES('verified_embedding',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (fingerprint,),
        )
        db.commit()


def verify_heading_chunker(cfg: dict) -> None:
    wc = cfg["weknora"]
    executable = wc["upload_command"][0]
    chunk_sizes = wc.get("chunk_sizes") or {}
    configured = (
        ("父块", int(chunk_sizes.get("parent", 0))),
        ("子块", int(chunk_sizes.get("child", 0))),
    )
    for layer, chunk_size in configured:
        if chunk_size <= 0:
            continue
        body = {
            "text": "# 题目一\n题干甲。\n\n# 题目二\n题干乙。",
            "chunking_config": {
                "chunk_size": chunk_size,
                "chunk_overlap": 0,
                "separators": ["\n\n", "\n", "。"],
                "strategy": "heading",
                "token_limit": 0,
                "languages": ["zh"],
            },
        }
        checked = run_command(
            [
                executable,
                "api",
                "/api/v1/chunker/preview",
                "-d",
                json.dumps(body, ensure_ascii=False),
                "--format",
                "json",
                "--profile",
                wc["setup_profile"],
            ],
            {"root": str(ROOT)},
            ROOT,
            timeout_seconds=60,
        )
        outer = checked.get("data", checked)
        result = outer.get("data", outer)
        chunks = result.get("chunks") or []
        if result.get("selected_tier") != "heading" or len(chunks) != 2:
            raise RuntimeError(
                f"WeKnora{layer}标题分块预检失败: "
                f"tier={result.get('selected_tier')}, chunks={len(chunks)}"
            )


def adaptive_embedding_keep_alive() -> str:
    _, available_gb = windows_memory_gb()
    if available_gb >= 4:
        return "30m"
    if available_gb >= 2.5:
        return "15m"
    if available_gb >= 1.5:
        return "5m"
    return "1m"


def warm_embedding_model(cfg: dict) -> None:
    ollama = cfg["ollama"]
    wc = cfg["weknora"]
    keep_alive = adaptive_embedding_keep_alive()
    expected = int(wc["models"]["embedding_dimension"])
    fingerprint = ":".join(
        (
            str(ollama["base_url"]),
            str(wc["models"]["embedding"]),
            str(expected),
            str(keep_alive),
        )
    )
    with EMBEDDING_VERIFICATION_LOCK:
        if fingerprint in EMBEDDING_WARMED_THIS_PROCESS:
            return
        response = requests.post(
            f"{ollama['base_url']}/api/embed",
            json={
                "model": wc["models"]["embedding"],
                "input": "题库检索预热",
                "keep_alive": keep_alive,
            },
            timeout=15,
        )
        response.raise_for_status()
        embeddings = response.json().get("embeddings") or []
        if not embeddings or len(embeddings[0]) != expected:
            raise RuntimeError(f"Embedding预热维度异常，期望{expected}")
        EMBEDDING_WARMED_THIS_PROCESS.add(fingerprint)
        print(f"Embedding已预热并弹性驻留{keep_alive}")


def preflight(cfg: dict, db: sqlite3.Connection) -> None:
    required: set[str] = set()
    if cfg["ollama"].get("mimo", {}).get("fallback_to_ollama", False):
        required.add(
            cfg["ollama"]["vision_model"]
        )
    classification_fallback = cfg["ollama"].get("mimo", {}).get(
        "classification_fallback_to_ollama", "auto"
    )
    if str(classification_fallback).casefold() in {"auto", "true", "1", "yes"}:
        required.add(cfg["ollama"]["classification_model"])
    wc = cfg["weknora"]
    embedding_is_local = wc["models"].get("provider") == "ollama"
    if embedding_is_local:
        required.add(wc["models"]["embedding"])
    if required:
        response = requests.get(f"{cfg['ollama']['base_url']}/api/tags", timeout=10)
        response.raise_for_status()
        tags = response.json()
        available = {item["name"] for item in tags.get("models", [])}
        if missing := required - available:
            raise RuntimeError(f"Ollama缺少模型: {sorted(missing)}")
    verify_embedding_model(cfg, db)
    if embedding_is_local:
        try:
            warm_embedding_model(cfg)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Embedding预热失败: {type(exc).__name__}"
            ) from exc
    status_cmd = [
        wc["upload_command"][0], "kb", "status", "{kb}", "--format", "json",
        "--profile", wc["profile"],
    ]
    for layer, kb_id in (
        ("父块", wc["parent_knowledge_base"]),
        ("子块", wc["child_knowledge_base"]),
        ("原文", wc["raw_knowledge_base"]),
    ):
        status = run_command(
            status_cmd, {"root": str(ROOT), "kb": kb_id}, ROOT
        )
        if not status.get("data", status).get("retrieval_ready"):
            raise RuntimeError(
                f"WeKnora{layer}知识库尚未达到可检索状态，未开始MinerU解析"
            )
    verify_heading_chunker(cfg)


def clean_job(job: Path, work_root: Path) -> None:
    resolved, root = job.resolve(), work_root.resolve()
    if resolved == root or root not in resolved.parents:
        raise RuntimeError(f"拒绝清理work目录之外的路径: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    # NUL bytes can survive extraction from damaged Office/PDF sources.
    # They are invalid in subprocess arguments and PostgreSQL text fields.
    temporary.write_text(text.replace("\x00", ""), "utf-8")
    os.replace(temporary, path)


def atomic_write_text_sha256(text: str) -> str:
    """Return the byte digest produced by atomic_write on this platform."""
    cleaned = text.replace("\x00", "")
    if os.linesep != "\n":
        cleaned = cleaned.replace("\n", os.linesep)
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def read_direct_text(source: Path) -> str:
    raw = source.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            text = raw.decode(encoding)
            if text.strip():
                if source.suffix.casefold() == ".html":
                    text = re.sub(
                        r"(?is)<(script|style)\b.*?</\1>", " ", text
                    )
                    text = re.sub(r"(?s)<[^>]+>", "\n", text)
                    text = html.unescape(text)
                    text = re.sub(r"\n[ \t]*\n+", "\n\n", text)
                return text
        except UnicodeDecodeError:
            continue
    raise RuntimeError("文本文件编码无法识别或正文为空")


def unique_failed_path(source: Path, digest: str, failed_dir: Path) -> Path:
    base = failed_dir / f"{source.stem}-{digest[:10]}{source.suffix}"
    if not base.exists():
        return base
    index = 2
    while True:
        candidate = failed_dir / f"{source.stem}-{digest[:10]}-{index}{source.suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def wait_until_stable(source: Path, seconds: int = 3) -> None:
    first = source.stat()
    time.sleep(seconds)
    second = source.stat()
    if (first.st_size, first.st_mtime_ns) != (second.st_size, second.st_mtime_ns):
        raise RuntimeError("文件仍在复制或写入，请稍后自动重试")


def reconcile_pending_deletion_audits(db: sqlite3.Connection) -> dict[str, int]:
    """Close deletion intents interrupted between unlink and final audit update."""
    result = {"confirmed_missing": 0, "not_deleted": 0, "uncertain": 0}
    rows = db.execute(
        """SELECT * FROM deletion_audit
        WHERE success=0 AND completed_at IS NULL ORDER BY id"""
    ).fetchall()
    now = int(time.time())
    for row in rows:
        source = Path(row["source_path"])
        canonical = Path(row["markdown_path"])
        if source.exists():
            db.execute(
                """UPDATE deletion_audit SET completed_at=?,error=?
                WHERE id=? AND success=0 AND completed_at IS NULL""",
                (
                    now,
                    "启动恢复确认源文件仍存在；此前删除未完成",
                    row["id"],
                ),
            )
            result["not_deleted"] += 1
            continue
        try:
            retained_matches = (
                canonical.is_file()
                and stable_sha256(canonical)
                == str(row["markdown_sha256"] or "")
            )
        except (OSError, RuntimeError):
            retained_matches = False
        if retained_matches:
            db.execute(
                """UPDATE deletion_audit
                SET completed_at=?,success=1,error=?
                WHERE id=? AND success=0 AND completed_at IS NULL""",
                (
                    now,
                    "启动恢复确认：删除意图提交后源文件已不存在",
                    row["id"],
                ),
            )
            result["confirmed_missing"] += 1
        else:
            db.execute(
                """UPDATE deletion_audit SET completed_at=?,error=?
                WHERE id=? AND success=0 AND completed_at IS NULL""",
                (
                    now,
                    "源文件已不存在，但保留Markdown缺失或摘要变化；无法确认删除结果",
                    row["id"],
                ),
            )
            result["uncertain"] += 1
    if rows:
        db.commit()
    return result


def reconcile_deleted_sources(db: sqlite3.Connection) -> None:
    audit_result = reconcile_pending_deletion_audits(db)
    if any(audit_result.values()):
        print(
            "删除审计中断恢复："
            f"确认已不存在{audit_result['confirmed_missing']}｜"
            f"确认未删除{audit_result['not_deleted']}｜"
            f"无法确认{audit_result['uncertain']}"
        )
    rows = db.execute(
        "SELECT * FROM files WHERE state='verified'"
    ).fetchall()
    for row in rows:
        source = Path(row["source_path"])
        markdown = Path(row["markdown_path"]) if row["markdown_path"] else None
        if not source.exists() and markdown and markdown.is_file():
            save_state(
                db,
                row["sha256"],
                source,
                "completed",
                row["batch_id"],
                row["markdown_path"],
                weknora_doc_id=row["weknora_doc_id"],
            )
    group_rows = db.execute(
        """SELECT * FROM groups
        WHERE state IN ('verified','cleanup_pending','completed')"""
    ).fetchall()
    for group in group_rows:
        members = db.execute(
            "SELECT * FROM group_files WHERE group_id=?",
            (group["group_id"],),
        ).fetchall()
        if not members:
            continue
        missing = 0
        for member in members:
            source = Path(member["source_path"])
            if source.exists():
                continue
            missing += 1
            file_row = db.execute(
                "SELECT * FROM files WHERE sha256=?",
                (member["sha256"],),
            ).fetchone()
            save_state(
                db,
                member["sha256"],
                source,
                "completed",
                file_row["batch_id"] if file_row else None,
                group["markdown_path"],
                error="索引已验证；启动恢复时确认源文件已经不存在",
                weknora_doc_id=group["parent_doc_id"],
            )
        if group["state"] != "completed":
            state = "completed" if missing == len(members) else "cleanup_pending"
            save_group_state(
                db,
                group["group_id"],
                group["group_name"],
                state,
                Path(group["markdown_path"]) if group["markdown_path"] else None,
                group["parent_doc_id"],
                group["child_doc_id"],
                "" if state == "completed" else "部分源文件已删除，等待继续清理",
            )


def process(
    source: Path,
    cfg: dict,
    db: sqlite3.Connection,
    defer_index: bool = False,
    mineru_preferred_slot: str | None = None,
) -> Path | None:
    path_row = db.execute(
        "SELECT * FROM files WHERE source_path=? ORDER BY updated_at DESC LIMIT 1",
        (str(source),),
    ).fetchone()
    if not path_row or not decode_batch_ids(path_row["batch_id"]):
        wait_until_stable(source)
    if source.suffix.casefold() in DIRECT_TEXT:
        direct_text_limit = max(
            1,
            int(cfg["mineru"].get("max_mb", 200)),
        ) * 1024 * 1024
        if source.stat().st_size > direct_text_limit:
            raise RuntimeError(
                "直接文本文件超过本地处理上限，"
                f"当前上限为{direct_text_limit // (1024 * 1024)}MB"
            )
    digest = stable_sha256(source)
    row = (
        path_row
        if path_row and str(path_row["sha256"]) == digest
        else db.execute(
        "SELECT * FROM files WHERE sha256=?", (digest,)
        ).fetchone()
    )
    if row and str(row["source_path"] or "") != str(source):
        # The files table is content-addressed.  Never reuse another path's
        # mutable batch/Markdown state merely because the bytes are identical.
        row = None
    if defer_index and row and row["state"] == "completed":
        # A byte-identical source can legitimately participate in a different
        # question/answer group. Its old canonical Markdown already contains
        # the previous group's merge, so it must not be reused as parsed input.
        row = None
    instance_token = source_path_token(source)
    job = cfg["folders"]["work"] / f"{digest[:16]}-{instance_token}"
    final = (
        Path(row["markdown_path"])
        if row and row["markdown_path"]
        else cfg["folders"]["markdown"]
        / f"{source.stem}-{digest[:10]}-{instance_token}.md"
    )
    batch_ids = decode_batch_ids(row["batch_id"] if row else None)
    doc_id = row["weknora_doc_id"] if row else None
    metrics = file_metrics(row)
    active_attempt_started = time.perf_counter()
    tokens = mineru_tokens()
    if not metrics["started_at"]:
        metrics["started_at"] = int(time.time())
    if not metrics["source_pages"] and source.suffix.lower() == ".pdf":
        metrics["source_pages"] = len(PdfReader(str(source)).pages)
    try:
        if row and row["state"] == "completed":
            if not doc_id or not final.is_file():
                raise RuntimeError("同一文件已有完成记录，但缺少可验证的文档ID或Markdown")
            weknora_verify(final, source, cfg["weknora"], doc_id, wait=False)
            if cfg["cleanup"]["permanently_delete_source_after_search"]:
                delete_source_with_audit(
                    db,
                    source,
                    digest,
                    final,
                    "重复文件索引复核通过",
                )
                print(f"重复文件已确认入库并永久删除: {source.name}")
            return

        can_resume_index = bool(
            row
            and final.is_file()
            and row["state"] in {"parsed", "indexing", "verified", "failed"}
        )
        if not can_resume_index and source.suffix.casefold() in DIRECT_TEXT:
            clean_job(job, cfg["folders"]["work"])
            direct_text = read_direct_text(source)
            metadata = (
                "---\n"
                f"source_file: {json.dumps(source.name, ensure_ascii=False)}\n"
                f"source_sha256: {digest}\n"
                "parser: direct-text\n---\n\n"
            )
            atomic_write(final, metadata + direct_text.strip() + "\n")
            save_state(
                db,
                digest,
                source,
                "indexing",
                md_path=str(final),
                metrics=metrics,
            )
            can_resume_index = True
        if not can_resume_index:
            clean_job(job, cfg["folders"]["work"])
            parts = split_source(source, job, cfg["mineru"])

            def persist_created_batch(batch: MinerUBatch) -> None:
                batch_ids.append(batch)
                metrics["mineru_token_slots"] = sorted({
                    item.token_slot for item in batch_ids
                })
                save_state(
                    db,
                    digest,
                    source,
                    "parsing",
                    batch_id=encode_batch_ids(batch_ids),
                    md_path=str(final),
                    metrics=metrics,
                )

            def submit_current_parts(reason: str = "") -> None:
                batch_ids.clear()
                metrics["mineru_batch_submitted_at"] = int(time.time())
                if reason:
                    save_state(
                        db,
                        digest,
                        source,
                        "submitting",
                        batch_id="[]",
                        md_path=str(final),
                        error=reason,
                        metrics=metrics,
                    )
                mineru_submit(
                    parts,
                    tokens,
                    cfg["mineru"],
                    on_batch_created=persist_created_batch,
                    preferred_slot=mineru_preferred_slot,
                )

            if not (row and row["state"] == "parsing" and batch_ids):
                batch_ids = []
                submit_current_parts()
            mineru_wait_started = time.perf_counter()
            try:
                items = mineru_wait(batch_ids, tokens, cfg["mineru"])
            except MinerURepartitionRequired:
                submit_current_parts(
                    "旧MinerU任务超过当前200页限制；已按190页重新分卷提交"
                )
                raise MinerURetryLater(
                    "超过200页的旧任务已按190页重新分卷，等待云端解析"
                )
            except MinerUWaitingFile as exc:
                stale_after = max(
                    60,
                    int(
                        cfg["mineru"].get(
                            "waiting_file_resubmit_seconds", 600
                        )
                    ),
                )
                submitted_at = int(
                    metrics.get("mineru_batch_submitted_at")
                    or 0
                )
                # New submissions persist this timestamp. Legacy batches do
                # not, so treating their frequently refreshed updated_at as
                # the submit time would prevent them from ever becoming stale.
                row_age = (
                    stale_after
                    if submitted_at <= 0
                    else max(0, int(time.time()) - submitted_at)
                )
                if row_age >= stale_after:
                    submit_current_parts(
                        "MinerU任务长期停留waiting-file；已重新上传并提交"
                    )
                    raise MinerURetryLater(
                        "长期waiting-file任务已重新提交，等待云端解析"
                    ) from exc
                raise MinerURetryLater(str(exc)) from exc
            metrics["mineru_wait_seconds"] = round(
                metrics["mineru_wait_seconds"]
                + time.perf_counter()
                - mineru_wait_started,
                3,
            )
            metrics["mineru_cloud_done_at"] = int(time.time())
            download_started = time.perf_counter()
            parsed = download_results(items, parts, job)
            metrics["mineru_download_seconds"] = round(
                metrics["mineru_download_seconds"]
                + time.perf_counter()
                - download_started,
                3,
            )
            sections = []
            content_parts = []
            for part, md_file in parsed:
                text = md_file.read_text("utf-8")
                sections.append(
                    repair_images(
                        text, md_file, part.path, job, cfg["ollama"], metrics
                    )
                )
                if cfg["mineru"].get("save_content_json"):
                    for path in md_file.parent.rglob("*content_list.json"):
                        content_parts.append(
                            {
                                "file": part.path.name,
                                "start_page": part.start_page,
                                "content": json.loads(path.read_text("utf-8")),
                            }
                        )
            metadata = (
                "---\n"
                f"source_file: {json.dumps(source.name, ensure_ascii=False)}\n"
                f"source_sha256: {digest}\n"
                "parser: mineru-v4\n---\n\n"
            )
            atomic_write(final, metadata + "\n\n".join(sections))
            if content_parts:
                atomic_write(
                    final.with_suffix(".json"),
                    json.dumps(
                        {
                            "source_file": source.name,
                            "sha256": digest,
                            "parts": content_parts,
                        },
                        ensure_ascii=False,
                    ),
                )
            save_state(
                db,
                digest,
                source,
                "indexing",
                encode_batch_ids(batch_ids),
                str(final),
                metrics=metrics,
            )

        if defer_index:
            save_state(
                db,
                digest,
                source,
                "parsed",
                encode_batch_ids(batch_ids),
                str(final),
                metrics=metrics,
            )
            if cfg["cleanup"]["delete_temporary_files"] and job.exists():
                shutil.rmtree(job)
            return final

        if cfg["weknora"]["enabled"]:
            already_verified = bool(row and row["state"] == "verified" and doc_id)
            if not doc_id:
                doc_id = weknora_find_existing(
                    final, source, cfg["weknora"]
                ) or weknora_upload(final, source, cfg["weknora"])
                save_state(
                    db,
                    digest,
                    source,
                    "indexing",
                    encode_batch_ids(batch_ids),
                    str(final),
                    weknora_doc_id=doc_id,
                    metrics=metrics,
                )
            if not already_verified:
                index_started = time.perf_counter()
                weknora_verify(final, source, cfg["weknora"], doc_id)
                metrics["weknora_index_seconds"] = round(
                    metrics["weknora_index_seconds"]
                    + time.perf_counter()
                    - index_started,
                    3,
                )
                save_state(
                    db,
                    digest,
                    source,
                    "verified",
                    encode_batch_ids(batch_ids),
                    str(final),
                    weknora_doc_id=doc_id,
                    metrics=metrics,
                )
        else:
            raise RuntimeError("WeKnora尚未启用；为防误删，源文件保留")
        if cfg["cleanup"]["delete_temporary_files"] and job.exists():
            shutil.rmtree(job)
        if cfg["cleanup"]["permanently_delete_source_after_search"]:
            delete_source_with_audit(
                db,
                source,
                digest,
                final,
                "单文件索引与检索确认通过",
            )
        metrics["finished_at"] = int(time.time())
        metrics["total_seconds"] = round(
            metrics["finished_at"] - metrics["started_at"], 3
        )
        save_state(
            db,
            digest,
            source,
            "completed",
            encode_batch_ids(batch_ids),
            str(final),
            weknora_doc_id=doc_id,
            metrics=metrics,
        )
        return final
    except MinerURetryLater as exc:
        save_state(
            db,
            digest,
            source,
            "parsing" if batch_ids else "queued",
            encode_batch_ids(batch_ids) if batch_ids else None,
            str(final) if final.is_file() else None,
            error=str(exc),
            weknora_doc_id=doc_id,
            metrics=metrics,
        )
        if cfg["cleanup"]["delete_temporary_files"] and job.exists():
            try:
                shutil.rmtree(job)
            except OSError as cleanup_error:
                print(f"临时分卷将在下轮处理前清理: {cleanup_error}")
        print(f"云端任务尚未完成，已让出本地工作线程: {source.name}: {exc}")
        return None
    except Exception as exc:
        metrics["finished_at"] = int(time.time())
        metrics["total_seconds"] = round(
            metrics["finished_at"] - metrics["started_at"], 3
        )
        failed = source
        if (
            not defer_index
            and source.exists()
            and source.parent != cfg["folders"]["failed"]
        ):
            failed = unique_failed_path(source, digest, cfg["folders"]["failed"])
            shutil.move(str(source), str(failed))
        else:
            failed = source
        save_state(
            db,
            digest,
            failed,
            "failed",
            encode_batch_ids(batch_ids) if batch_ids else None,
            str(final) if final.is_file() else None,
            error=str(exc),
            weknora_doc_id=doc_id,
            metrics=metrics,
        )
        if cfg["cleanup"]["delete_temporary_files"] and job.exists():
            try:
                shutil.rmtree(job)
            except OSError as cleanup_error:
                print(f"临时目录未能删除，将在下次重试时清理: {cleanup_error}")
        location = "inbox等待整组处理" if defer_index else str(failed)
        print(f"失败并保留源文件: {source.name}｜{location}: {exc}")
    finally:
        metrics["active_processing_seconds"] = round(
            float(metrics.get("active_processing_seconds") or 0)
            + time.perf_counter()
            - active_attempt_started,
            3,
        )
        try:
            db.execute(
                "UPDATE files SET metrics_json=? WHERE sha256=?",
                (
                    json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                    digest,
                ),
            )
            db.commit()
        except sqlite3.Error:
            pass


def cleanup_verified_sources(
    sources: list[Path],
    canonical: Path,
    parent_doc_id: str,
    group_id: str,
    cfg: dict,
    db: sqlite3.Connection,
) -> list[str]:
    errors = []
    delete_enabled = bool(
        cfg["cleanup"].get("permanently_delete_source_after_search", False)
    )
    for source in sources:
        manifest_row = db.execute(
            """SELECT sha256 FROM group_files
            WHERE group_id=? AND source_path=?""",
            (group_id, str(source)),
        ).fetchone()
        if not manifest_row:
            errors.append(f"{source.name}: 缺少资料组摘要登记，拒绝删除")
            continue
        expected_digest = manifest_row["sha256"]
        if not source.exists():
            current = db.execute(
                "SELECT * FROM files WHERE sha256=?",
                (expected_digest,),
            ).fetchone()
            metrics = file_metrics(current)
            if not metrics["finished_at"]:
                metrics["finished_at"] = int(time.time())
            save_state(
                db,
                expected_digest,
                source,
                "completed",
                current["batch_id"] if current else None,
                str(canonical),
                error="源文件历史缺失；已由保留的解析结果完成三层索引",
                weknora_doc_id=parent_doc_id,
                metrics=metrics,
            )
            continue
        try:
            digest = stable_sha256(source)
        except (OSError, RuntimeError) as exc:
            errors.append(f"{source.name}: 删除前摘要无法稳定确认: {exc}")
            continue
        if digest != expected_digest:
            save_state(
                db,
                expected_digest,
                source,
                "completed",
                md_path=str(canonical),
                error="源文件内容已变化；旧内容索引保留，新内容未删除",
                weknora_doc_id=parent_doc_id,
            )
            existing_new = db.execute(
                "SELECT source_path FROM files WHERE sha256=?", (digest,)
            ).fetchone()
            if existing_new is None or str(existing_new["source_path"] or "") == str(
                source
            ):
                save_state(
                    db,
                    digest,
                    source,
                    "queued",
                    error="检测到同路径新内容，等待作为新资料处理",
                )
            else:
                print(
                    "检测到同内容的另一来源路径；当前副本保留，"
                    f"等待重复内容人工确认: {source}"
                )
            print(
                f"源文件内容在处理期间发生变化，保留新文件等待下次处理: "
                f"{source.name}"
            )
            continue
        if delete_enabled:
            try:
                delete_source_with_audit(
                    db,
                    source,
                    expected_digest,
                    canonical,
                    "三层索引与混合检索确认通过",
                    group_id,
                )
            except (OSError, RuntimeError) as exc:
                errors.append(f"{source.name}: {exc}")
        # A retained source is the requested terminal state when deletion is
        # disabled; it must not become an endless cleanup retry candidate.
        state = (
            "cleanup_pending"
            if delete_enabled and source.exists()
            else "completed"
        )
        metric_row = db.execute(
            "SELECT metrics_json FROM files WHERE sha256=?", (digest,)
        ).fetchone()
        metrics = file_metrics(metric_row)
        if not metrics["finished_at"]:
            metrics["finished_at"] = int(time.time())
        if metrics["started_at"]:
            metrics["total_seconds"] = round(
                metrics["finished_at"] - metrics["started_at"], 3
            )
        save_state(
            db,
            digest,
            source,
            state,
            md_path=str(canonical),
            error="" if state == "completed" else "索引已验证，等待重试永久删除",
            weknora_doc_id=parent_doc_id,
            metrics=metrics,
        )
    return errors


def is_retryable_parse_error(error: str) -> bool:
    """网络抖动和旧版小模型格式错误不应把源文件移出自动队列。"""
    retryable_markers = (
        "Read timed out",
        "ConnectTimeout",
        "ConnectionError",
        "ConnectionAbortedError",
        "ConnectionResetError",
        "HTTPSConnectionPool",
        "HTTPConnectionPool",
        "Max retries exceeded",
        "SSLEOFError",
        "UNEXPECTED_EOF_WHILE_READING",
        "RemoteDisconnected",
        "IncompleteRead",
        "Connection broken",
        "ChunkedEncodingError",
        "ProtocolError",
        "Connection reset",
        "WinError 5",
        "Access is denied",
        "PermissionError",
        "sharing violation",
        "used by another process",
        "MinerU提交连接中断",
        "MinerU云端尚未完成",
        "MinerU等待文件上传",
        "等待云端解析",
        "waiting-file",
        "轮询暂时限流",
        "Temporary failure",
        "Ollama临时失败",
        "本地模型连续两次没有返回合法JSON",
        "408 Request Timeout",
        "425 Too Early",
        "429 Too Many Requests",
        "500 Server Error",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "504 Gateway Timeout",
        "MiMo全部已配置Key暂不可用",
    )
    return any(marker.casefold() in error.casefold() for marker in retryable_markers)


def is_permanent_source_parse_error(error: str) -> bool:
    """Errors proving that the source itself cannot enter the parser."""
    permanent_markers = (
        "File has not been decrypted",
        "Stack overflow",
        "Invalid object in /Pages",
        "Stream has ended unexpectedly",
        "invalid pdf header",
        "EOF marker not found",
        "PDF单页超过MinerU",
        "非PDF文件超过MinerU",
        "直接文本文件超过本地处理上限",
        "model_version 'vlm' cannot process",
    )
    return any(marker.casefold() in error.casefold() for marker in permanent_markers)


def finalize_group_indexing(task: GroupIndexTask, cfg: dict) -> None:
    """Wait for both indexes without occupying a parsing/merge worker."""
    db = db_open()
    verified = False
    current = db.execute(
        "SELECT error FROM groups WHERE group_id=?",
        (task.group_id,),
    ).fetchone()
    force_full_check = bool(
        current
        and "完整向量/BM25抽查失败" in (current["error"] or "")
    )
    full_check = force_full_check or full_route_check_due(cfg["weknora"])
    try:
        verify_two_level_indexes(
            task.parent_path,
            task.child_path,
            task.sources[0],
            cfg["weknora"],
            task.parent_doc_id,
            task.child_doc_id,
            classification=task.classification,
            full_check=full_check,
            raw_path=task.raw_path,
            raw_doc_id=task.raw_doc_id,
        )
        verified = True
        save_group_state(
            db,
            task.group_id,
            task.group_name,
            "verified",
            task.parent_path,
            task.parent_doc_id,
            task.child_doc_id,
            classification=task.classification,
            raw_path=task.raw_path,
            raw_doc_id=task.raw_doc_id,
        )
        cleanup_errors = cleanup_verified_sources(
            list(task.sources),
            task.parent_path,
            task.parent_doc_id,
            task.group_id,
            cfg,
            db,
        )
        for parsed_path in task.parsed_paths:
            if parsed_path == task.parent_path:
                continue
            try:
                parsed_path.unlink(missing_ok=True)
                parsed_path.with_suffix(".json").unlink(missing_ok=True)
            except OSError as cleanup_error:
                print(f"独立解析临时文件将在下次清理: {cleanup_error}")
        if cfg["cleanup"]["delete_temporary_files"]:
            try:
                task.child_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                print(f"子块临时文件将在下次清理: {cleanup_error}")
        final_state = "cleanup_pending" if cleanup_errors else "completed"
        save_group_state(
            db,
            task.group_id,
            task.group_name,
            final_state,
            task.parent_path,
            task.parent_doc_id,
            task.child_doc_id,
            "; ".join(cleanup_errors),
            classification=task.classification,
            raw_path=task.raw_path,
            raw_doc_id=task.raw_doc_id,
        )
        if cleanup_errors:
            print(
                "父块和子块索引保持有效，源文件等待下次重试删除: "
                + "; ".join(cleanup_errors)
            )
        else:
            verification = (
                "完整向量/BM25抽查"
                if full_check
                else "三层混合检索"
            )
            print(
                f"{verification}及原文层均已确认，源文件已按规则清理: "
                f"{task.group_name}"
            )
    except Exception as exc:
        state = "cleanup_pending" if verified else "indexing"
        if full_check and not verified:
            error = f"完整向量/BM25抽查失败，必须完整重试: {exc}"
        else:
            error = (
                f"索引已验证，清理状态等待重试: {exc}"
                if verified
                else f"索引或检索等待重试: {exc}"
            )
        try:
            save_group_state(
                db,
                task.group_id,
                task.group_name,
                state,
                task.parent_path,
                task.parent_doc_id,
                task.child_doc_id,
                error,
                classification=task.classification,
                raw_path=task.raw_path,
                raw_doc_id=task.raw_doc_id,
            )
        except sqlite3.Error as state_error:
            print(f"索引流水线状态记录等待重试: {state_error}")
        print(
            f"父块、子块和原文层尚未满足删除条件，源文件与Markdown均保留: "
            f"{task.group_name}: {exc}"
        )
    finally:
        db.close()


def process_group(
    group_id: str,
    group_name: str,
    sources: list[Path],
    cfg: dict,
    db: sqlite3.Connection,
    mineru_preferred_slot: str | None = None,
    submit_index_task=None,
    index_gate: ElasticConcurrencyGate | None = None,
    preparsed: list[tuple[Path, Path]] | None = None,
) -> None:
    group_row = db.execute(
        "SELECT * FROM groups WHERE group_id=?", (group_id,)
    ).fetchone()
    exclusion_state = (
        str(group_row["state"] or "")
        if group_row
        and str(group_row["state"] or "")
        in PERMANENT_GROUP_EXCLUSION_STATES
        else ""
    )
    if exclusion_state:
        print(
            f"资料组已永久排除，跳过且不重新生成: "
            f"{group_name} ({group_id}, {exclusion_state})"
        )
        return
    raw_required = bool(
        (cfg.get("weknora") or {}).get("raw_knowledge_base")
    )
    if (
        group_row
        and group_row["state"] in {
            "completed", "cleanup_pending", "excluded_cleanup_pending"
        }
        and group_row["markdown_path"]
        and group_row["parent_doc_id"]
        and group_row["child_doc_id"]
        and (
            not raw_required
            or (
                group_row["raw_path"]
                and group_row["raw_doc_id"]
                and Path(group_row["raw_path"]).is_file()
            )
        )
    ):
        canonical = Path(group_row["markdown_path"])
        if canonical.is_file():
            verify_two_level_indexes(
                canonical,
                canonical,
                sources[0],
                cfg["weknora"],
                group_row["parent_doc_id"],
                group_row["child_doc_id"],
                full_check=False,
                raw_path=(
                    Path(group_row["raw_path"])
                    if group_row["raw_path"]
                    else None
                ),
                raw_doc_id=str(group_row["raw_doc_id"] or ""),
            )
            cleanup_errors = cleanup_verified_sources(
                sources,
                canonical,
                group_row["parent_doc_id"],
                group_id,
                cfg,
                db,
            )
            state = "cleanup_pending" if cleanup_errors else "completed"
            save_group_state(
                db,
                group_row["group_id"],
                group_row["group_name"],
                state,
                canonical,
                group_row["parent_doc_id"],
                group_row["child_doc_id"],
                "; ".join(cleanup_errors),
                raw_path=(
                    Path(group_row["raw_path"])
                    if group_row["raw_path"]
                    else None
                ),
                raw_doc_id=group_row["raw_doc_id"],
            )
            if cleanup_errors:
                print(
                    f"索引保持有效，以下源文件等待下次重试删除: "
                    f"{'; '.join(cleanup_errors)}"
                )
            else:
                print(f"重复资料组已确认三层索引并按规则清理: {group_name}")
            return
    if (
        group_row
        and group_row["state"] == "excluded_completed"
        and group_row["markdown_path"]
        and Path(group_row["markdown_path"]).is_file()
    ):
        return
    if (
        group_row
        and group_row["state"] == "classification_pending"
        and group_row["markdown_path"]
        and Path(group_row["markdown_path"]).is_file()
    ):
        try:
            cached = classification_from_dict(
                json.loads(group_row["classification_json"] or "{}")
            )
            current_version = int(
                cfg["document_classification"]["taxonomy"].get("version")
                or cfg["document_classification"].get("version")
                or 1
            )
            if cached.version == current_version:
                return
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    parsed: list[tuple[Path, Path]] = list(preparsed or [])
    for source in ([] if preparsed is not None else sources):
        if not source.exists():
            continue
        parsed_path = process(
            source,
            cfg,
            db,
            defer_index=True,
            mineru_preferred_slot=mineru_preferred_slot,
        )
        if parsed_path is None or not parsed_path.is_file():
            source_digest = sha256(source)
            failed_row = db.execute(
                "SELECT * FROM files WHERE sha256=?", (source_digest,)
            ).fetchone()
            parse_error = failed_row["error"] if failed_row else "解析未返回Markdown"
            if (
                is_retryable_parse_error(parse_error)
                or not is_permanent_source_parse_error(parse_error)
            ):
                retry_state = (
                    "parsing"
                    if failed_row and decode_batch_ids(failed_row["batch_id"])
                    else "queued"
                )
                save_state(
                    db,
                    source_digest,
                    source,
                    retry_state,
                    failed_row["batch_id"] if failed_row else None,
                    failed_row["markdown_path"] if failed_row else None,
                    error=f"临时错误，等待自动重试: {parse_error}",
                    metrics=file_metrics(failed_row),
                )
                save_group_state(
                    db,
                    group_id,
                    group_name,
                    "retry_wait",
                    error=f"{source.name}等待自动重试: {parse_error}",
                )
                print(
                    f"临时错误，源文件保留在inbox等待自动重试: "
                    f"{source.name}: {parse_error}"
                )
                return
            if cfg["cleanup"]["delete_temporary_files"]:
                pass
            # 单个文件的永久解析失败只隔离该文件。已经成功解析的同组
            # 文件及其 Markdown 保持可恢复，不能因为一个成员失败而整组丢弃。
            failed_source = source
            if source.exists() and source.parent != cfg["folders"]["failed"]:
                destination = unique_failed_path(
                    source, source_digest, cfg["folders"]["failed"]
                )
                shutil.move(str(source), str(destination))
                failed_source = destination
            save_state(
                db,
                source_digest,
                failed_source,
                "failed",
                failed_row["batch_id"] if failed_row else None,
                failed_row["markdown_path"] if failed_row else None,
                error=parse_error,
                metrics=file_metrics(failed_row),
            )
            save_group_state(
                db, group_id, group_name, "failed", error=f"{source.name}解析失败"
            )
            print(
                f"单个文件解析失败并隔离，其他同组文件保留继续处理: "
                f"{source.name}"
            )
            return
        parsed.append((source, parsed_path))
    if not parsed:
        return

    group_row = db.execute(
        "SELECT * FROM groups WHERE group_id=?", (group_id,)
    ).fetchone()
    parent_doc_id = group_row["parent_doc_id"] if group_row else None
    child_doc_id = group_row["child_doc_id"] if group_row else None
    raw_doc_id = group_row["raw_doc_id"] if group_row else None
    parent_path: Path | None = None
    child_path: Path | None = None
    raw_path: Path | None = (
        Path(group_row["raw_path"])
        if group_row and group_row["raw_path"]
        else None
    )
    indexes_verified = False
    index_slot_acquired = False
    classification: DocumentClassification | None = None
    try:
        classification = classify_group(
            group_name,
            parsed,
            cfg,
            group_row["classification_json"] if group_row else None,
        )
        (
            parent_path,
            child_path,
            raw_path,
            unit_count,
            unmatched_answers,
            document_mode,
        ) = group_documents(
            group_id,
            group_name,
            parsed,
            cfg["folders"]["markdown"],
            cfg["folders"]["work"],
            int(cfg["pairing"]["child_chars"]),
            classification,
        )
        print(
            f"先解析后合并完成：{group_name}｜"
            f"{'章节' if document_mode == 'section' else '题目'}{unit_count}｜"
            f"未匹配答案{unmatched_answers}｜"
            f"{classification_line(classification)}"
        )
        save_group_state(
            db,
            group_id,
            group_name,
            "classified",
            parent_path,
            parent_doc_id,
            child_doc_id,
            classification=classification,
            raw_path=raw_path,
            raw_doc_id=raw_doc_id,
        )
        if classification.document_type == "待分类":
            for source, parsed_path in parsed:
                digest = recorded_source_digest(db, source, group_id)
                if not digest:
                    raise RuntimeError(
                        f"缺少源文件摘要登记，无法安全保存待分类状态: {source}"
                    )
                current = db.execute(
                    "SELECT * FROM files WHERE sha256=?", (digest,)
                ).fetchone()
                save_state(
                    db,
                    digest,
                    source,
                    "classification_pending",
                    current["batch_id"] if current else None,
                    str(parent_path),
                    error="分类证据不足，源文件保留",
                    metrics=file_metrics(current),
                )
                if parsed_path != parent_path:
                    parsed_path.unlink(missing_ok=True)
                    parsed_path.with_suffix(".json").unlink(missing_ok=True)
            if child_path and cfg["cleanup"]["delete_temporary_files"]:
                child_path.unlink(missing_ok=True)
            save_group_state(
                db,
                group_id,
                group_name,
                "classification_pending",
                parent_path,
                error="分类证据不足，等待规则或别名表更新",
                classification=classification,
                raw_path=raw_path,
            )
            print(f"分类实施：待分类并保留源文件｜{group_name}")
            return
        if classification.document_type == "其他资料":
            if not markdown_body(parent_path).strip():
                raise RuntimeError("其他资料Markdown为空，拒绝删除源文件")
            markdown_digest = stable_sha256(parent_path)
            cleanup_errors = []
            for source, parsed_path in parsed:
                digest = recorded_source_digest(db, source, group_id)
                if not digest:
                    raise RuntimeError(
                        f"缺少源文件摘要登记，拒绝完成其他资料清理: {source}"
                    )
                current = db.execute(
                    "SELECT * FROM files WHERE sha256=?", (digest,)
                ).fetchone()
                try:
                    delete_other_source = bool(
                        cfg["document_classification"].get(
                        "delete_other_source_after_markdown", False
                        )
                    )
                    if delete_other_source and source.exists():
                        delete_source_with_audit(
                            db,
                            source,
                            digest,
                            parent_path,
                            "高置信度其他资料已保存Markdown且不入RAG",
                            group_id,
                        )
                    state = "excluded_completed"
                    error = f"未入RAG；markdown_sha256={markdown_digest}"
                except OSError as cleanup_error:
                    state = "excluded_cleanup_pending"
                    error = f"其他资料源文件等待重试删除: {cleanup_error}"
                    cleanup_errors.append(f"{source.name}: {cleanup_error}")
                save_state(
                    db,
                    digest,
                    source,
                    state,
                    current["batch_id"] if current else None,
                    str(parent_path),
                    error=error,
                    metrics=file_metrics(current),
                )
                if parsed_path != parent_path:
                    parsed_path.unlink(missing_ok=True)
                    parsed_path.with_suffix(".json").unlink(missing_ok=True)
            if child_path and cfg["cleanup"]["delete_temporary_files"]:
                child_path.unlink(missing_ok=True)
            if raw_path and raw_path != parent_path:
                raw_path.unlink(missing_ok=True)
            classification_record = classification_to_dict(classification)
            classification_record["markdown_sha256"] = markdown_digest
            save_group_state(
                db,
                group_id,
                group_name,
                "excluded_cleanup_pending"
                if cleanup_errors
                else "excluded_completed",
                parent_path,
                error="; ".join(cleanup_errors),
                classification=classification_record,
            )
            print(
                f"分类实施：其他资料已保存Markdown且未入RAG｜{group_name}｜"
                + (
                    "源文件等待重试删除"
                    if cleanup_errors
                    else (
                        "源文件已永久删除"
                        if delete_other_source
                        else "源文件按配置保留"
                    )
                )
            )
            return
        wc = cfg["weknora"]
        resuming_existing_index = bool(
            parent_doc_id or child_doc_id or raw_doc_id
        )
        if not resuming_existing_index:
            resources = cfg.get("resource_control") or {}
            hard_pause_gb = max(
                0.5,
                float(resources.get("hard_pause_windows_free_gb", 0.65)),
            )
            memory_wait_reported = False
            while True:
                _, available_gb = windows_memory_gb()
                if available_gb >= hard_pause_gb:
                    break
                if not memory_wait_reported:
                    print(
                        f"WeKnora上传前等待：可用内存{available_gb:.2f}GB｜"
                        "Markdown和源文件保持不动"
                    )
                    memory_wait_reported = True
                time.sleep(5)
        if index_gate is not None:
            while True:
                _, active_groups, waiting_groups = index_gate.snapshot()
                index_gate.set_limit(
                    adaptive_weknora_index_count(
                        cfg, active_groups + waiting_groups + 1
                    ),
                    "上传前按内存和队列压力调整",
                    float(
                        (cfg.get("resource_control") or {}).get(
                            "weknora_concurrency_increase_hold_seconds", 30
                        )
                    ),
                )
                if index_gate.acquire(timeout=1):
                    index_slot_acquired = True
                    break
        parent_kb = wc["parent_knowledge_base"]
        child_kb = wc["child_knowledge_base"]
        raw_kb = wc.get("raw_knowledge_base")
        if not raw_kb:
            raise RuntimeError("WeKnora原文知识库尚未配置，拒绝删除源文件")
        parent_doc_id = repair_storage_quota_failed_document(
            parent_doc_id,
            parent_path,
            parsed[0][0],
            wc,
            parent_kb,
            "parent",
            group_id,
            classification,
        )
        child_doc_id = repair_storage_quota_failed_document(
            child_doc_id,
            child_path,
            parsed[0][0],
            wc,
            child_kb,
            "child",
            group_id,
            classification,
        )
        raw_doc_id = repair_storage_quota_failed_document(
            raw_doc_id,
            raw_path,
            parsed[0][0],
            wc,
            raw_kb,
            "raw",
            group_id,
            classification,
        )
        if not parent_doc_id:
            parent_doc_id = weknora_find_existing(
                parent_path, parsed[0][0], wc, parent_kb
            ) or weknora_upload(
                parent_path,
                parsed[0][0],
                wc,
                parent_kb,
                "parent",
                group_id,
                classification,
            )
        save_group_state(
            db,
            group_id,
            group_name,
            "indexing",
            parent_path,
            parent_doc_id,
            child_doc_id,
            classification=classification,
            raw_path=raw_path,
            raw_doc_id=raw_doc_id,
        )
        if not child_doc_id:
            child_doc_id = weknora_find_existing(
                child_path, parsed[0][0], wc, child_kb
            ) or weknora_upload(
                child_path,
                parsed[0][0],
                wc,
                child_kb,
                "child",
                group_id,
                classification,
            )
        save_group_state(
            db,
            group_id,
            group_name,
            "indexing",
            parent_path,
            parent_doc_id,
            child_doc_id,
            classification=classification,
            raw_path=raw_path,
            raw_doc_id=raw_doc_id,
        )
        if not raw_doc_id:
            raw_doc_id = weknora_find_existing(
                raw_path, parsed[0][0], wc, raw_kb
            ) or weknora_upload(
                raw_path,
                parsed[0][0],
                wc,
                raw_kb,
                "raw",
                group_id,
                classification,
            )
        save_group_state(
            db,
            group_id,
            group_name,
            "indexing",
            parent_path,
            parent_doc_id,
            child_doc_id,
            classification=classification,
            raw_path=raw_path,
            raw_doc_id=raw_doc_id,
        )
        index_task = GroupIndexTask(
            group_id=group_id,
            group_name=group_name,
            sources=tuple(source for source, _ in parsed),
            parsed_paths=tuple(path for _, path in parsed),
            parent_path=parent_path,
            child_path=child_path,
            raw_path=raw_path,
            parent_doc_id=parent_doc_id,
            child_doc_id=child_doc_id,
            raw_doc_id=raw_doc_id,
            classification=classification,
        )
        if submit_index_task is not None:
            submit_index_task(index_task)
            index_slot_acquired = False
            print(
                f"父块、子块和原文已上传，转入独立索引等待队列: {group_name}"
            )
        else:
            finalize_group_indexing(index_task, cfg)
        return
    except Exception as exc:
        no_question_body = (
            "没有识别到可入库的题目正文" in str(exc)
            and not parent_doc_id
            and not child_doc_id
            and not raw_doc_id
        )
        if no_question_body:
            first_markdown: Path | None = None
            for source, parsed_path in parsed:
                first_markdown = first_markdown or parsed_path
                digest = recorded_source_digest(db, source, group_id)
                if not digest:
                    continue
                current = db.execute(
                    "SELECT * FROM files WHERE sha256=?", (digest,)
                ).fetchone()
                save_state(
                    db,
                    digest,
                    source,
                    "classification_pending",
                    current["batch_id"] if current else None,
                    str(parsed_path),
                    error=str(exc),
                    weknora_doc_id=(
                        current["weknora_doc_id"] if current else None
                    ),
                    metrics=file_metrics(current),
                )
            save_group_state(
                db,
                group_id,
                group_name,
                "classification_pending",
                first_markdown,
                error=str(exc),
                classification=classification,
            )
            print(
                "解析结果没有可安全识别的题目正文，已进入待分类并保留源文件，"
                f"不再自动重复处理: {group_name}"
            )
            return
        if indexes_verified:
            try:
                save_group_state(
                    db,
                    group_id,
                    group_name,
                    "cleanup_pending",
                    parent_path,
                    parent_doc_id,
                    child_doc_id,
                    f"索引已验证，清理状态等待重试: {exc}",
                    classification=classification,
                    raw_path=raw_path,
                    raw_doc_id=raw_doc_id,
                )
            except sqlite3.Error as state_error:
                print(f"索引已验证，但状态记录等待重试: {state_error}")
            print(
                f"父块、子块和原文索引已验证，未回滚Markdown或移动源文件；"
                f"下次只重试清理: {group_name}: {exc}"
            )
            return
        if parent_doc_id or child_doc_id or raw_doc_id:
            try:
                save_group_state(
                    db,
                    group_id,
                    group_name,
                    "indexing",
                    parent_path,
                    parent_doc_id,
                    child_doc_id,
                    f"部分入库已保留，等待断点续传: {exc}",
                    classification=classification,
                    raw_path=raw_path,
                    raw_doc_id=raw_doc_id,
                )
            except sqlite3.Error as state_error:
                print(f"部分入库状态记录等待重试: {state_error}")
            print(
                f"资料组已有WeKnora文档，保留源文件、Markdown和文档ID，"
                f"下次从断点续传: {group_name}: {exc}"
            )
            return
        for source, parsed_path in parsed:
            digest = recorded_source_digest(db, source, group_id)
            if digest:
                current = db.execute(
                    "SELECT * FROM files WHERE sha256=?", (digest,)
                ).fetchone()
                save_state(
                    db,
                    digest,
                    source,
                    "parsed",
                    current["batch_id"] if current else None,
                    str(parsed_path),
                    error=f"资料组合并或入库等待重试: {exc}",
                    weknora_doc_id=(
                        current["weknora_doc_id"] if current else None
                    ),
                    metrics=file_metrics(current),
                )
        save_group_state(
            db,
            group_id,
            group_name,
            "retry_wait",
            parent_path,
            parent_doc_id,
            child_doc_id,
            str(exc),
            classification=classification,
            raw_path=raw_path,
            raw_doc_id=raw_doc_id,
        )
        if (
            child_path
            and cfg["cleanup"]["delete_temporary_files"]
            and child_path.exists()
        ):
            child_path.unlink(missing_ok=True)
        print(
            "资料组暂未完成，源文件、解析结果和父块保留等待重试: "
            f"{group_name}: {exc}"
        )
    finally:
        if index_slot_acquired and index_gate is not None:
            index_gate.release()


def relink_missing_sources_from_failed(
    db: sqlite3.Connection,
    cfg: dict,
) -> int:
    """Move digest-matched retained files back to inbox without copying them."""
    missing_rows = [
        row
        for row in db.execute(
            """SELECT sha256,source_path,state FROM files
            WHERE state NOT IN (
                'completed','excluded_completed','failed',
                'user_delete_pending','user_deleted'
            )"""
        ).fetchall()
        if not Path(row["source_path"]).exists()
    ]
    prefixes = {str(row["sha256"])[:10] for row in missing_rows}
    candidates: dict[str, list[Path]] = {}
    for path in cfg["folders"]["failed"].rglob("*"):
        if not path.is_file() or path.name.startswith("._"):
            continue
        for prefix in prefixes:
            if f"-{prefix}" in path.name.casefold():
                candidates.setdefault(prefix, []).append(path)
    restored = 0
    restore_root = cfg["folders"]["inbox"] / "_recovered"
    for row in missing_rows:
        digest = str(row["sha256"])
        matched = None
        for candidate in candidates.get(digest[:10], []):
            try:
                if stable_sha256(candidate) == digest:
                    matched = candidate
                    break
            except (OSError, RuntimeError):
                continue
        if matched is None:
            continue
        restore_root.mkdir(parents=True, exist_ok=True)
        target = restore_root / matched.name
        counter = 2
        while target.exists():
            target = restore_root / (
                f"{matched.stem}-{counter}{matched.suffix}"
            )
            counter += 1
        shutil.move(str(matched), str(target))
        old_path = str(row["source_path"])
        db.execute(
            "UPDATE files SET source_path=?,updated_at=? WHERE sha256=?",
            (str(target), int(time.time()), digest),
        )
        memberships = db.execute(
            """SELECT group_id FROM group_files
            WHERE sha256=? AND source_path=?""",
            (digest, old_path),
        ).fetchall()
        for membership in memberships:
            db.execute(
                """INSERT OR IGNORE INTO group_files(group_id,sha256,source_path)
                VALUES(?,?,?)""",
                (membership["group_id"], digest, str(target)),
            )
        db.execute(
            "DELETE FROM group_files WHERE sha256=? AND source_path=?",
            (digest, old_path),
        )
        db.commit()
        restored += 1
        print(f"历史源文件已按SHA-256重新关联并移回inbox: {target.name}")
    return restored


def recovered_markdown_path(
    row: sqlite3.Row,
    cfg: dict,
) -> Path:
    markdown_root = cfg["folders"]["markdown"].resolve()
    if row["markdown_path"]:
        candidate = Path(row["markdown_path"]).resolve()
        if candidate == markdown_root or markdown_root in candidate.parents:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return candidate
    source = Path(row["source_path"])
    recovery_root = markdown_root / "待分类" / "历史缺源恢复"
    recovery_root.mkdir(parents=True, exist_ok=True)
    return recovery_root / (
        f"{safe_path_component(source.stem, '历史资料')}-{row['sha256'][:10]}.md"
    )


def recover_missing_source_batches(
    cfg: dict,
    limit: int = 0,
) -> dict[str, int]:
    """Recover MinerU text/assets by batch ID without resubmitting or charging again."""
    db = db_open()
    tokens = mineru_tokens()
    stats = {"restored": 0, "recovered": 0, "waiting": 0, "unrecoverable": 0}
    try:
        stats["restored"] = relink_missing_sources_from_failed(db, cfg)
        rows = [
            row
            for row in db.execute(
                """SELECT * FROM files
                WHERE state IN (
                    'parsing','parsed','source_missing',
                    'source_missing_recovered'
                )
                ORDER BY updated_at"""
            ).fetchall()
            if not Path(row["source_path"]).exists()
            and decode_batch_ids(row["batch_id"])
            and (
                row["state"] != "source_missing"
                or is_retryable_parse_error(str(row["error"] or ""))
            )
            and not (
                row["markdown_path"]
                and Path(row["markdown_path"]).is_file()
            )
        ]
        if limit > 0:
            rows = rows[:limit]
        for index, row in enumerate(rows, 1):
            source = Path(row["source_path"])
            digest = str(row["sha256"])
            batches = decode_batch_ids(row["batch_id"])
            metrics = file_metrics(row)
            job = cfg["folders"]["work"] / f"recover-{digest[:16]}"
            try:
                clean_job(job, cfg["folders"]["work"])
                items = mineru_wait(batches, tokens, cfg["mineru"])
                metrics["mineru_cloud_done_at"] = int(time.time())
                download_started = time.perf_counter()
                parsed = download_recovered_results(items, source, job)
                metrics["mineru_download_seconds"] = round(
                    float(metrics.get("mineru_download_seconds") or 0)
                    + time.perf_counter()
                    - download_started,
                    3,
                )
                sections = []
                for part, markdown in parsed:
                    sections.append(
                        repair_images(
                            markdown.read_text("utf-8"),
                            markdown,
                            part.path,
                            job,
                            cfg["ollama"],
                            metrics,
                        )
                    )
                final = recovered_markdown_path(row, cfg)
                metadata = (
                    "---\n"
                    f"source_file: {json.dumps(source.name, ensure_ascii=False)}\n"
                    f"source_sha256: {digest}\n"
                    "parser: mineru-v4-source-missing-recovery\n"
                    "source_missing: true\n---\n\n"
                )
                atomic_write(final, metadata + "\n\n".join(sections))
                save_state(
                    db,
                    digest,
                    source,
                    "source_missing_recovered",
                    row["batch_id"],
                    str(final),
                    error="源文件历史缺失；已从原MinerU批次恢复Markdown",
                    weknora_doc_id=row["weknora_doc_id"],
                    metrics=metrics,
                )
                stats["recovered"] += 1
                print(
                    f"缺源MinerU恢复：{index}/{len(rows)}｜"
                    f"未重新提交｜{source.name}"
                )
            except MinerURetryLater as exc:
                save_state(
                    db,
                    digest,
                    source,
                    "parsing",
                    row["batch_id"],
                    row["markdown_path"],
                    error=f"源文件历史缺失，等待原MinerU批次完成: {exc}",
                    weknora_doc_id=row["weknora_doc_id"],
                    metrics=metrics,
                )
                stats["waiting"] += 1
            except Exception as exc:
                retryable = is_retryable_parse_error(str(exc))
                save_state(
                    db,
                    digest,
                    source,
                    "parsing" if retryable else "source_missing",
                    row["batch_id"],
                    row["markdown_path"],
                    error=(
                        f"源文件历史缺失，下载连接中断等待自动恢复: {exc}"
                        if retryable
                        else f"源文件缺失且原MinerU结果无法恢复: {exc}"
                    ),
                    weknora_doc_id=row["weknora_doc_id"],
                    metrics=metrics,
                )
                if retryable:
                    stats["waiting"] += 1
                    print(f"缺源任务网络中断，保留队列等待重试: {source.name}")
                else:
                    stats["unrecoverable"] += 1
                    print(f"缺源任务恢复失败并停止空转: {source.name}: {exc}")
            finally:
                if cfg["cleanup"]["delete_temporary_files"] and job.exists():
                    shutil.rmtree(job, ignore_errors=True)
        return stats
    finally:
        db.close()


def recover_missing_sources_in_background(
    cfg: dict,
    limit: int = 0,
) -> dict[str, int]:
    """Run the idempotent recovery lane without allowing a second recovery process."""
    with single_instance(".source-recovery.lock"):
        return recover_missing_source_batches(cfg, limit)


def process_recovered_groups(cfg: dict, limit: int = 0) -> dict[str, int]:
    """Merge and index groups whose only remaining input is recovered Markdown."""
    db = db_open()
    stats = {"completed": 0, "resumed_indexing": 0, "skipped": 0}
    try:
        resume_after = max(
            0,
            int(
                (cfg.get("resource_control") or {}).get(
                    "indexing_resume_after_seconds", 3600
                )
            ),
        )
        stale_rows = db.execute(
            """SELECT * FROM groups
            WHERE state='indexing' AND updated_at<?
            AND markdown_path IS NOT NULL
            AND trim(coalesce(parent_doc_id,''))<>''
            AND trim(coalesce(child_doc_id,''))<>''
            ORDER BY updated_at""",
            (int(time.time()) - resume_after,),
        ).fetchall()
        for group in stale_rows:
            parent_path = Path(group["markdown_path"])
            if not parent_path.is_file():
                continue
            try:
                classification = classification_from_dict(
                    json.loads(group["classification_json"] or "{}")
                )
                sources = tuple(
                    Path(row["source_path"])
                    for row in db.execute(
                        """SELECT source_path FROM group_files
                        WHERE group_id=? ORDER BY source_path""",
                        (group["group_id"],),
                    ).fetchall()
                )
                if not sources:
                    continue
                child_path = child_index_from_parent(
                    parent_path,
                    cfg["folders"]["work"],
                    int(cfg["pairing"]["child_chars"]),
                )
                raw_path = (
                    Path(group["raw_path"])
                    if group["raw_path"] and Path(group["raw_path"]).is_file()
                    else raw_index_from_parent(parent_path)
                )
                raw_kb = cfg["weknora"].get("raw_knowledge_base")
                if not raw_kb:
                    raise RuntimeError("WeKnora原文知识库尚未配置")
                parent_doc_id = repair_storage_quota_failed_document(
                    group["parent_doc_id"],
                    parent_path,
                    sources[0],
                    cfg["weknora"],
                    cfg["weknora"]["parent_knowledge_base"],
                    "parent",
                    group["group_id"],
                    classification,
                )
                child_doc_id = repair_storage_quota_failed_document(
                    group["child_doc_id"],
                    child_path,
                    sources[0],
                    cfg["weknora"],
                    cfg["weknora"]["child_knowledge_base"],
                    "child",
                    group["group_id"],
                    classification,
                )
                raw_doc_id = repair_storage_quota_failed_document(
                    group["raw_doc_id"],
                    raw_path,
                    sources[0],
                    cfg["weknora"],
                    raw_kb,
                    "raw",
                    group["group_id"],
                    classification,
                ) or weknora_find_existing(
                    raw_path,
                    sources[0],
                    cfg["weknora"],
                    raw_kb,
                ) or weknora_upload(
                    raw_path,
                    sources[0],
                    cfg["weknora"],
                    raw_kb,
                    "raw",
                    group["group_id"],
                    classification,
                )
                save_group_state(
                    db,
                    group["group_id"],
                    group["group_name"],
                    "indexing",
                    parent_path,
                    parent_doc_id,
                    child_doc_id,
                    classification=classification,
                    raw_path=raw_path,
                    raw_doc_id=raw_doc_id,
                )
                finalize_group_indexing(
                    GroupIndexTask(
                        group_id=group["group_id"],
                        group_name=group["group_name"],
                        sources=sources,
                        parsed_paths=(parent_path,),
                        parent_path=parent_path,
                        child_path=child_path,
                        raw_path=raw_path,
                        parent_doc_id=parent_doc_id,
                        child_doc_id=child_doc_id,
                        raw_doc_id=raw_doc_id,
                        classification=classification,
                    ),
                    cfg,
                )
                stats["resumed_indexing"] += 1
            except Exception as exc:
                print(
                    f"历史索引断点仍需保留等待重试: "
                    f"{group['group_name']}: {exc}"
                )
        rows = db.execute(
            """SELECT * FROM groups
            WHERE state IN ('retry_wait','failed')
            ORDER BY updated_at"""
        ).fetchall()
        handled = 0
        for group in rows:
            members = db.execute(
                """SELECT gf.source_path,gf.sha256,f.markdown_path,f.state
                FROM group_files gf
                LEFT JOIN files f ON f.sha256=gf.sha256
                WHERE gf.group_id=? ORDER BY gf.source_path""",
                (group["group_id"],),
            ).fetchall()
            if not members:
                continue
            preparsed = []
            ready = True
            for member in members:
                markdown = (
                    Path(member["markdown_path"])
                    if member["markdown_path"]
                    else None
                )
                if (
                    member["state"] not in {
                        "source_missing_recovered", "parsed", "indexing"
                    }
                    or markdown is None
                    or not markdown.is_file()
                ):
                    ready = False
                    break
                preparsed.append((Path(member["source_path"]), markdown))
            if not ready:
                continue
            process_group(
                group["group_id"],
                group["group_name"],
                [source for source, _ in preparsed],
                cfg,
                db,
                preparsed=preparsed,
            )
            handled += 1
            updated = db.execute(
                "SELECT state FROM groups WHERE group_id=?",
                (group["group_id"],),
            ).fetchone()
            if updated and updated["state"] == "completed":
                stats["completed"] += 1
            else:
                stats["skipped"] += 1
            if limit > 0 and handled >= limit:
                break
        return stats
    finally:
        db.close()


def repair_stale_state_metadata(db: sqlite3.Connection) -> dict[str, int]:
    """Remove only provably orphaned DB links and label missing-source states."""
    orphan_links = db.execute(
        """SELECT count(*) FROM group_files gf
        LEFT JOIN groups g ON g.group_id=gf.group_id
        WHERE g.group_id IS NULL"""
    ).fetchone()[0]
    db.execute(
        """DELETE FROM group_files
        WHERE group_id NOT IN (SELECT group_id FROM groups)"""
    )
    marked = 0
    for row in db.execute(
        """SELECT sha256,source_path,state,error FROM files
        WHERE state IN ('queued','parsing','parsed','classification_pending')"""
    ).fetchall():
        if Path(row["source_path"]).exists():
            continue
        error = str(row["error"] or "")
        if "源文件历史缺失" not in error:
            db.execute(
                "UPDATE files SET error=?,updated_at=? WHERE sha256=?",
                (
                    ("源文件历史缺失；" + error).strip("；"),
                    int(time.time()),
                    row["sha256"],
                ),
            )
            marked += 1
    db.commit()
    return {"orphan_links_removed": int(orphan_links), "missing_marked": marked}


def process_group_lane(
    index: int,
    total: int,
    logical_group_id: str,
    group_name: str,
    sources: list[Path],
    cfg: dict,
    token_slot: str,
    submit_index_task=None,
    index_gate: ElasticConcurrencyGate | None = None,
) -> None:
    db = db_open()
    try:
        print(f"当前批次：{index} / {total}｜MinerU通道：{token_slot}")
        print(f"当前资料组：{group_name}｜文件{len(sources)}")
        print("当前动作：解析、题答合并、上传；索引验证由独立队列完成")
        try:
            digests = {}
            for source in sources:
                digests[source] = stable_sha256(source)
            reject_active_duplicate_source_instances(db, sources, digests)
            pending_group = pending_group_for_sources(db, sources, digests)
            group_id = (
                pending_group["group_id"]
                if pending_group
                else content_group_id(logical_group_id, sources, digests)
            )
            exclusion_state = permanent_group_exclusion_state(db, group_id)
            if exclusion_state:
                print(
                    f"资料组已永久排除，跳过且不重新生成: "
                    f"{group_name} ({group_id}, {exclusion_state})"
                )
                return
            if not register_group_files(db, group_id, sources, digests):
                print(
                    f"资料组在处理前已被永久排除，停止生成: "
                    f"{group_name} ({group_id})"
                )
                return
            process_group(
                group_id,
                group_name,
                sources,
                cfg,
                db,
                mineru_preferred_slot=token_slot,
                submit_index_task=submit_index_task,
                index_gate=index_gate,
            )
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            print(f"资料组暂未完成并保留在inbox: {group_name}: {exc}")
        counts = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT state,count(*) FROM files GROUP BY state"
            )
        }
        print(
            f"当前批次进度：{round(index * 100 / total)}%｜"
            f"{index}/{total}｜MinerU通道：{token_slot}｜"
            f"成功完成并按规则清理：{counts.get('completed', 0)}｜"
            f"失败并保留：{counts.get('failed', 0)}"
        )
    finally:
        db.close()


def processing_candidate_priority(
    source: Path,
    row: sqlite3.Row | None,
    candidate_metrics: dict,
    cfg: dict,
) -> int:
    state = row["state"] if row else "queued"
    state_rank = {
        "parsed": 0,
        "indexing": 0,
        "verified": 0,
        "cleanup_pending": 0,
        "parsing": 1,
        "submitting": 2,
        "queued": 2,
    }.get(state, 2)
    priority = 0 if source.suffix.casefold() in DIRECT_TEXT else state_rank
    row_error = (row["error"] or "") if row else ""
    if row and (
        "等待文件上传" in row_error
        or "waiting-file" in row_error.casefold()
    ):
        submitted_at = int(
            candidate_metrics.get("mineru_batch_submitted_at") or 0
        )
        stale_after = max(
            60,
            int(
                cfg["mineru"].get(
                    "waiting_file_resubmit_seconds", 600
                )
            ),
        )
        if (
            submitted_at <= 0
            or int(time.time()) - submitted_at >= stale_after
        ):
            return -1
        return 3
    if row and (
        "MinerU云端尚未完成" in row_error
        or "轮询暂时限流" in row_error
    ):
        # Healthy but unfinished cloud jobs remain behind likely-ready batches.
        return 3
    return priority


def processing_candidates(cfg: dict, db: sqlite3.Connection) -> list[Path]:
    """Return inbox files that can still make automatic progress."""
    terminal_states = {
        "completed",
        "excluded_completed",
        "classification_pending",
        "failed",
        "source_missing",
        "user_delete_pending",
        "user_deleted",
    }
    rows_by_path = {
        row["source_path"]: row
        for row in db.execute(
            "SELECT sha256,source_path,state,error,metrics_json,updated_at "
            "FROM files ORDER BY updated_at"
        ).fetchall()
    }
    candidates: list[tuple[tuple[int, int, int, str], Path]] = []
    for source in cfg["folders"]["inbox"].rglob("*"):
        if not source.is_file() or source.suffix.casefold() not in SUPPORTED:
            continue
        if is_appledouble_path(source):
            continue
        row = rows_by_path.get(str(source))
        if row and row["state"] in terminal_states:
            try:
                if stable_sha256(source) == str(row["sha256"]):
                    continue
            except (OSError, RuntimeError):
                # A file still being copied remains a candidate but will be
                # safely deferred by the stable-hash check at the worker entry.
                pass
            row = None
        pages = 0
        candidate_metrics: dict = {}
        if row and row["metrics_json"]:
            try:
                candidate_metrics = json.loads(row["metrics_json"])
                pages = int(candidate_metrics.get("source_pages") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                pages = 0
                candidate_metrics = {}
        priority = processing_candidate_priority(
            source, row, candidate_metrics, cfg
        )
        size = source.stat().st_size
        candidates.append(
            (
                (
                    priority,
                    pages if pages > 0 else 10**9,
                    size,
                    str(source).casefold(),
                ),
                source,
            )
        )
    return [source for _, source in sorted(candidates, key=lambda item: item[0])]


def mixed_size_group_schedule(
    group_items: list[tuple[tuple[str, str], list[Path]]],
    cfg: dict,
    db: sqlite3.Connection,
) -> list[tuple[tuple[str, str], list[Path]]]:
    """Interleave two light groups with one heavy group inside each state band."""
    rows_by_path = {
        row["source_path"]: row
        for row in db.execute(
            "SELECT source_path,state,error,metrics_json,updated_at FROM files"
        ).fetchall()
    }
    bands: dict[
        int,
        list[
            tuple[
                tuple[int, int, str],
                tuple[tuple[str, str], list[Path]],
            ]
        ],
    ] = {}
    for item in group_items:
        (_, group_name), sources = item
        priorities = []
        total_pages = 0
        total_image_cost = 0
        total_bytes = 0
        for source in sources:
            row = rows_by_path.get(str(source))
            metrics: dict = {}
            if row and row["metrics_json"]:
                try:
                    metrics = json.loads(row["metrics_json"])
                except (json.JSONDecodeError, TypeError):
                    metrics = {}
            priorities.append(
                processing_candidate_priority(source, row, metrics, cfg)
            )
            total_pages += int(metrics.get("source_pages") or 0)
            total_image_cost += (
                int(metrics.get("important_images") or 0) * 4
                + int(metrics.get("image_placeholders") or 0)
            )
            try:
                total_bytes += source.stat().st_size
            except OSError:
                pass
        band = min(priorities or [2])
        known_cost = total_pages * 4 + total_image_cost
        # Unknown-page files are ordered by size, but remain below known heavy
        # books only within their original processing-state priority band.
        size_key = (
            known_cost if known_cost > 0 else 10**9,
            total_bytes,
            group_name.casefold(),
        )
        bands.setdefault(band, []).append((size_key, item))

    scheduled = []
    light_per_heavy = max(
        1,
        min(
            4,
            int(
                (cfg.get("resource_control") or {}).get(
                    "group_mix_light_per_heavy", 2
                )
            ),
        ),
    )
    for band in sorted(bands):
        ranked = sorted(bands[band], key=lambda entry: entry[0])
        low = 0
        high = len(ranked) - 1
        while low <= high:
            for _ in range(light_per_heavy):
                if low > high:
                    break
                scheduled.append(ranked[low][1])
                low += 1
            if low <= high:
                scheduled.append(ranked[high][1])
                high -= 1
    return scheduled


def consume_processing_round(
    files: list[Path],
    cfg: dict,
    db: sqlite3.Connection,
    token_slots: list[str],
    round_number: int,
) -> None:
    groups: dict[tuple[str, str], list[Path]] = {}
    for source in files:
        key = source_group_key(source, cfg["folders"]["inbox"])
        groups.setdefault(key, []).append(source)
    group_items = mixed_size_group_schedule(
        list(groups.items()), cfg, db
    )
    resources = cfg.get("resource_control") or {}
    configured_max = int(resources.get("local_processing_workers_max", 4))
    worker_cap = max(
        1,
        min(configured_max, len(group_items) or 1),
    )
    initial_workers = adaptive_worker_count(
        cfg, "local", worker_cap
    )
    print(
        f"本地消费轮次：{round_number}｜资料组{len(group_items)}｜"
        f"初始通道{initial_workers}｜弹性上限{worker_cap}｜"
        f"云端Key{len(token_slots)}｜大小资料按轻"
        f"{max(1, min(4, int(resources.get('group_mix_light_per_heavy', 2))))}"
        "重1混合调度"
    )
    index_max = max(
        1,
        min(6, int(resources.get("weknora_inflight_groups_max", 4))),
    )
    index_min = max(
        1,
        min(
            index_max,
            int(resources.get("weknora_inflight_groups_min", 1)),
        ),
    )
    index_gate = ElasticConcurrencyGate(
        "WeKnora索引组",
        index_min,
        index_max,
        adaptive_weknora_index_count(cfg),
    )
    index_futures: list[Future] = []
    index_futures_lock = threading.Lock()
    next_item = 0
    active: dict[Future, int] = {}
    last_reported_workers: int | None = None
    memory_paused = False
    with ThreadPoolExecutor(
        max_workers=index_max,
        thread_name_prefix="weknora-index",
    ) as index_executor:
        def submit_index_task(task: GroupIndexTask) -> None:
            def run_and_release() -> None:
                try:
                    finalize_group_indexing(task, cfg)
                finally:
                    index_gate.release()

            future = index_executor.submit(run_and_release)
            with index_futures_lock:
                index_futures.append(future)

        with ThreadPoolExecutor(
            max_workers=worker_cap,
            thread_name_prefix="document-prepare",
        ) as executor:
            while next_item < len(group_items) or active:
                desired_workers = adaptive_worker_count(
                    cfg, "local", len(token_slots)
                )
                desired_workers = min(worker_cap, desired_workers)
                _, available_gb = windows_memory_gb()
                hard_pause_gb = max(
                    0.5,
                    float(resources.get("hard_pause_windows_free_gb", 0.65)),
                )
                resume_after_pause_gb = max(
                    hard_pause_gb + 0.25,
                    float(resources.get("critical_windows_free_gb", 1.2)),
                )
                if available_gb < hard_pause_gb:
                    memory_paused = True
                elif memory_paused and available_gb >= resume_after_pause_gb:
                    memory_paused = False
                if memory_paused:
                    desired_workers = 0
                index_limit, index_active, index_waiters = (
                    index_gate.snapshot()
                )
                with index_futures_lock:
                    index_pending = sum(
                        not future.done() for future in index_futures
                    )
                index_pressure = max(
                    index_pending,
                    index_active + index_waiters,
                )
                index_gate.set_limit(
                    adaptive_weknora_index_count(cfg, index_pressure),
                    "按内存和队列压力调整",
                    float(
                        resources.get(
                            "weknora_concurrency_increase_hold_seconds", 30
                        )
                    ),
                )
                index_limit, index_active, index_waiters = (
                    index_gate.snapshot()
                )
                queue_buffer = max(
                    0,
                    int(
                        resources.get(
                            "weknora_index_queue_buffer_groups", 1
                        )
                    ),
                )
                if index_pressure > index_limit + queue_buffer:
                    if desired_workers > 0:
                        desired_workers = max(
                            1,
                            desired_workers
                            - (index_pressure - index_limit - queue_buffer),
                        )
                mimo_limit, _, mimo_waiters = MIMO_REQUEST_GATE.snapshot()
                if mimo_waiters:
                    desired_workers = min(
                        desired_workers,
                        min(worker_cap, mimo_limit + 1),
                    )
                if desired_workers != last_reported_workers:
                    print(
                        f"本地消费通道动态调整："
                        f"{initial_workers if last_reported_workers is None else last_reported_workers}"
                        f"→{desired_workers}｜"
                        f"Windows可用内存{available_gb:.2f}GB｜"
                        f"索引压力{index_pressure}/{index_limit}｜"
                        f"MiMo等待{mimo_waiters}"
                    )
                    last_reported_workers = desired_workers
                while (
                    next_item < len(group_items)
                    and len(active) < desired_workers
                ):
                    index = next_item + 1
                    (
                        (logical_group_id, group_name),
                        sources,
                    ) = group_items[next_item]
                    token_slot = token_slots[
                        (index - 1) % len(token_slots)
                    ]
                    future = executor.submit(
                        process_group_lane,
                        index,
                        len(group_items),
                        logical_group_id,
                        group_name,
                        sources,
                        cfg,
                        token_slot,
                        submit_index_task,
                        index_gate,
                    )
                    active[future] = index
                    next_item += 1
                if not active:
                    time.sleep(5)
                    continue
                completed, _ = wait(
                    tuple(active),
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    active.pop(future, None)
                    future.result()
        with index_futures_lock:
            remaining_index_futures = tuple(index_futures)
        for future in remaining_index_futures:
            future.result()


def prequeue_mineru_source(
    index: int,
    total: int,
    source: Path,
    cfg: dict,
    token_slot: str,
) -> str:
    """只提交MinerU，不等待解析；批次号立即落库供后续消费。"""
    db = db_open()
    job: Path | None = None
    try:
        row = db.execute(
            """SELECT * FROM files WHERE source_path=?
            ORDER BY updated_at DESC LIMIT 1""",
            (str(source),),
        ).fetchone()
        wait_until_stable(source)
        digest = stable_sha256(source)
        reject_active_duplicate_source_instances(db, [source], {source: digest})
        if row and str(row["sha256"]) != digest:
            row = None
        if row and str(row["source_path"] or "") != str(source):
            row = None
        existing_batches = decode_batch_ids(row["batch_id"] if row else None)
        if existing_batches:
            print(
                f"MinerU预排队：{index}/{total}｜已提交，跳过：{source.name}"
            )
            return "already"
        if row and row["state"] in {
            "parsed", "indexing", "verified", "cleanup_pending", "completed"
        }:
            print(
                f"MinerU预排队：{index}/{total}｜已有后续结果，跳过："
                f"{source.name}"
            )
            return "already"
        row = row or db.execute(
            "SELECT * FROM files WHERE sha256=?", (digest,)
        ).fetchone()
        if row and str(row["source_path"] or "") != str(source):
            row = None

        job = cfg["folders"]["work"] / (
            f"{digest[:16]}-{source_path_token(source)}"
        )
        clean_job(job, cfg["folders"]["work"])
        parts = split_source(source, job, cfg["mineru"])
        batches: list[MinerUBatch] = []
        metrics = file_metrics(row)
        if not metrics["started_at"]:
            metrics["started_at"] = int(time.time())

        save_state(
            db,
            digest,
            source,
            "submitting",
            md_path=row["markdown_path"] if row else None,
            error="MinerU预提交中",
            metrics=metrics,
        )

        def persist_created_batch(batch: MinerUBatch) -> None:
            batches.append(batch)
            metrics["mineru_token_slots"] = sorted({
                item.token_slot for item in batches
            })
            save_state(
                db,
                digest,
                source,
                "parsing",
                batch_id=encode_batch_ids(batches),
                md_path=row["markdown_path"] if row else None,
                error="已提交MinerU云端队列，等待后续消费",
                metrics=metrics,
            )

        metrics["mineru_batch_submitted_at"] = int(time.time())
        mineru_submit(
            parts,
            mineru_tokens(),
            cfg["mineru"],
            on_batch_created=persist_created_batch,
            preferred_slot=token_slot,
        )
        save_state(
            db,
            digest,
            source,
            "parsing",
            batch_id=encode_batch_ids(batches),
            md_path=row["markdown_path"] if row else None,
            error="已提交MinerU云端队列，等待后续消费",
            metrics=metrics,
        )
        print(
            f"MinerU预排队：{index}/{total}｜{token_slot}｜"
            f"已提交：{source.name}"
        )
        return "submitted"
    except Exception as exc:
        digest = sha256(source) if source.exists() else ""
        if digest:
            row = db.execute(
                "SELECT * FROM files WHERE sha256=?", (digest,)
            ).fetchone()
            if (
                source.exists()
                and is_permanent_source_parse_error(str(exc))
            ):
                failed_source = move_to_failed(
                    source, cfg["folders"]["failed"]
                )
                save_state(
                    db,
                    digest,
                    failed_source,
                    "failed",
                    batch_id=row["batch_id"] if row else None,
                    md_path=row["markdown_path"] if row else None,
                    error=f"永久解析失败并保留: {exc}",
                    metrics=file_metrics(row),
                )
                print(
                    f"MinerU预排队：{index}/{total}｜文件本身无法解析，"
                    f"已保留到failed：{source.name}: {exc}"
                )
                return "failed"
            known_batches = decode_batch_ids(row["batch_id"] if row else None)
            save_state(
                db,
                digest,
                source,
                "parsing" if known_batches else "queued",
                batch_id=row["batch_id"] if row else None,
                md_path=row["markdown_path"] if row else None,
                error=f"MinerU预提交等待重试: {exc}",
                metrics=file_metrics(row),
            )
        print(
            f"MinerU预排队：{index}/{total}｜暂未提交，源文件保留："
            f"{source.name}: {exc}"
        )
        return "retry"
    finally:
        if job and cfg["cleanup"]["delete_temporary_files"] and job.exists():
            shutil.rmtree(job, ignore_errors=True)
        db.close()


def prequeue_all_mineru(
    files: list[Path], cfg: dict, token_slots: list[str]
) -> None:
    if not files:
        return
    workers_per_key = max(
        1, int(cfg["mineru"].get("submission_workers_per_key", 4))
    )
    requested_workers = min(
        len(files), len(token_slots) * workers_per_key
    )
    worker_count = adaptive_worker_count(
        cfg, "prequeue", requested_workers
    )
    print(
        f"MinerU优先排队开始：文件{len(files)}｜"
        f"并行Key {len(token_slots)}｜上传槽{worker_count}｜"
        "先提交、不等待解析"
    )
    counts = {"submitted": 0, "already": 0, "retry": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                prequeue_mineru_source,
                index,
                len(files),
                source,
                cfg,
                token_slots[(index - 1) % len(token_slots)],
            )
            for index, source in enumerate(files, 1)
        ]
        for future in futures:
            outcome = future.result()
            counts[outcome] += 1
    print(
        "MinerU优先排队完成："
        f"新提交{counts['submitted']}｜已在队列{counts['already']}｜"
        f"永久失败并保留{counts['failed']}｜"
        f"等待重试{counts['retry']}｜随后开始消费结果"
    )


def migrate_lightweight_indexes(cfg: dict, db: sqlite3.Connection) -> None:
    """把已有Markdown迁移到当前配置的父/子知识库，不重复解析源文件。"""
    preflight(cfg, db)
    rows = db.execute(
        """SELECT * FROM groups
        WHERE markdown_path IS NOT NULL AND state != 'failed'
        ORDER BY updated_at"""
    ).fetchall()
    candidates = [
        row for row in rows if Path(row["markdown_path"]).is_file()
    ]
    print(f"轻量索引迁移：共{len(candidates)}组")
    for index, row in enumerate(candidates, 1):
        parent_path = Path(row["markdown_path"])
        child_path = child_index_from_parent(
            parent_path,
            cfg["folders"]["work"],
            int(cfg["pairing"]["child_chars"]),
        )
        parent_doc_id = None
        child_doc_id = None
        raw_doc_id = None
        raw_path: Path | None = None
        source_rows = db.execute(
            "SELECT source_path FROM group_files WHERE group_id=?",
            (row["group_id"],),
        ).fetchall()
        sources = [Path(item["source_path"]) for item in source_rows]
        source_hint = sources[0] if sources else parent_path
        try:
            wc = cfg["weknora"]
            raw_path = (
                Path(row["raw_path"])
                if row["raw_path"] and Path(row["raw_path"]).is_file()
                else raw_index_from_parent(parent_path)
            )
            raw_kb = wc.get("raw_knowledge_base")
            if not raw_kb:
                raise RuntimeError("WeKnora原文知识库尚未配置")
            parent_doc_id = weknora_find_existing(
                parent_path, source_hint, wc, wc["parent_knowledge_base"]
            ) or weknora_upload(
                parent_path,
                source_hint,
                wc,
                wc["parent_knowledge_base"],
                "parent",
                row["group_id"],
            )
            child_doc_id = weknora_find_existing(
                child_path, source_hint, wc, wc["child_knowledge_base"]
            ) or weknora_upload(
                child_path,
                source_hint,
                wc,
                wc["child_knowledge_base"],
                "child",
                row["group_id"],
            )
            raw_doc_id = row["raw_doc_id"] or weknora_find_existing(
                raw_path, source_hint, wc, raw_kb
            ) or weknora_upload(
                raw_path,
                source_hint,
                wc,
                raw_kb,
                "raw",
                row["group_id"],
            )
            save_group_state(
                db,
                row["group_id"],
                row["group_name"],
                "indexing",
                parent_path,
                parent_doc_id,
                child_doc_id,
                raw_path=raw_path,
                raw_doc_id=raw_doc_id,
            )
            verify_two_level_indexes(
                parent_path,
                child_path,
                source_hint,
                wc,
                parent_doc_id,
                child_doc_id,
                raw_path=raw_path,
                raw_doc_id=raw_doc_id,
            )
            cleanup_errors = cleanup_verified_sources(
                sources,
                parent_path,
                parent_doc_id,
                row["group_id"],
                cfg,
                db,
            )
            save_group_state(
                db,
                row["group_id"],
                row["group_name"],
                "cleanup_pending" if cleanup_errors else "completed",
                parent_path,
                parent_doc_id,
                child_doc_id,
                "; ".join(cleanup_errors),
                raw_path=raw_path,
                raw_doc_id=raw_doc_id,
            )
            print(
                f"轻量索引迁移：{index}/{len(candidates)}｜"
                f"{row['group_name']}｜完成"
            )
        except Exception as exc:
            save_group_state(
                db,
                row["group_id"],
                row["group_name"],
                "indexing",
                parent_path,
                parent_doc_id,
                child_doc_id,
                f"轻量索引迁移等待续传: {exc}",
                raw_path=raw_path,
                raw_doc_id=raw_doc_id,
            )
            raise
        finally:
            if cfg["cleanup"]["delete_temporary_files"]:
                child_path.unlink(missing_ok=True)


def local_parsed_candidate(
    markdown_root: Path,
    digest: str,
    excluded: set[Path],
) -> Path | None:
    matches = [
        path
        for path in markdown_root.glob(f"*-{digest[:10]}.md")
        if path.resolve() not in excluded
    ]
    return matches[0] if len(matches) == 1 else None


def migration_parsed_inputs(
    row: sqlite3.Row,
    members: list[sqlite3.Row],
    cfg: dict,
    job: Path,
    require_original: bool,
    memory_gate=None,
) -> list[tuple[Path, Path]]:
    """Use local MinerU markdown first, then the existing cloud batch."""
    parent_path = Path(row["markdown_path"])
    excluded = {parent_path.resolve()}
    if row["raw_path"]:
        excluded.add(Path(row["raw_path"]).resolve())
    parsed: list[tuple[Path, Path]] = []
    tokens = mineru_tokens()
    for member_index, member in enumerate(members, 1):
        if memory_gate is not None:
            memory_gate(f"恢复第{member_index}/{len(members)}份解析结果")
        source = Path(member["source_path"])
        local = local_parsed_candidate(
            cfg["folders"]["markdown"],
            str(member["sha256"]),
            excluded,
        )
        if local is not None:
            parsed.append((source, local))
            continue
        batches = decode_batch_ids(member["batch_id"])
        if require_original and batches:
            member_job = job / f"member-{member_index:03d}"
            member_job.mkdir(parents=True, exist_ok=True)
            items = mineru_wait(batches, tokens, cfg["mineru"])
            recovered = download_recovered_results(items, source, member_job)
            metrics = file_metrics(member)
            sections = []
            for part_index, (part, markdown) in enumerate(recovered, 1):
                if memory_gate is not None:
                    memory_gate(
                        f"补图第{member_index}/{len(members)}份"
                        f"第{part_index}/{len(recovered)}卷"
                    )
                sections.append(
                    repair_images(
                        markdown.read_text("utf-8"),
                        markdown,
                        part.path,
                        member_job,
                        cfg["ollama"],
                        metrics,
                    )
                )
            combined = member_job / (
                f"{safe_path_component(source.stem, 'source')}.md"
            )
            atomic_write(combined, "\n\n".join(sections).strip() + "\n")
            parsed.append((source, combined))
            continue
        if len(members) == 1:
            parsed.append((source, parent_path))
            continue
        raise RuntimeError(
            f"缺少可恢复的独立Markdown，无法安全重建多文件题答组: {source.name}"
        )
    return parsed


def parent_has_nonempty_answers(parent_path: Path) -> bool:
    body = markdown_body(parent_path)
    fields = re.split(r"(?m)^\*\*(题目|正文|答案|解析)\*\*\s*$", body)
    return any(
        fields[index].strip() == "答案" and fields[index + 1].strip()
        for index in range(1, len(fields) - 1, 2)
    )


def set_content_migration_status(
    db: sqlite3.Connection,
    state: str,
    index: int,
    total: int,
    group_name: str = "",
    action: str = "",
) -> None:
    value = json.dumps(
        {
            "state": state,
            "index": index,
            "total": total,
            "group_name": group_name,
            "action": action,
            "updated_at": int(time.time()),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    db.execute(
        """INSERT INTO metadata(key,value)
        VALUES('content_migration_status',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (value,),
    )
    db.commit()


def content_migration_journal_path(
    prepared: PreparedContentMigration,
) -> Path:
    return prepared.job / "migration-journal.json"


def write_content_migration_journal(
    prepared: PreparedContentMigration,
    cfg: dict,
    phase: str,
    parent_doc_id: str = "",
    child_doc_id: str = "",
    raw_doc_id: str = "",
    parent_created: bool = False,
    child_created: bool = False,
    raw_created: bool = False,
    error: str = "",
    upload_attempted_layer: str = "",
    parent_placed: bool = False,
    raw_placed: bool = False,
) -> Path:
    """Persist intent before upload and acquired IDs before interruption."""
    path = content_migration_journal_path(prepared)
    previous: dict = {}
    if path.is_file():
        try:
            previous = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    previous_layers = {
        str(layer.get("name") or ""): layer
        for layer in previous.get("layers") or []
    }
    layer_inputs = (
        (
            "parent",
            prepared.parent_path,
            prepared.final_parent_path,
            parent_doc_id,
            cfg["weknora"]["parent_knowledge_base"],
            parent_created,
        ),
        (
            "child",
            prepared.child_path,
            None,
            child_doc_id,
            cfg["weknora"]["child_knowledge_base"],
            child_created,
        ),
        (
            "raw",
            prepared.raw_path,
            prepared.final_raw_path,
            raw_doc_id,
            cfg["weknora"]["raw_knowledge_base"],
            raw_created,
        ),
    )
    layers = []
    for (
        name,
        local_path,
        fallback_path,
        doc_id,
        knowledge_base_id,
        created,
    ) in layer_inputs:
        old = previous_layers.get(name) or {}
        content_path = local_path if local_path.is_file() else fallback_path
        file_md5 = str(old.get("file_md5") or "")
        file_sha256 = str(old.get("file_sha256") or "")
        if content_path is not None and content_path.is_file():
            file_md5 = md5_digest(content_path)
            file_sha256 = sha256(content_path)
        layers.append(
            {
                "name": name,
                "doc_id": doc_id or str(old.get("doc_id") or ""),
                "knowledge_base_id": knowledge_base_id,
                "created": bool(created or old.get("created")),
                "upload_attempted": bool(
                    old.get("upload_attempted")
                    or upload_attempted_layer == name
                ),
                "new_placed": bool(
                    old.get("new_placed")
                    or (name == "parent" and parent_placed)
                    or (name == "raw" and raw_placed)
                ),
                "remote_file_name": str(
                    old.get("remote_file_name") or local_path.name
                ),
                "local_path": str(
                    old.get("local_path") or local_path
                ),
                "fallback_path": str(
                    old.get("fallback_path")
                    or (fallback_path if fallback_path is not None else "")
                ),
                "file_md5": file_md5,
                "file_sha256": file_sha256,
            }
        )
    payload = {
        "version": 2,
        "group_id": prepared.group_id,
        "group_name": prepared.group_name,
        "phase": phase,
        "updated_at": int(time.time()),
        "source_hint": str(prepared.source_hint),
        "classification": classification_to_dict(
            prepared.classification
        ),
        "old_state": {
            "parent_path": str(prepared.old_parent),
            "raw_path": str(prepared.old_raw or ""),
            "parent_sha256": prepared.old_parent_sha256,
            "raw_sha256": prepared.old_raw_sha256,
            "parent_doc_id": prepared.old_parent_id,
            "child_doc_id": prepared.old_child_id,
            "raw_doc_id": prepared.old_raw_id,
        },
        "final_parent_path": str(prepared.final_parent_path),
        "final_raw_path": str(prepared.final_raw_path),
        "layers": layers,
        "error": error[:2000],
    }
    atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return path


def recover_content_migration_journals(
    cfg: dict,
    db: sqlite3.Connection,
) -> dict[str, int]:
    """Recover index IDs/files from an interrupted pre-commit publication."""
    root = cfg["folders"]["work"] / "content-migration"
    result = {"journals": 0, "queued": 0, "adopted": 0, "errors": 0}
    if not root.is_dir():
        return result
    markdown_root = cfg["folders"]["markdown"].resolve()
    for journal in sorted(root.glob("*/migration-journal.json")):
        try:
            payload = json.loads(journal.read_text("utf-8"))
            group_id = str(payload.get("group_id") or "").strip()
            if not group_id:
                raise RuntimeError("迁移日志缺少group_id")
            try:
                classification = classification_from_dict(
                    payload.get("classification") or {}
                )
            except (KeyError, TypeError, ValueError):
                classification = None
            source_hint = Path(
                str(payload.get("source_hint") or journal)
            )
            for layer in payload.get("layers") or []:
                doc_id = str(layer.get("doc_id") or "").strip()
                knowledge_base_id = str(
                    layer.get("knowledge_base_id") or ""
                ).strip()
                created = bool(layer.get("created"))
                if (
                    not doc_id
                    and bool(layer.get("upload_attempted"))
                ):
                    if classification is None:
                        raise RuntimeError(
                            "上传状态不明且迁移日志缺少分类信息"
                        )
                    local_candidates = [
                        Path(str(layer.get(key) or ""))
                        for key in ("local_path", "fallback_path")
                        if str(layer.get(key) or "").strip()
                    ]
                    content_path = next(
                        (
                            candidate
                            for candidate in local_candidates
                            if candidate.is_file()
                            and md5_digest(candidate)
                            == str(layer.get("file_md5") or "")
                        ),
                        None,
                    )
                    if content_path is None:
                        raise RuntimeError(
                            "上传状态不明且找不到哈希一致的本地内容："
                            f"{layer.get('name')}"
                        )
                    doc_id = (
                        weknora_find_existing(
                            content_path,
                            source_hint,
                            cfg["weknora"],
                            knowledge_base_id,
                            str(layer.get("name") or ""),
                            group_id,
                            classification,
                            expected_file_name=str(
                                layer.get("remote_file_name") or ""
                            ),
                        )
                        or ""
                    )
                    if not doc_id:
                        raise RuntimeError(
                            "上传结果状态不明且远端暂不可见，保留日志重试："
                            f"{layer.get('name')}"
                        )
                    created = bool(doc_id)
                if not doc_id or not created:
                    continue
                enqueue_index_cleanup(
                    db,
                    group_id,
                    doc_id,
                    knowledge_base_id,
                    journal,
                )
                result["queued"] += 1
            group_state = db.execute(
                "SELECT state FROM groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            user_deleted = bool(
                group_state
                and str(group_state["state"]) in {
                    "user_delete_pending",
                    "user_deleted",
                }
            )
            layer_by_name = {
                str(layer.get("name") or ""): layer
                for layer in payload.get("layers") or []
            }
            for field, rollback_name, layer_name in (
                (
                    "final_parent_path",
                    "rollback-parent.md",
                    "parent",
                ),
                ("final_raw_path", "rollback-raw.md", "raw"),
            ):
                raw_path = str(payload.get(field) or "").strip()
                if not raw_path:
                    continue
                candidate = Path(raw_path)
                resolved = candidate.resolve()
                if (
                    resolved == markdown_root
                    or markdown_root not in resolved.parents
                ):
                    raise RuntimeError(
                        f"迁移日志中的正式路径越界: {candidate}"
                    )
                active = db.execute(
                    """SELECT group_id FROM groups
                    WHERE state NOT IN (
                        'user_delete_pending','user_deleted'
                    )
                    AND (markdown_path=? OR raw_path=?)
                    LIMIT 1""",
                    (str(candidate), str(candidate)),
                ).fetchone()
                rollback = journal.parent / rollback_name
                expected_sha256 = str(
                    (layer_by_name.get(layer_name) or {}).get(
                        "file_sha256"
                    )
                    or ""
                )
                new_placed = bool(
                    (layer_by_name.get(layer_name) or {}).get(
                        "new_placed"
                    )
                )
                if active:
                    if str(active["group_id"]) != group_id:
                        raise RuntimeError(
                            "迁移正式路径被其他资料组采用，拒绝自动处理："
                            f"{candidate}"
                        )
                    if (
                        not candidate.is_file()
                        or not expected_sha256
                        or sha256(candidate) != expected_sha256
                    ):
                        raise RuntimeError(
                            "数据库已采用迁移路径，但正式文件缺失或哈希不符；"
                            f"保留日志等待人工恢复：{candidate}"
                        )
                    rollback.unlink(missing_ok=True)
                    continue
                if user_deleted:
                    if candidate.is_file():
                        if (
                            not new_placed
                            or
                            not expected_sha256
                            or sha256(candidate) != expected_sha256
                        ):
                            raise RuntimeError(
                                "待删除路径并非已确认由本次迁移放置，"
                                "或文件哈希不符，拒绝自动删除："
                                f"{candidate}"
                            )
                        candidate.unlink()
                    rollback.unlink(missing_ok=True)
                    continue
                if rollback.is_file():
                    if candidate.is_file():
                        if (
                            not expected_sha256
                            or sha256(candidate) != expected_sha256
                        ):
                            raise RuntimeError(
                                "迁移候选文件哈希不符，拒绝覆盖："
                                f"{candidate}"
                            )
                        candidate.unlink()
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    rollback.replace(candidate)
                elif candidate.is_file():
                    if (
                        not new_placed
                        or
                        not expected_sha256
                        or sha256(candidate) != expected_sha256
                    ):
                        raise RuntimeError(
                            "正式路径并非已确认由本次迁移放置，"
                            "或文件哈希不符，拒绝自动删除："
                            f"{candidate}"
                        )
                    candidate.unlink()
            cleanup = drain_index_cleanup_queue(
                db,
                cfg,
                group_id=group_id,
                allow_unresolved_journal_group=group_id,
            )
            result["adopted"] += cleanup.get("adopted", 0)
            journal.unlink()
            result["journals"] += 1
        except Exception as exc:
            result["errors"] += 1
            print(f"迁移中断日志保留等待恢复：{journal}｜{exc}")
    return result


def wait_for_content_migration_memory(
    cfg: dict,
    action: str,
    prefetch_release: threading.Event | None = None,
) -> None:
    resources = cfg.get("resource_control") or {}
    critical_resume_gb = max(
        float(resources.get("critical_windows_free_gb", 1.0)),
        float(resources.get("hard_pause_windows_free_gb", 0.65)),
    )
    pipeline_resume_gb = max(
        critical_resume_gb,
        float(resources.get("content_migration_pipeline_min_free_gb", 2.0)),
    )
    reported = False
    while True:
        resume_gb = (
            pipeline_resume_gb
            if prefetch_release is not None and not prefetch_release.is_set()
            else critical_resume_gb
        )
        _, available_gb = windows_memory_gb()
        if available_gb >= resume_gb:
            return
        if not reported:
            print(
                "内容结构迁移弹性暂停："
                f"Windows可用内存{available_gb:.2f}GB｜"
                f"恢复线{resume_gb:.2f}GB｜{action}"
            )
            reported = True
        if prefetch_release is not None and not prefetch_release.is_set():
            prefetch_release.wait(timeout=5)
        else:
            time.sleep(30)


def content_migration_prefetch_allowed(cfg: dict) -> bool:
    resources = cfg.get("resource_control") or {}
    if not bool(resources.get("content_migration_pipeline_enabled", True)):
        return False
    minimum = float(
        resources.get("content_migration_pipeline_min_free_gb", 2.0)
    )
    _, available_gb = windows_memory_gb()
    available_gb = smoothed_available_memory_gb(
        "content-migration-pipeline",
        available_gb,
        int(resources.get("memory_hysteresis_samples", 3)),
    )
    return available_gb >= minimum


def prepare_content_migration(
    cfg: dict,
    row_data: dict,
    index: int,
    total: int,
    prefetch_release: threading.Event | None = None,
) -> PreparedContentMigration:
    """Prepare one group on disk; never alter its live DB row or old indexes."""
    group_id = str(row_data["group_id"])
    group_name = str(row_data["group_name"] or group_id)
    expected_updated_at = int(row_data["updated_at"] or 0)
    old_parent = Path(row_data["markdown_path"])
    old_raw_value = str(row_data.get("raw_path") or "").strip()
    old_raw = Path(old_raw_value) if old_raw_value else None
    job = cfg["folders"]["work"] / "content-migration" / group_id
    if job.exists():
        unresolved = (
            (job / "migration-journal.json").exists()
            or (job / "rollback-parent.md").exists()
            or (job / "rollback-raw.md").exists()
        )
        if unresolved:
            raise RuntimeError(
                f"检测到未恢复的迁移日志或回滚文件，拒绝覆盖: {job}"
            )
        work_root = cfg["folders"]["work"].resolve()
        resolved_job = job.resolve()
        if resolved_job == work_root or work_root not in resolved_job.parents:
            raise RuntimeError(f"拒绝清理工作目录外路径: {resolved_job}")
        shutil.rmtree(resolved_job, ignore_errors=True)
    job.mkdir(parents=True, exist_ok=True)
    worker_db = db_open()
    try:
        fresh = worker_db.execute(
            """SELECT state,updated_at,markdown_path,raw_path,
            parent_doc_id,child_doc_id,raw_doc_id,classification_json
            FROM groups WHERE group_id=?""",
            (group_id,),
        ).fetchone()
        if (
            not fresh
            or str(fresh["state"]) != "completed"
            or int(fresh["updated_at"] or 0) != expected_updated_at
            or str(fresh["markdown_path"] or "")
            != str(row_data["markdown_path"] or "")
            or str(fresh["raw_path"] or "")
            != str(row_data.get("raw_path") or "")
            or str(fresh["parent_doc_id"] or "")
            != str(row_data.get("parent_doc_id") or "")
            or str(fresh["child_doc_id"] or "")
            != str(row_data.get("child_doc_id") or "")
            or str(fresh["raw_doc_id"] or "")
            != str(row_data.get("raw_doc_id") or "")
            or str(fresh["classification_json"] or "")
            != str(row_data.get("classification_json") or "")
            or not old_parent.is_file()
        ):
            raise ContentMigrationCancelled(
                "资料组已被删除、更新或父块已被用户移除"
            )
        try:
            classification = classification_from_dict(
                json.loads(row_data.get("classification_json") or "{}")
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            classification = classification_from_markdown(old_parent)
            if classification is None:
                raise RuntimeError("缺少可用分类结果")
        members = [
            dict(member)
            for member in worker_db.execute(
                """SELECT gf.sha256,gf.source_path,f.batch_id,f.state,
                f.markdown_path,f.metrics_json
                FROM group_files gf
                JOIN files f ON f.sha256=gf.sha256
                WHERE gf.group_id=? ORDER BY gf.source_path""",
                (group_id,),
            ).fetchall()
        ]
        active_markdown_paths = {
            str(Path(value).resolve()).casefold()
            for active_row in worker_db.execute(
                """SELECT markdown_path,raw_path FROM groups
                WHERE state NOT IN (
                    'user_delete_pending','user_deleted'
                )"""
            ).fetchall()
            for value in (
                active_row["markdown_path"],
                active_row["raw_path"],
            )
            if value
        }
    finally:
        worker_db.close()
    if not members:
        raise RuntimeError("资料组没有成员记录")
    if old_raw is not None and not old_raw.is_file():
        raise ContentMigrationCancelled(
            "资料组登记的原文Markdown已被移除，拒绝自动重建"
        )
    old_parent_digest = sha256(old_parent)
    old_raw_digest = sha256(old_raw) if old_raw is not None else ""
    wait_for_content_migration_memory(
        cfg,
        f"准备{group_name}",
        prefetch_release,
    )
    has_answer_source = any(
        ANSWER_FILE_RE.search(Path(member["source_path"]).stem)
        for member in members
    )
    require_original = (
        classification.document_type in {"教材", "讲义"}
        or has_answer_source
        or not parent_has_nonempty_answers(old_parent)
    )
    parsed = migration_parsed_inputs(
        row_data,
        members,
        cfg,
        job,
        require_original,
        lambda action: wait_for_content_migration_memory(
            cfg,
            f"{group_name}｜{action}",
            prefetch_release,
        ),
    )
    wait_for_content_migration_memory(
        cfg,
        f"生成{group_name}父子原文",
        prefetch_release,
    )
    staging_root = job / "staged-markdown"
    (
        parent_path,
        child_path,
        raw_path,
        unit_count,
        unmatched_answers,
        document_mode,
    ) = group_documents(
        group_id,
        group_name,
        parsed,
        staging_root,
        job,
        int(cfg["pairing"]["child_chars"]),
        classification,
    )
    final_directory = classification_directory(
        cfg["folders"]["markdown"],
        classification,
        create=False,
    )
    final_parent_path = final_directory / parent_path.name
    final_raw_path = final_directory / raw_path.name

    def unique_target(candidate: Path, forbidden: set[str]) -> Path:
        for attempt in range(100):
            resolved = str(candidate.resolve()).casefold()
            if (
                resolved not in forbidden
                and resolved not in active_markdown_paths
                and not candidate.exists()
            ):
                return candidate
            candidate = candidate.with_name(
                f"{candidate.stem}-发布-{group_id[:8]}-"
                f"{time.time_ns()}-{attempt}{candidate.suffix}"
            )
        raise RuntimeError("无法为内容迁移分配无冲突的正式Markdown路径")

    forbidden_paths = {str(old_parent.resolve()).casefold()}
    if old_raw is not None:
        forbidden_paths.add(str(old_raw.resolve()).casefold())
    final_parent_path = unique_target(final_parent_path, forbidden_paths)
    forbidden_paths.add(str(final_parent_path.resolve()).casefold())
    final_raw_path = unique_target(final_raw_path, forbidden_paths)
    return PreparedContentMigration(
        index=index,
        total=total,
        group_id=group_id,
        group_name=group_name,
        expected_updated_at=expected_updated_at,
        expected_classification_json=str(
            row_data.get("classification_json") or ""
        ),
        old_parent=old_parent,
        old_raw=old_raw,
        old_parent_sha256=old_parent_digest,
        old_raw_sha256=old_raw_digest,
        old_parent_id=str(row_data.get("parent_doc_id") or ""),
        old_child_id=str(row_data.get("child_doc_id") or ""),
        old_raw_id=str(row_data.get("raw_doc_id") or ""),
        parent_path=parent_path,
        child_path=child_path,
        raw_path=raw_path,
        final_parent_path=final_parent_path,
        final_raw_path=final_raw_path,
        source_hint=Path(members[0]["source_path"]),
        classification=classification,
        unit_count=unit_count,
        unmatched_answers=unmatched_answers,
        document_mode=document_mode,
        job=job,
    )


def enqueue_new_migration_indexes_for_cleanup(
    db: sqlite3.Connection,
    cfg: dict,
    prepared: PreparedContentMigration,
    parent_doc_id: str,
    child_doc_id: str,
    raw_doc_id: str,
    parent_created: bool,
    child_created: bool,
    raw_created: bool,
) -> None:
    protected_old_ids = {
        prepared.old_parent_id,
        prepared.old_child_id,
        prepared.old_raw_id,
    }
    for doc_id, created, knowledge_base_id in (
        (
            parent_doc_id,
            parent_created,
            cfg["weknora"]["parent_knowledge_base"],
        ),
        (
            child_doc_id,
            child_created,
            cfg["weknora"]["child_knowledge_base"],
        ),
        (
            raw_doc_id,
            raw_created,
            cfg["weknora"]["raw_knowledge_base"],
        ),
    ):
        if not created or not doc_id or doc_id in protected_old_ids:
            continue
        enqueue_index_cleanup(
            db,
            prepared.group_id,
            doc_id,
            knowledge_base_id,
            prepared.parent_path,
        )
    drain_index_cleanup_queue(
        db,
        cfg,
        group_id=prepared.group_id,
    )


def publish_content_migration(
    prepared: PreparedContentMigration,
    cfg: dict,
    db: sqlite3.Connection,
) -> None:
    """Publish one staged group, then atomically switch DB references."""
    parent_doc_id = ""
    child_doc_id = ""
    raw_doc_id = ""
    parent_created = False
    child_created = False
    raw_created = False
    database_switched = False
    preserve_job = False
    parent_new_placed = False
    raw_new_placed = False

    def acquire_index(
        path: Path,
        knowledge_base_id: str,
        layer: str,
    ) -> None:
        nonlocal parent_doc_id, child_doc_id, raw_doc_id
        nonlocal parent_created, child_created, raw_created
        existing = weknora_find_existing(
            path,
            prepared.source_hint,
            wc,
            knowledge_base_id,
            layer,
            prepared.group_id,
            prepared.classification,
        )
        if existing:
            doc_id = existing
            created = False
        else:
            write_content_migration_journal(
                prepared,
                cfg,
                f"{layer}_upload_pending",
                parent_doc_id,
                child_doc_id,
                raw_doc_id,
                parent_created,
                child_created,
                raw_created,
                upload_attempted_layer=layer,
            )
            doc_id = weknora_upload(
                path,
                prepared.source_hint,
                wc,
                knowledge_base_id,
                layer,
                prepared.group_id,
                prepared.classification,
            )
            created = True
        if layer == "parent":
            parent_doc_id, parent_created = doc_id, created
        elif layer == "child":
            child_doc_id, child_created = doc_id, created
        elif layer == "raw":
            raw_doc_id, raw_created = doc_id, created
        else:
            raise RuntimeError(f"未知迁移索引层: {layer}")
        write_content_migration_journal(
            prepared,
            cfg,
            f"{layer}_acquired",
            parent_doc_id,
            child_doc_id,
            raw_doc_id,
            parent_created,
            child_created,
            raw_created,
        )

    try:
        current = db.execute(
            """SELECT state,updated_at,markdown_path,raw_path,
            parent_doc_id,child_doc_id,raw_doc_id,classification_json
            FROM groups WHERE group_id=?""",
            (prepared.group_id,),
        ).fetchone()
        if (
            not content_migration_snapshot_matches(current, prepared)
            or not content_migration_source_files_match(prepared)
        ):
            raise ContentMigrationCancelled(
                "发布前检测到资料组已删除或发生更新"
            )
        wait_for_content_migration_memory(
            cfg, f"发布{prepared.group_name}"
        )
        wc = cfg["weknora"]
        write_content_migration_journal(
            prepared,
            cfg,
            "starting",
        )
        acquire_index(
            prepared.parent_path,
            wc["parent_knowledge_base"],
            "parent",
        )
        acquire_index(
            prepared.child_path,
            wc["child_knowledge_base"],
            "child",
        )
        acquire_index(
            prepared.raw_path,
            wc["raw_knowledge_base"],
            "raw",
        )
        write_content_migration_journal(
            prepared,
            cfg,
            "indexes_acquired",
            parent_doc_id,
            child_doc_id,
            raw_doc_id,
            parent_created,
            child_created,
            raw_created,
        )
        verify_two_level_indexes(
            prepared.parent_path,
            prepared.child_path,
            prepared.source_hint,
            wc,
            parent_doc_id,
            child_doc_id,
            classification=prepared.classification,
            full_check=True,
            raw_path=prepared.raw_path,
            raw_doc_id=raw_doc_id,
        )
        current = db.execute(
            """SELECT state,updated_at,markdown_path,raw_path,
            parent_doc_id,child_doc_id,raw_doc_id,classification_json
            FROM groups WHERE group_id=?""",
            (prepared.group_id,),
        ).fetchone()
        if (
            not content_migration_snapshot_matches(current, prepared)
            or not content_migration_source_files_match(prepared)
        ):
            raise ContentMigrationCancelled(
                "验证后检测到资料组已删除或发生更新；索引不会切换"
            )
        cleanup_specs = (
            (prepared.old_parent_id, wc["parent_knowledge_base"]),
            (prepared.old_child_id, wc["child_knowledge_base"]),
            (prepared.old_raw_id, wc["raw_knowledge_base"]),
        )
        record = classification_to_dict(prepared.classification)
        record["content_structure_version"] = CONTENT_STRUCTURE_VERSION
        record["child_chunk_version"] = CHILD_CHUNK_VERSION
        record["content_structure_mode"] = prepared.document_mode
        record["unmatched_answers"] = prepared.unmatched_answers
        write_content_migration_journal(
            prepared,
            cfg,
            "verified",
            parent_doc_id,
            child_doc_id,
            raw_doc_id,
            parent_created,
            child_created,
            raw_created,
        )
        try:
            db.execute("BEGIN IMMEDIATE")
            locked = db.execute(
                """SELECT state,updated_at,markdown_path,raw_path,
                parent_doc_id,child_doc_id,raw_doc_id,classification_json
                FROM groups WHERE group_id=?""",
                (prepared.group_id,),
            ).fetchone()
            if (
                not content_migration_snapshot_matches(locked, prepared)
                or not content_migration_source_files_match(prepared)
            ):
                raise ContentMigrationCancelled(
                    "原子切换前检测到资料组已删除或发生更新"
                )
            if (
                prepared.final_parent_path == prepared.final_raw_path
                or prepared.final_parent_path.exists()
                or prepared.final_raw_path.exists()
            ):
                raise ContentMigrationCancelled(
                    "正式目标路径已被占用；拒绝覆盖现有Markdown"
                )
            collision = db.execute(
                """SELECT group_id FROM groups
                WHERE group_id<>?
                AND state NOT IN (
                    'user_delete_pending','user_deleted'
                )
                AND (
                    markdown_path IN (?,?)
                    OR raw_path IN (?,?)
                )
                LIMIT 1""",
                (
                    prepared.group_id,
                    str(prepared.final_parent_path),
                    str(prepared.final_raw_path),
                    str(prepared.final_parent_path),
                    str(prepared.final_raw_path),
                ),
            ).fetchone()
            if collision:
                raise ContentMigrationCancelled(
                    "正式目标路径已被其他资料组采用；拒绝覆盖"
                )
            prepared.final_parent_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            os.rename(
                prepared.parent_path,
                prepared.final_parent_path,
            )
            parent_new_placed = True
            write_content_migration_journal(
                prepared,
                cfg,
                "parent_file_placed",
                parent_doc_id,
                child_doc_id,
                raw_doc_id,
                parent_created,
                child_created,
                raw_created,
                parent_placed=True,
            )
            prepared.final_raw_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            os.rename(
                prepared.raw_path,
                prepared.final_raw_path,
            )
            raw_new_placed = True
            write_content_migration_journal(
                prepared,
                cfg,
                "files_placed",
                parent_doc_id,
                child_doc_id,
                raw_doc_id,
                parent_created,
                child_created,
                raw_created,
                parent_placed=True,
                raw_placed=True,
            )
            for doc_id, knowledge_base_id in cleanup_specs:
                if doc_id and doc_id not in {
                    parent_doc_id,
                    child_doc_id,
                    raw_doc_id,
                }:
                    enqueue_index_cleanup(
                        db,
                        prepared.group_id,
                        doc_id,
                        knowledge_base_id,
                        prepared.old_parent,
                        commit=False,
                    )
            for doc_id, knowledge_base_id in (
                (parent_doc_id, wc["parent_knowledge_base"]),
                (child_doc_id, wc["child_knowledge_base"]),
                (raw_doc_id, wc["raw_knowledge_base"]),
            ):
                db.execute(
                    """UPDATE index_cleanup_queue
                    SET state='adopted',
                        last_error='清理已取消：索引被本次迁移采用',
                        updated_at=?
                    WHERE doc_id=? AND knowledge_base_id=?""",
                    (int(time.time()), doc_id, knowledge_base_id),
                )
            save_group_state(
                db,
                prepared.group_id,
                prepared.group_name,
                "completed",
                prepared.final_parent_path,
                parent_doc_id,
                child_doc_id,
                classification=record,
                raw_path=prepared.final_raw_path,
                raw_doc_id=raw_doc_id,
                commit=False,
            )
            db.execute(
                """UPDATE files
                SET markdown_path=?,
                    weknora_doc_id=(
                        SELECT CASE
                            WHEN COUNT(DISTINCT g.parent_doc_id)=1
                            THEN MAX(g.parent_doc_id)
                            ELSE NULL
                        END
                        FROM group_files linked
                        JOIN groups g ON g.group_id=linked.group_id
                        WHERE linked.sha256=files.sha256
                        AND g.state NOT IN (
                            'user_delete_pending','user_deleted'
                        )
                        AND coalesce(g.parent_doc_id,'')!=''
                    ),
                    updated_at=?
                WHERE sha256 IN (
                    SELECT sha256 FROM group_files WHERE group_id=?
                )""",
                (
                    str(prepared.final_parent_path),
                    int(time.time()),
                    prepared.group_id,
                ),
            )
            if not content_migration_source_files_match(prepared):
                raise ContentMigrationCancelled(
                    "提交前检测到旧Markdown被删除或修改；"
                    "数据库与新文件均不会切换"
                )
            try:
                db.commit()
            except Exception as commit_exc:
                if db.in_transaction:
                    db.rollback()
                probe_db = db_open()
                try:
                    committed = probe_db.execute(
                        """SELECT state,markdown_path,raw_path,
                        parent_doc_id,child_doc_id,raw_doc_id
                        FROM groups WHERE group_id=?""",
                        (prepared.group_id,),
                    ).fetchone()
                finally:
                    probe_db.close()
                if (
                    not content_migration_committed_state_matches(
                        committed,
                        prepared,
                        parent_doc_id,
                        child_doc_id,
                        raw_doc_id,
                    )
                    or not content_migration_placed_files_match(prepared)
                ):
                    raise commit_exc
                print(
                    "数据库提交回执异常，但独立连接已确认切换成功："
                    f"{prepared.group_name}"
                )
            database_switched = True
            content_migration_journal_path(prepared).unlink(missing_ok=True)
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        cleanup_result = drain_index_cleanup_queue(
            db,
            cfg,
            group_id=prepared.group_id,
        )
        if cleanup_result["pending"]:
            print(
                "内容结构迁移：旧索引已进入持久重试队列｜"
                f"{prepared.group_name}｜"
                f"待重试{cleanup_result['pending']}"
            )
        if (
            prepared.old_parent.resolve()
            != prepared.final_parent_path.resolve()
        ):
            if (
                prepared.old_parent.is_file()
                and sha256(prepared.old_parent)
                == prepared.old_parent_sha256
            ):
                prepared.old_parent.unlink()
            elif prepared.old_parent.exists():
                print(
                    "旧父块在切换后发生变化，已保留且不会自动删除："
                    f"{prepared.old_parent}"
                )
        if (
            prepared.old_raw is not None
            and prepared.old_raw.resolve()
            != prepared.final_raw_path.resolve()
        ):
            if (
                prepared.old_raw.is_file()
                and sha256(prepared.old_raw)
                == prepared.old_raw_sha256
            ):
                prepared.old_raw.unlink()
            elif prepared.old_raw.exists():
                print(
                    "旧原文在切换后发生变化，已保留且不会自动删除："
                    f"{prepared.old_raw}"
                )
        print(
            f"内容结构迁移：{prepared.index}/{prepared.total}｜"
            f"{'章节' if prepared.document_mode == 'section' else '题目'}"
            f"{prepared.unit_count}｜"
            f"未匹配答案{prepared.unmatched_answers}｜"
            f"{prepared.group_name}"
        )
    except Exception:
        if not database_switched:
            for final, expected_hash, new_placed in (
                (
                    prepared.final_raw_path,
                    sha256(prepared.raw_path)
                    if prepared.raw_path.is_file()
                    else "",
                    raw_new_placed,
                ),
                (
                    prepared.final_parent_path,
                    sha256(prepared.parent_path)
                    if prepared.parent_path.is_file()
                    else "",
                    parent_new_placed,
                ),
            ):
                try:
                    if new_placed and final.is_file():
                        journal_payload = json.loads(
                            content_migration_journal_path(
                                prepared
                            ).read_text("utf-8")
                        )
                        layer_name = (
                            "raw"
                            if final == prepared.final_raw_path
                            else "parent"
                        )
                        journal_layer = next(
                            (
                                layer
                                for layer in journal_payload.get(
                                    "layers"
                                )
                                or []
                                if layer.get("name") == layer_name
                            ),
                            {},
                        )
                        expected_hash = str(
                            journal_layer.get("file_sha256")
                            or expected_hash
                        )
                        if (
                            not expected_hash
                            or sha256(final) != expected_hash
                        ):
                            raise RuntimeError(
                                f"新文件哈希不符，拒绝自动删除: {final}"
                            )
                        final.unlink()
                except Exception as rollback_exc:
                    preserve_job = True
                    print(
                        "内容结构迁移文件回滚失败，保留临时目录供恢复："
                        f"{prepared.group_name}｜{rollback_exc}"
                    )
            try:
                enqueue_new_migration_indexes_for_cleanup(
                    db,
                    cfg,
                    prepared,
                    parent_doc_id,
                    child_doc_id,
                    raw_doc_id,
                    parent_created,
                    child_created,
                    raw_created,
                )
                journal_path = content_migration_journal_path(prepared)
                unknown_upload = False
                if journal_path.is_file():
                    journal_payload = json.loads(
                        journal_path.read_text("utf-8")
                    )
                    unknown_upload = any(
                        bool(layer.get("upload_attempted"))
                        and not str(layer.get("doc_id") or "")
                        for layer in journal_payload.get("layers") or []
                    )
                if unknown_upload:
                    preserve_job = True
                else:
                    journal_path.unlink(missing_ok=True)
            except Exception as cleanup_exc:
                preserve_job = True
                try:
                    write_content_migration_journal(
                        prepared,
                        cfg,
                        "cleanup_pending",
                        parent_doc_id,
                        child_doc_id,
                        raw_doc_id,
                        parent_created,
                        child_created,
                        raw_created,
                        str(cleanup_exc),
                    )
                except Exception as journal_exc:
                    print(
                        "内容结构迁移日志写入失败，临时目录仍将保留："
                        f"{prepared.group_name}｜{journal_exc}"
                    )
                print(
                    "内容结构迁移新索引清理排队失败，保留旧索引："
                    f"{prepared.group_name}｜{cleanup_exc}"
                )
        raise
    finally:
        if (
            prepared.child_path
            and cfg["cleanup"]["delete_temporary_files"]
            and not preserve_job
        ):
            prepared.child_path.unlink(missing_ok=True)
        if (
            prepared.job.exists()
            and cfg["cleanup"]["delete_temporary_files"]
            and not preserve_job
        ):
            shutil.rmtree(prepared.job, ignore_errors=True)


def migrate_content_structure_serial(
    cfg: dict,
    db: sqlite3.Connection,
    limit: int = 0,
    group_ids: set[str] | None = None,
) -> None:
    """Rebuild historical rows into parent, child, and raw layers safely."""
    resumed_cleanup = drain_index_cleanup_queue(db, cfg, limit=100)
    if resumed_cleanup["completed"] or resumed_cleanup["pending"]:
        print(
            "旧索引持久清理恢复："
            f"完成{resumed_cleanup['completed']}｜"
            f"仍待重试{resumed_cleanup['pending']}"
        )
    rows = db.execute(
        """SELECT * FROM groups
        WHERE state='completed' AND markdown_path IS NOT NULL
        ORDER BY updated_at DESC"""
    ).fetchall()
    candidates = []
    for row in rows:
        if group_ids and row["group_id"] not in group_ids:
            continue
        path = Path(row["markdown_path"])
        if not path.is_file():
            continue
        metadata = markdown_frontmatter(path)
        if (
            int(metadata.get("content_structure_version") or 0)
            >= CONTENT_STRUCTURE_VERSION
            and int(metadata.get("child_chunk_version") or 0)
            >= CHILD_CHUNK_VERSION
            and row["raw_path"]
            and Path(row["raw_path"]).is_file()
            and row["raw_doc_id"]
        ):
            continue
        candidates.append(row)
    if limit > 0:
        candidates = candidates[:limit]
    if group_ids:
        found = {row["group_id"] for row in candidates}
        missing = sorted(group_ids - found)
        if missing:
            print(
                "内容结构迁移跳过不存在、未完成或已是新版的资料组："
                + "、".join(missing)
            )
    print(f"内容结构迁移：共{len(candidates)}组｜旧索引验证前不删除")
    for index, row in enumerate(candidates, 1):
        resources = cfg.get("resource_control") or {}
        resume_gb = max(
            float(resources.get("critical_windows_free_gb", 1.2)),
            float(resources.get("hard_pause_windows_free_gb", 0.65)),
        )
        memory_wait_reported = False
        while True:
            _, available_gb = windows_memory_gb()
            if available_gb >= resume_gb:
                break
            if not memory_wait_reported:
                print(
                    "内容结构迁移弹性暂停："
                    f"Windows可用内存{available_gb:.2f}GB｜"
                    f"恢复线{resume_gb:.2f}GB"
                )
                memory_wait_reported = True
            time.sleep(30)
        old_parent = Path(row["markdown_path"])
        old_parent_id = str(row["parent_doc_id"] or "")
        old_child_id = str(row["child_doc_id"] or "")
        old_raw_id = str(row["raw_doc_id"] or "")
        job = cfg["folders"]["work"] / "content-migration" / row["group_id"]
        parent_path: Path | None = None
        child_path: Path | None = None
        raw_path: Path | None = None
        try:
            try:
                classification = classification_from_dict(
                    json.loads(row["classification_json"] or "{}")
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                classification = classification_from_markdown(old_parent)
                if classification is None:
                    raise RuntimeError("缺少可用分类结果")
            members = db.execute(
                """SELECT gf.sha256,gf.source_path,f.batch_id,f.state,
                f.markdown_path,f.metrics_json
                FROM group_files gf
                JOIN files f ON f.sha256=gf.sha256
                WHERE gf.group_id=? ORDER BY gf.source_path""",
                (row["group_id"],),
            ).fetchall()
            if not members:
                raise RuntimeError("资料组没有成员记录")
            has_answer_source = any(
                ANSWER_FILE_RE.search(Path(member["source_path"]).stem)
                for member in members
            )
            require_original = (
                classification.document_type in {"教材", "讲义"}
                or has_answer_source
                or not parent_has_nonempty_answers(old_parent)
            )
            job.mkdir(parents=True, exist_ok=True)
            parsed = migration_parsed_inputs(
                row,
                list(members),
                cfg,
                job,
                require_original,
            )
            (
                parent_path,
                child_path,
                raw_path,
                unit_count,
                unmatched_answers,
                document_mode,
            ) = group_documents(
                row["group_id"],
                row["group_name"],
                parsed,
                cfg["folders"]["markdown"],
                cfg["folders"]["work"],
                int(cfg["pairing"]["child_chars"]),
                classification,
            )
            wc = cfg["weknora"]
            source_hint = Path(members[0]["source_path"])
            new_parent_id = weknora_find_existing(
                parent_path, source_hint, wc, wc["parent_knowledge_base"]
            ) or weknora_upload(
                parent_path,
                source_hint,
                wc,
                wc["parent_knowledge_base"],
                "parent",
                row["group_id"],
                classification,
            )
            new_child_id = weknora_find_existing(
                child_path, source_hint, wc, wc["child_knowledge_base"]
            ) or weknora_upload(
                child_path,
                source_hint,
                wc,
                wc["child_knowledge_base"],
                "child",
                row["group_id"],
                classification,
            )
            new_raw_id = weknora_find_existing(
                raw_path, source_hint, wc, wc["raw_knowledge_base"]
            ) or weknora_upload(
                raw_path,
                source_hint,
                wc,
                wc["raw_knowledge_base"],
                "raw",
                row["group_id"],
                classification,
            )
            verify_two_level_indexes(
                parent_path,
                child_path,
                source_hint,
                wc,
                new_parent_id,
                new_child_id,
                classification=classification,
                full_check=True,
                raw_path=raw_path,
                raw_doc_id=new_raw_id,
            )
            cleanup_specs = [
                (old_parent_id, wc["parent_knowledge_base"]),
                (old_child_id, wc["child_knowledge_base"]),
                (old_raw_id, wc["raw_knowledge_base"]),
            ]
            record = classification_to_dict(classification)
            record["content_structure_version"] = CONTENT_STRUCTURE_VERSION
            record["child_chunk_version"] = CHILD_CHUNK_VERSION
            record["content_structure_mode"] = document_mode
            record["unmatched_answers"] = unmatched_answers
            with db:
                for doc_id, knowledge_base_id in cleanup_specs:
                    if doc_id and doc_id not in {
                        new_parent_id,
                        new_child_id,
                        new_raw_id,
                    }:
                        enqueue_index_cleanup(
                            db,
                            str(row["group_id"]),
                            doc_id,
                            knowledge_base_id,
                            old_parent,
                            commit=False,
                        )
                save_group_state(
                    db,
                    row["group_id"],
                    row["group_name"],
                    "completed",
                    parent_path,
                    new_parent_id,
                    new_child_id,
                    classification=record,
                    raw_path=raw_path,
                    raw_doc_id=new_raw_id,
                    commit=False,
                )
                db.execute(
                    """UPDATE files
                    SET markdown_path=?,
                        weknora_doc_id=(
                            SELECT CASE
                                WHEN COUNT(DISTINCT g.parent_doc_id)=1
                                THEN MAX(g.parent_doc_id)
                                ELSE NULL
                            END
                            FROM group_files linked
                            JOIN groups g ON g.group_id=linked.group_id
                            WHERE linked.sha256=files.sha256
                            AND g.state NOT IN (
                                'user_delete_pending','user_deleted'
                            )
                            AND coalesce(g.parent_doc_id,'')!=''
                        ),
                        updated_at=?
                    WHERE sha256 IN (
                        SELECT sha256 FROM group_files WHERE group_id=?
                    )""",
                    (
                        str(parent_path),
                        int(time.time()),
                        row["group_id"],
                    ),
                )
            cleanup_result = drain_index_cleanup_queue(
                db,
                cfg,
                group_id=str(row["group_id"]),
            )
            if cleanup_result["pending"]:
                print(
                    "内容结构迁移：旧索引已进入持久重试队列｜"
                    f"{row['group_name']}｜"
                    f"待重试{cleanup_result['pending']}"
                )
            if old_parent.resolve() != parent_path.resolve():
                old_parent.unlink(missing_ok=True)
            print(
                f"内容结构迁移：{index}/{len(candidates)}｜"
                f"{'章节' if document_mode == 'section' else '题目'}{unit_count}｜"
                f"未匹配答案{unmatched_answers}｜{row['group_name']}"
            )
        except Exception as exc:
            print(
                f"内容结构迁移保留旧索引等待重试："
                f"{row['group_name']}：{exc}"
            )
        finally:
            if child_path and cfg["cleanup"]["delete_temporary_files"]:
                child_path.unlink(missing_ok=True)
            if job.exists() and cfg["cleanup"]["delete_temporary_files"]:
                shutil.rmtree(job, ignore_errors=True)


def migrate_content_structure(
    cfg: dict,
    db: sqlite3.Connection,
    limit: int = 0,
    group_ids: set[str] | None = None,
) -> None:
    """Run a one-item preparation pipeline ahead of serialized publishing."""
    resources = cfg.get("resource_control") or {}
    pipeline_enabled = bool(
        resources.get("content_migration_pipeline_enabled", True)
    )
    recovered = recover_content_migration_journals(cfg, db)
    if recovered["journals"] or recovered["errors"]:
        print(
            "内容结构迁移中断恢复："
            f"日志{recovered['journals']}｜"
            f"清理排队{recovered['queued']}｜"
            f"已被活动资料采用{recovered['adopted']}｜"
            f"错误{recovered['errors']}"
        )
    resumed_cleanup = drain_index_cleanup_queue(db, cfg, limit=100)
    if resumed_cleanup["completed"] or resumed_cleanup["pending"]:
        print(
            "旧索引持久清理恢复："
            f"完成{resumed_cleanup['completed']}｜"
            f"仍待重试{resumed_cleanup['pending']}"
        )
    rows = db.execute(
        """SELECT * FROM groups
        WHERE state='completed' AND markdown_path IS NOT NULL
        ORDER BY updated_at DESC"""
    ).fetchall()
    candidates: list[dict] = []
    for row in rows:
        if group_ids and row["group_id"] not in group_ids:
            continue
        path = Path(row["markdown_path"])
        if not path.is_file():
            continue
        metadata = markdown_frontmatter(path)
        if (
            int(metadata.get("content_structure_version") or 0)
            >= CONTENT_STRUCTURE_VERSION
            and int(metadata.get("child_chunk_version") or 0)
            >= CHILD_CHUNK_VERSION
            and row["raw_path"]
            and Path(row["raw_path"]).is_file()
            and row["raw_doc_id"]
        ):
            continue
        candidates.append(dict(row))
    if limit > 0:
        candidates = candidates[:limit]
    if group_ids:
        found = {str(row["group_id"]) for row in candidates}
        missing = sorted(group_ids - found)
        if missing:
            print(
                "内容结构迁移跳过不存在、未完成或已是新版的资料组："
                + "、".join(missing)
            )
    total = len(candidates)
    print(
        f"内容结构迁移：共{total}组｜"
        "单格准备流水线｜旧索引验证前不删除"
    )
    if not candidates:
        set_content_migration_status(db, "completed", 0, 0)
        return
    migration_success = 0
    migration_skipped = 0
    migration_failed = 0

    def submit_prepare(
        executor: ThreadPoolExecutor,
        position: int,
        prefetch_release: threading.Event | None = None,
    ) -> Future:
        row = candidates[position]
        set_content_migration_status(
            db,
            "running",
            position,
            total,
            str(row["group_name"] or ""),
            "准备MinerU结果、补图和父子原文",
        )
        return executor.submit(
            prepare_content_migration,
            cfg,
            row,
            position + 1,
            total,
            prefetch_release,
        )

    with ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="migration-prepare",
    ) as executor:
        current_future = submit_prepare(executor, 0)
        for position, row in enumerate(candidates):
            prepared: PreparedContentMigration | None = None
            try:
                prepared = current_future.result()
            except ContentMigrationCancelled as exc:
                migration_skipped += 1
                print(
                    "内容结构迁移跳过已变化资料组："
                    f"{row['group_name']}｜{exc}"
                )
            except Exception as exc:
                migration_failed += 1
                print(
                    "内容结构迁移保留旧索引等待重试："
                    f"{row['group_name']}：{exc}"
                )
            if prepared is None:
                failed_job = (
                    cfg["folders"]["work"]
                    / "content-migration"
                    / str(row["group_id"])
                )
                if (
                    failed_job.exists()
                    and cfg["cleanup"]["delete_temporary_files"]
                    and not (failed_job / "migration-journal.json").exists()
                    and not (failed_job / "rollback-parent.md").exists()
                    and not (failed_job / "rollback-raw.md").exists()
                ):
                    shutil.rmtree(failed_job, ignore_errors=True)

            next_future: Future | None = None
            next_release: threading.Event | None = None
            next_position = position + 1
            if (
                next_position < total
                and pipeline_enabled
                and content_migration_prefetch_allowed(cfg)
            ):
                next_release = threading.Event()
                next_future = submit_prepare(
                    executor,
                    next_position,
                    next_release,
                )
                print(
                    "内容结构迁移流水线："
                    "当前组发布时预备下一组｜缓冲上限1组"
                )

            try:
                if prepared is not None:
                    set_content_migration_status(
                        db,
                        "running",
                        prepared.index,
                        total,
                        prepared.group_name,
                        "WeKnora三层上传、验证和旧索引清理",
                    )
                    try:
                        publish_content_migration(prepared, cfg, db)
                    except ContentMigrationCancelled as exc:
                        migration_skipped += 1
                        print(
                            "内容结构迁移发布已取消："
                            f"{prepared.group_name}｜{exc}"
                        )
                    except Exception as exc:
                        migration_failed += 1
                        print(
                            "内容结构迁移保留旧索引等待重试："
                            f"{prepared.group_name}：{exc}"
                        )
                    else:
                        migration_success += 1
            finally:
                if next_release is not None:
                    next_release.set()

            if next_position < total and next_future is None:
                next_future = submit_prepare(executor, next_position)
            if next_future is not None:
                current_future = next_future

    final_cleanup = drain_index_cleanup_queue(db, cfg, limit=500)
    final_state = "partial" if migration_failed else "completed"
    set_content_migration_status(
        db,
        final_state,
        total,
        total,
        action=(
            f"成功{migration_success}｜"
            f"跳过{migration_skipped}｜"
            f"失败待重试{migration_failed}"
        ),
    )
    print(
        "内容结构迁移汇总："
        f"成功{migration_success}｜"
        f"跳过{migration_skipped}｜"
        f"失败待重试{migration_failed}"
    )
    if final_cleanup["pending"]:
        print(
            "内容结构迁移结束："
            f"旧索引仍有{final_cleanup['pending']}个等待后续重试"
        )


def classified_parent_body(
    path: Path, classification: DocumentClassification
) -> str:
    body = markdown_body(path)
    body = re.sub(r"(?m)^分类：[^\n]*\n?", "", body)
    line = classification_line(classification)
    body, count = re.subn(
        r"(?m)^(资料：[^\n]*)$", rf"\1\n{line}", body
    )
    if count == 0:
        body, count = re.subn(
            r"(?m)^(# [^\n]+)$", rf"\1\n{line}", body
        )
    if count == 0:
        body = line + "\n\n" + body
    return body.strip() + "\n"


def classification_migration_journal_path(cfg: dict, group_id: str) -> Path:
    token = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:24]
    return (
        cfg["folders"]["work"]
        / "classification-migration"
        / f"{token}.json"
    )


def write_classification_migration_journal(
    cfg: dict,
    row: sqlite3.Row,
    old_path: Path,
    target: Path,
    classification: DocumentClassification,
    record: dict,
    target_text: str,
) -> Path:
    """Persist the filesystem move intent before changing the live path."""
    path = classification_migration_journal_path(cfg, str(row["group_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "group_id": str(row["group_id"]),
        "group_name": str(row["group_name"] or row["group_id"]),
        "expected_updated_at": int(row["updated_at"] or 0),
        "old_path": str(old_path),
        "target_path": str(target),
        "old_sha256": stable_sha256(old_path),
        "target_sha256": atomic_write_text_sha256(target_text),
        "classification": classification_to_dict(classification),
        "classification_record": record,
        "created_at": int(time.time()),
    }
    atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return path


def recover_classification_migration_journals(
    cfg: dict,
    db: sqlite3.Connection,
) -> dict[str, int]:
    """Repair a Markdown move interrupted before the SQLite path switch."""
    root = cfg["folders"]["work"] / "classification-migration"
    result = {"journals": 0, "recovered": 0, "cleared": 0, "errors": 0}
    if not root.is_dir():
        return result
    markdown_root = cfg["folders"]["markdown"].resolve()

    def checked_markdown_path(value: object) -> Path:
        candidate = Path(str(value or ""))
        resolved = candidate.resolve()
        if resolved == markdown_root or markdown_root not in resolved.parents:
            raise RuntimeError(
                f"分类迁移日志路径越出Markdown目录: {candidate}"
            )
        return candidate

    def same_path(first: Path, second: Path) -> bool:
        return str(first.resolve()).casefold() == str(second.resolve()).casefold()

    for journal in sorted(root.glob("*.json")):
        result["journals"] += 1
        try:
            payload = json.loads(journal.read_text("utf-8"))
            group_id = str(payload.get("group_id") or "").strip()
            if not group_id:
                raise RuntimeError("分类迁移日志缺少group_id")
            old_path = checked_markdown_path(payload.get("old_path"))
            target = checked_markdown_path(payload.get("target_path"))
            if same_path(old_path, target):
                raise RuntimeError("分类迁移日志的新旧路径相同")
            old_digest = str(payload.get("old_sha256") or "")
            target_digest = str(payload.get("target_sha256") or "")
            if not old_digest or not target_digest:
                raise RuntimeError("分类迁移日志缺少文件摘要")
            row = db.execute(
                "SELECT * FROM groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("分类迁移日志对应资料组不存在")
            if str(row["state"] or "") in PERMANENT_GROUP_EXCLUSION_STATES:
                raise RuntimeError("资料组已永久排除，拒绝恢复旧迁移")
            row_path = Path(str(row["markdown_path"] or ""))
            row_is_old = same_path(row_path, old_path)
            row_is_target = same_path(row_path, target)
            if not row_is_old and not row_is_target:
                raise RuntimeError("资料组Markdown路径已被其他操作修改")
            expected_updated_at = int(payload.get("expected_updated_at") or 0)
            if row_is_old and int(row["updated_at"] or 0) != expected_updated_at:
                raise RuntimeError("资料组在分类迁移中断后已被其他操作更新")
            if row_is_target and str(row["state"] or "") != "classification_migrating":
                raise RuntimeError("资料组已越过本地分类迁移阶段，拒绝回退状态")
            old_exists = old_path.is_file()
            target_exists = target.is_file()
            if old_exists and target_exists:
                raise RuntimeError("分类迁移新旧Markdown同时存在，拒绝猜测")
            if old_exists:
                if not row_is_old or stable_sha256(old_path) != old_digest:
                    raise RuntimeError("原Markdown路径或摘要已变化")
                journal.unlink()
                result["cleared"] += 1
                continue
            if not target_exists:
                raise RuntimeError("分类迁移的新旧Markdown均不存在")
            classification = classification_from_dict(
                payload.get("classification") or {}
            )
            current_digest = stable_sha256(target)
            if current_digest == old_digest:
                recovered_text = classification_frontmatter(
                    group_id,
                    str(payload.get("group_name") or row["group_name"]),
                    classification,
                    "parent",
                ) + classified_parent_body(target, classification)
                recovered_digest = atomic_write_text_sha256(recovered_text)
                if recovered_digest != target_digest:
                    raise RuntimeError("分类迁移日志无法重建目标Markdown摘要")
                atomic_write(target, recovered_text)
                current_digest = stable_sha256(target)
            if current_digest != target_digest:
                raise RuntimeError("分类迁移目标Markdown摘要不匹配")
            record = payload.get("classification_record") or {}
            if not isinstance(record, dict):
                raise RuntimeError("分类迁移日志的分类记录无效")
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE files SET markdown_path=? WHERE markdown_path=?",
                    (str(target), str(old_path)),
                )
                save_group_state(
                    db,
                    group_id,
                    str(row["group_name"] or group_id),
                    "classification_migrating",
                    target,
                    row["parent_doc_id"],
                    row["child_doc_id"],
                    classification=record,
                    raw_path=(
                        Path(row["raw_path"]) if row["raw_path"] else None
                    ),
                    raw_doc_id=row["raw_doc_id"],
                    commit=False,
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            journal.unlink()
            result["recovered"] += 1
        except Exception as exc:
            result["errors"] += 1
            print(f"分类迁移中断日志保留等待恢复：{journal}｜{exc}")
    return result


def migrate_classified_markdown(
    cfg: dict,
    db: sqlite3.Connection,
    dry_run: bool,
    delete_old_indexes: bool,
) -> None:
    if not dry_run:
        recovered = recover_classification_migration_journals(cfg, db)
        if recovered["journals"]:
            print(
                "分类迁移中断恢复："
                f"日志{recovered['journals']}｜"
                f"恢复{recovered['recovered']}｜"
                f"未执行清理{recovered['cleared']}｜"
                f"错误{recovered['errors']}"
            )
    rows = db.execute(
        """SELECT * FROM groups
        WHERE markdown_path IS NOT NULL AND state != 'failed'
        ORDER BY updated_at"""
    ).fetchall()
    candidates = [
        row for row in rows if Path(row["markdown_path"]).is_file()
    ]
    totals = {
        "rule": 0,
        "mimo": 0,
        "local": 0,
        "pending": 0,
        "other": 0,
        "completed": 0,
        "failed": 0,
    }
    print(
        f"分类迁移{'预览' if dry_run else '执行'}：共{len(candidates)}组｜"
        "不重新调用MinerU"
    )
    for index, row in enumerate(candidates, 1):
        old_path = Path(row["markdown_path"])
        existing_record = {}
        try:
            existing_record = json.loads(row["classification_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        current_version = int(
            cfg["document_classification"]["taxonomy"].get("version")
            or cfg["document_classification"].get("version")
            or 1
        )
        if (
            not dry_run
            and existing_record.get("migration_phase") == "completed"
            and int(
                existing_record.get("classification_version") or 0
            ) == current_version
        ):
            totals["completed"] += 1
            print(
                f"分类实施进度："
                f"{round(index * 100 / max(1, len(candidates)))}%｜"
                f"已分类：{index}/{len(candidates)}｜已完成，跳过："
                f"{row['group_name']}"
            )
            continue
        source_rows = db.execute(
            "SELECT sha256,source_path FROM group_files WHERE group_id=?",
            (row["group_id"],),
        ).fetchall()
        sources = [Path(item["source_path"]) for item in source_rows]
        source_hint = sources[0] if sources else old_path
        classification = classify_group(
            row["group_name"],
            [(source_hint, old_path)],
            cfg,
            row["classification_json"],
        )
        if "mimo" in classification.method:
            totals["mimo"] += 1
        elif "local" in classification.method:
            totals["local"] += 1
        else:
            totals["rule"] += 1
        if classification.document_type == "待分类":
            totals["pending"] += 1
        if classification.document_type == "其他资料":
            totals["other"] += 1
        destination = classification_directory(
            cfg["folders"]["markdown"], classification, create=not dry_run
        )
        display = safe_path_component(row["group_name"], "题库资料")
        target = destination / (
            f"{display}-{row['group_id']}-分类v{classification.version}.md"
        )
        print(
            f"分类实施进度：{round(index * 100 / max(1, len(candidates)))}%｜"
            f"已分类：{index}/{len(candidates)}｜"
            f"{classification_line(classification)}｜{target}"
        )
        if dry_run:
            continue

        markdown_root = cfg["folders"]["markdown"].resolve()
        for label, candidate in (("原", old_path), ("目标", target)):
            resolved = candidate.resolve()
            if resolved == markdown_root or markdown_root not in resolved.parents:
                raise RuntimeError(
                    f"分类迁移{label}Markdown越出配置目录: {candidate}"
                )

        legacy_parent_id = (
            existing_record.get("legacy_parent_doc_id")
            or row["parent_doc_id"]
        )
        legacy_child_id = (
            existing_record.get("legacy_child_doc_id")
            or row["child_doc_id"]
        )
        record = classification_to_dict(classification)
        record.update(
            {
                "legacy_parent_doc_id": legacy_parent_id,
                "legacy_child_doc_id": legacy_child_id,
                "migration_phase": "classified_local",
            }
        )
        old_markdown_digest = stable_sha256(old_path)
        body = classified_parent_body(old_path, classification)
        if stable_sha256(old_path) != old_markdown_digest:
            raise RuntimeError("分类期间Markdown内容发生变化，拒绝迁移")
        target_text = classification_frontmatter(
            row["group_id"],
            row["group_name"],
            classification,
            "parent",
        ) + body
        move_journal = None
        try:
            db.execute("BEGIN IMMEDIATE")
            fresh = db.execute(
                "SELECT * FROM groups WHERE group_id=?",
                (row["group_id"],),
            ).fetchone()
            guarded_fields = (
                "state",
                "markdown_path",
                "parent_doc_id",
                "child_doc_id",
                "raw_path",
                "raw_doc_id",
                "classification_json",
                "updated_at",
            )
            if fresh is None or any(
                str(fresh[field] or "") != str(row[field] or "")
                for field in guarded_fields
            ):
                raise RuntimeError(
                    "资料组在分类计算期间已被其他操作更新，拒绝覆盖"
                )
            if stable_sha256(old_path) != old_markdown_digest:
                raise RuntimeError("分类提交前Markdown内容发生变化，拒绝迁移")
            if old_path.resolve() != target.resolve():
                if target.exists():
                    raise RuntimeError(f"分类目标已存在，拒绝覆盖: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                move_journal = write_classification_migration_journal(
                    cfg,
                    row,
                    old_path,
                    target,
                    classification,
                    record,
                    target_text,
                )
                os.replace(old_path, target)
            atomic_write(target, target_text)
            db.execute(
                "UPDATE files SET markdown_path=? WHERE markdown_path=?",
                (str(target), str(old_path)),
            )
            save_group_state(
                db,
                row["group_id"],
                row["group_name"],
                "classification_migrating",
                target,
                row["parent_doc_id"],
                row["child_doc_id"],
                classification=record,
                commit=False,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        if move_journal is not None:
            move_journal.unlink()

        child_path = child_index_from_parent(
            target,
            cfg["folders"]["work"],
            int(cfg["pairing"]["child_chars"]),
        )
        try:
            wc = cfg["weknora"]
            if classification.document_type in {"待分类", "其他资料"}:
                if delete_old_indexes:
                    weknora_delete(
                        legacy_parent_id,
                        target,
                        wc,
                        wc["parent_knowledge_base"],
                    )
                    weknora_delete(
                        legacy_child_id,
                        target,
                        wc,
                        wc["child_knowledge_base"],
                    )
                final_state = (
                    "classification_pending"
                    if classification.document_type == "待分类"
                    else "excluded_completed"
                )
                if classification.document_type == "其他资料":
                    markdown_digest = stable_sha256(target)
                    record["markdown_sha256"] = markdown_digest
                    other_cleanup_errors = []
                    for item in source_rows:
                        source = Path(item["source_path"])
                        digest = str(item["sha256"] or "")
                        current = db.execute(
                            "SELECT * FROM files WHERE sha256=?",
                            (digest,),
                        ).fetchone()
                        if source.exists() and cfg[
                            "document_classification"
                        ].get("delete_other_source_after_markdown", False):
                            try:
                                delete_source_with_audit(
                                    db,
                                    source,
                                    digest,
                                    target,
                                    "分类迁移后确认不入RAG的其他资料",
                                    row["group_id"],
                                )
                                file_state = "excluded_completed"
                                file_error = (
                                    "未入RAG；"
                                    f"markdown_sha256={markdown_digest}"
                                )
                            except (OSError, RuntimeError) as cleanup_error:
                                file_state = "excluded_cleanup_pending"
                                file_error = (
                                    "其他资料源文件等待重试删除: "
                                    f"{cleanup_error}"
                                )
                                other_cleanup_errors.append(
                                    f"{source.name}: {cleanup_error}"
                                )
                        else:
                            file_state = "excluded_completed"
                            file_error = (
                                "未入RAG；"
                                f"markdown_sha256={markdown_digest}"
                            )
                        save_state(
                            db,
                            digest,
                            source,
                            file_state,
                            current["batch_id"] if current else None,
                            str(target),
                            error=file_error,
                            metrics=file_metrics(current),
                        )
                record["migration_phase"] = (
                    "completed"
                    if delete_old_indexes
                    or not (legacy_parent_id or legacy_child_id)
                    else "old_indexes_pending_delete"
                )
                save_group_state(
                    db,
                    row["group_id"],
                    row["group_name"],
                    (
                        "excluded_cleanup_pending"
                        if classification.document_type == "其他资料"
                        and other_cleanup_errors
                        else final_state
                    ),
                    target,
                    "",
                    "",
                    (
                        "; ".join(other_cleanup_errors)
                        if classification.document_type == "其他资料"
                        else ""
                    ),
                    classification=record,
                )
                totals["completed"] += 1
                continue

            parent_doc_id = weknora_find_existing(
                target,
                source_hint,
                wc,
                wc["parent_knowledge_base"],
            ) or weknora_upload(
                target,
                source_hint,
                wc,
                wc["parent_knowledge_base"],
                "parent",
                row["group_id"],
                classification,
            )
            child_doc_id = weknora_find_existing(
                child_path,
                source_hint,
                wc,
                wc["child_knowledge_base"],
            ) or weknora_upload(
                child_path,
                source_hint,
                wc,
                wc["child_knowledge_base"],
                "child",
                row["group_id"],
                classification,
            )
            verify_two_level_indexes(
                target,
                child_path,
                source_hint,
                wc,
                parent_doc_id,
                child_doc_id,
            )
            record["migration_phase"] = "new_indexes_verified"
            save_group_state(
                db,
                row["group_id"],
                row["group_name"],
                "verified",
                target,
                parent_doc_id,
                child_doc_id,
                classification=record,
            )
            if delete_old_indexes:
                if legacy_parent_id and legacy_parent_id != parent_doc_id:
                    weknora_delete(
                        legacy_parent_id,
                        target,
                        wc,
                        wc["parent_knowledge_base"],
                    )
                if legacy_child_id and legacy_child_id != child_doc_id:
                    weknora_delete(
                        legacy_child_id,
                        target,
                        wc,
                        wc["child_knowledge_base"],
                    )
                record["migration_phase"] = "completed"
            else:
                record["migration_phase"] = "old_indexes_pending_delete"
            cleanup_errors = cleanup_verified_sources(
                sources,
                target,
                parent_doc_id,
                row["group_id"],
                cfg,
                db,
            )
            save_group_state(
                db,
                row["group_id"],
                row["group_name"],
                "cleanup_pending" if cleanup_errors else "completed",
                target,
                parent_doc_id,
                child_doc_id,
                "; ".join(cleanup_errors),
                classification=record,
            )
            totals["completed"] += 1
        except Exception as exc:
            totals["failed"] += 1
            record["migration_phase"] = "retry_wait"
            save_group_state(
                db,
                row["group_id"],
                row["group_name"],
                "classification_migrating",
                target,
                classification=record,
                error=f"分类迁移等待重试: {exc}",
            )
            print(
                f"分类迁移单组失败并保留，继续下一组："
                f"{row['group_name']}：{exc}"
            )
        finally:
            if cfg["cleanup"]["delete_temporary_files"]:
                child_path.unlink(missing_ok=True)
    print(
        "分类迁移完成："
        f"规则确定{totals['rule']}｜MiMo兜底{totals['mimo']}｜"
        f"本地文本0.6B分类备用{totals['local']}｜"
        f"待分类并保留{totals['pending']}｜其他资料{totals['other']}｜"
        f"已执行{totals['completed']}｜等待重试{totals['failed']}"
    )
    if not dry_run:
        db.execute(
            """INSERT INTO metadata(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (
                "classification_implementation_percent",
                "100" if totals["failed"] == 0 else "95",
            ),
        )
        db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        help="配置文件路径；默认使用config.local.yaml或QUESTION_BANK_CONFIG",
    )
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--classify-only", action="store_true")
    parser.add_argument(
        "--prequeue-only",
        action="store_true",
        help="只把可解析文件提交到MinerU队列，不消费结果、不合并、不入库",
    )
    parser.add_argument(
        "--migrate-indexes",
        action="store_true",
        help="把已有Markdown迁移到当前三层知识库，不重新调用MinerU",
    )
    parser.add_argument(
        "--migrate-classification",
        action="store_true",
        help="迁移现有最终Markdown到分类目录并重建三层索引，不调用MinerU",
    )
    parser.add_argument(
        "--migrate-content-structure",
        action="store_true",
        help="把历史资料安全迁移为父块、子块和原文三层；不重新提交MinerU",
    )
    parser.add_argument(
        "--classification-dry-run",
        action="store_true",
        help="只预览现有最终Markdown分类，不移动、不上传、不删除",
    )
    parser.add_argument(
        "--delete-old-indexes",
        action="store_true",
        help="分类版三层索引验证后永久删除对应旧WeKnora文档",
    )
    parser.add_argument(
        "--recover-missing-sources",
        action="store_true",
        help="只从已存在的MinerU批次恢复历史缺源Markdown，不重新提交",
    )
    parser.add_argument(
        "--index-recovered",
        action="store_true",
        help="合并并入库已经从MinerU恢复的历史缺源Markdown",
    )
    parser.add_argument(
        "--repair-state",
        action="store_true",
        help="删除无对应资料组的孤立关系，并标记历史缺源记录",
    )
    parser.add_argument(
        "--sync-manual-deletions",
        help="按删除前后差异JSON同步本地文件、三层索引和永久排除状态",
    )
    parser.add_argument(
        "--detect-manual-deletions",
        nargs="?",
        const=str(ROOT / "outputs" / "manual-deletion-current.json"),
        help=(
            "直接核对当前state.db与文件系统并生成删除差异JSON；"
            "可选指定输出路径"
        ),
    )
    parser.add_argument(
        "--manual-deletion-dry-run",
        action="store_true",
        help="只统计手动删除同步范围，不执行删除",
    )
    parser.add_argument(
        "--recovery-limit",
        type=int,
        default=0,
        help="本次最多恢复或入库多少份；0表示全部",
    )
    parser.add_argument(
        "--migration-group",
        action="append",
        default=[],
        help="只迁移指定group_id；可重复提供，仅用于内容结构迁移",
    )
    parser.add_argument(
        "--search",
        help="同时检索父块、子块和原文层，全部使用向量＋BM25",
    )
    args = parser.parse_args()
    for option, value in (
        ("--search", args.search),
        ("--sync-manual-deletions", args.sync_manual_deletions),
        ("--detect-manual-deletions", args.detect_manual_deletions),
    ):
        if value is not None and not str(value).strip():
            parser.error(f"{option} requires a non-empty value")
    primary_modes = {
        "--supervise": args.supervise,
        "--worker": args.worker,
        "--status": args.status,
        "--classify-only": args.classify_only,
        "--prequeue-only": args.prequeue_only,
        "--migrate-indexes": args.migrate_indexes,
        "--migrate-classification": args.migrate_classification,
        "--migrate-content-structure": args.migrate_content_structure,
        "--classification-dry-run": args.classification_dry_run,
        "--recover-missing-sources": args.recover_missing_sources,
        "--index-recovered": args.index_recovered,
        "--repair-state": args.repair_state,
        "--detect-manual-deletions": args.detect_manual_deletions is not None,
        "--sync-manual-deletions": args.sync_manual_deletions is not None,
        "--search": args.search is not None,
    }
    selected_modes = [name for name, selected in primary_modes.items() if selected]
    if len(selected_modes) > 1:
        parser.error(
            "choose only one primary operation: " + ", ".join(selected_modes)
        )
    if args.manual_deletion_dry_run and args.sync_manual_deletions is None:
        parser.error(
            "--manual-deletion-dry-run requires --sync-manual-deletions"
        )
    if args.delete_old_indexes and not args.migrate_classification:
        parser.error("--delete-old-indexes requires --migrate-classification")
    if args.migration_group and not args.migrate_content_structure:
        parser.error("--migration-group requires --migrate-content-structure")
    cfg = load_settings(args.config)
    if args.supervise and not args.worker:
        supervise_ingest(cfg, args.config)
        return
    db = db_open()
    reconcile_appledouble_history(db)
    if args.detect_manual_deletions is not None:
        with single_instance(".mutation.lock"), single_instance(
            ".manual-deletion-detect.lock"
        ):
            result = detect_manual_deletions(
                cfg,
                db,
                Path(args.detect_manual_deletions),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        db.close()
        return
    if args.sync_manual_deletions is not None:
        with single_instance(".mutation.lock"), single_instance(
            ".manual-deletion-sync.lock"
        ):
            result = sync_manual_deletions(
                cfg,
                db,
                Path(args.sync_manual_deletions),
                dry_run=args.manual_deletion_dry_run,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        db.close()
        return
    if args.repair_state:
        with single_instance(".mutation.lock"), single_instance(
            ".state-repair.lock"
        ):
            result = repair_stale_state_metadata(db)
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.migrate_content_structure:
        with single_instance(".mutation.lock"), single_instance(
            ".content-migration.lock"
        ):
            migrate_content_structure(
                cfg,
                db,
                max(0, args.recovery_limit),
                set(args.migration_group),
            )
        return
    if args.recover_missing_sources:
        db.close()
        with single_instance(".mutation.lock"), single_instance(
            ".source-recovery.lock"
        ):
            result = recover_missing_source_batches(
                cfg, max(0, args.recovery_limit)
            )
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.index_recovered:
        db.close()
        with single_instance(".mutation.lock"), single_instance(
            ".ingest.lock"
        ):
            result = process_recovered_groups(
                cfg, max(0, args.recovery_limit)
            )
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.search is not None:
        results = parallel_hybrid_search(args.search, cfg["weknora"])
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if args.status:
        counts = {
            row[0]: row[1]
            for row in db.execute("SELECT state,count(*) FROM files GROUP BY state")
        }
        completed = counts.get("completed", 0) + counts.get(
            "excluded_completed", 0
        )
        audited_deletions = db.execute(
            "SELECT count(*) FROM deletion_audit WHERE success=1"
        ).fetchone()[0]
        failed = counts.get("failed", 0)
        source_missing = counts.get("source_missing", 0)
        user_deleted = counts.get("user_deleted", 0)
        inventory = inbox_inventory(cfg)
        active = sum(
            counts.get(state, 0)
            for state in (
                "submitting", "parsing", "parsed", "indexing",
                "verified", "cleanup_pending", "excluded_cleanup_pending",
                "source_missing_recovered",
            )
        )
        retained = counts.get("classification_pending", 0)
        queued = counts.get("queued", 0)
        processed = (
            completed + failed + retained + source_missing + user_deleted
        )
        total = sum(counts.values())
        percent = round(processed * 100 / total, 1) if total else 0.0
        successful_percent = (
            round(completed * 100 / total, 1) if total else 0.0
        )
        metric_rows = db.execute(
            """SELECT metrics_json FROM files
            WHERE state IN ('completed','failed')
            ORDER BY updated_at DESC LIMIT 100"""
        ).fetchall()
        recorded = [file_metrics(row) for row in metric_rows]
        recent_durations = sorted(
            float(item["active_processing_seconds"])
            for item in recorded
            if float(item.get("active_processing_seconds") or 0) > 0
            and int(item.get("finished_at") or 0) > 0
        )
        median_total = (
            round(
                (
                    recent_durations[len(recent_durations) // 2]
                    if len(recent_durations) % 2
                    else (
                        recent_durations[len(recent_durations) // 2 - 1]
                        + recent_durations[len(recent_durations) // 2]
                    )
                    / 2
                ),
                1,
            )
            if recent_durations
            else 0
        )
        p90_total = (
            round(
                recent_durations[
                    min(
                        len(recent_durations) - 1,
                        max(0, int(len(recent_durations) * 0.9) - 1),
                    )
                ],
                1,
            )
            if recent_durations
            else 0
        )
        current_row = None
        for candidate in db.execute(
            """SELECT source_path,state FROM files
            WHERE state NOT IN (
                'completed','excluded_completed','failed',
                'classification_pending','user_delete_pending','user_deleted'
            )
            ORDER BY updated_at DESC LIMIT 100"""
        ).fetchall():
            if Path(candidate["source_path"]).exists():
                current_row = candidate
                break
        current_file = Path(current_row["source_path"]).name if current_row else "无"
        current_state = current_row["state"] if current_row else ""
        action_by_state = {
            "queued": "等待处理",
            "submitting": "MinerU提交",
            "parsing": "MinerU解析",
            "parsed": "等待题目答案合并",
            "indexing": "WeKnora父块、子块和原文三层入库",
            "verified": "检索确认后清理",
            "cleanup_pending": "重试删除源文件",
            "classification_pending": "分类证据不足，保留源文件",
            "excluded_cleanup_pending": "其他资料重试删除源文件",
            "user_delete_pending": "同步用户手动删除",
            "user_deleted": "用户永久排除",
        }
        current_action = action_by_state.get(current_state, current_state or "无")
        migration_status: dict = {}
        migration_row = db.execute(
            """SELECT value FROM metadata
            WHERE key='content_migration_status'"""
        ).fetchone()
        if migration_row:
            try:
                migration_status = json.loads(migration_row["value"])
            except (json.JSONDecodeError, TypeError):
                migration_status = {}
        migration_running = (
            migration_status.get("state") == "running"
            and int(time.time())
            - int(migration_status.get("updated_at") or 0)
            < 1200
        )
        if migration_running:
            active += 1
            if current_file == "无":
                current_file = str(
                    migration_status.get("group_name") or "历史迁移资料组"
                )
                current_action = str(
                    migration_status.get("action") or "历史三层迁移"
                )
        classification_rows = db.execute(
            "SELECT state,classification_json FROM groups "
            "WHERE classification_json IS NOT NULL "
            "AND state NOT IN ('user_delete_pending','user_deleted')"
        ).fetchall()
        indexing_groups = db.execute(
            "SELECT count(*) FROM groups WHERE state='indexing'"
        ).fetchone()[0]
        raw_indexed_groups = db.execute(
            "SELECT count(*) FROM groups "
            "WHERE state='completed' AND raw_doc_id IS NOT NULL"
        ).fetchone()[0]
        completed_groups = db.execute(
            "SELECT count(*) FROM groups WHERE state='completed'"
        ).fetchone()[0]
        pending_group_rows = db.execute(
            """SELECT error FROM groups
            WHERE state='classification_pending'"""
        ).fetchall()
        no_content_pending = sum(
            "题目正文" in str(row["error"] or "")
            and (
                "没有识别" in str(row["error"] or "")
                or "没有可安全识别" in str(row["error"] or "")
            )
            for row in pending_group_rows
        )
        uncertain_classification = (
            len(pending_group_rows) - no_content_pending
        )
        cleanup_pending_indexes = db.execute(
            """SELECT count(*) FROM index_cleanup_queue
            WHERE state='pending'"""
        ).fetchone()[0]
        ignored_appledouble = db.execute(
            """SELECT count(*) FROM files
            WHERE state='excluded_completed'
            AND error LIKE 'macOS AppleDouble%'"""
        ).fetchone()[0]
        classification_methods = []
        classification_types = []
        for row in classification_rows:
            try:
                value = json.loads(row["classification_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            classification_methods.append(
                str(value.get("classification_method") or "")
            )
            classification_types.append(
                str(value.get("document_type") or "")
            )
        classification_percent_row = db.execute(
            "SELECT value FROM metadata "
            "WHERE key='classification_implementation_percent'"
        ).fetchone()
        classification_percent = (
            int(classification_percent_row["value"])
            if classification_percent_row
            else 80
        )
        print(f"系统实施进度：{SYSTEM_IMPLEMENTATION_PERCENT}%")
        print("当前阶段：批量整组解析、题答合并、自动分类和三层入库")
        print(
            "输入清点："
            f"压缩包{inventory['archives']}｜视频{inventory['videos']}｜"
            f"下载中{inventory['transient']}｜未知{inventory['unknown']}｜"
            f"文件夹{inventory['directories']}"
        )
        print(f"题库处理进度：{percent}%｜{processed} / {total}")
        print(f"成功完成并按规则清理：{completed}")
        print(f"启用删除审计后已确认永久删除：{audited_deletions}")
        print(f"用户手动永久排除：{user_deleted}")
        print(f"失败并保留：{failed}")
        if ignored_appledouble:
            print(f"已终结macOS伪文件历史记录：{ignored_appledouble}")
        print(
            f"成功进度：{successful_percent}%｜当前处理中：{active}｜"
            f"待提交或重试：{queued}｜待分类并保留：{retained}｜"
            f"历史缺源无法恢复：{source_missing}"
        )
        print(f"当前文件：{current_file}")
        print(f"当前动作：{current_action}")
        if migration_running:
            print(
                "历史三层迁移："
                f"{int(migration_status.get('index') or 0)} / "
                f"{int(migration_status.get('total') or 0)}"
            )
        elif migration_status.get("state") in {"partial", "completed"}:
            summary = str(migration_status.get("action") or "").strip()
            if summary:
                print(
                    "历史三层迁移最近汇总："
                    f"{migration_status.get('state')}｜{summary}"
                )
        print(f"WeKnora独立索引队列：{indexing_groups}组")
        print(f"旧索引持久清理队列：{cleanup_pending_indexes}个待重试")
        print(
            "三层内容结构："
            f"{raw_indexed_groups} / {completed_groups}组已含原文索引"
        )
        print(
            f"最近完成文件有效处理耗时：中位数{median_total}秒｜"
            f"P90 {p90_total}秒"
        )
        print(f"分类实施进度：{classification_percent}%")
        print(f"已分类资料组：{len(classification_types)}")
        print(
            "规则确定："
            f"{sum('mimo' not in item for item in classification_methods)}"
        )
        print(
            "MiMo兜底："
            f"{sum('mimo' in item for item in classification_methods)}"
        )
        print(
            "本地文本0.6B分类备用："
            f"{sum('local' in item for item in classification_methods)}"
        )
        print(
            "待处理分类组："
            f"{len(pending_group_rows)}｜"
            f"无可入库题目正文：{no_content_pending}｜"
            f"分类证据不足：{uncertain_classification}"
        )
        print(
            "文档类型仍为待分类："
            f"{sum(item == '待分类' for item in classification_types)}"
        )
        print(
            "其他资料："
            f"{sum(item == '其他资料' for item in classification_types)}"
        )
        return
    if args.migrate_indexes:
        with single_instance(".mutation.lock"), single_instance():
            migrate_lightweight_indexes(cfg, db)
        return
    if args.classification_dry_run:
        with single_instance(".mutation.lock"):
            migrate_classified_markdown(
                cfg,
                db,
                dry_run=True,
                delete_old_indexes=False,
            )
        return
    if args.migrate_classification:
        with single_instance(".mutation.lock"), single_instance(
            ".classification.lock"
        ):
            migrate_classified_markdown(
                cfg,
                db,
                dry_run=False,
                delete_old_indexes=args.delete_old_indexes,
            )
        return
    lock_name = ".prequeue.lock" if args.prequeue_only else ".ingest.lock"
    with single_instance(".mutation.lock"), single_instance(lock_name):
        prequeue_guard = (
            single_instance(".prequeue.lock")
            if not args.prequeue_only
            else nullcontext()
        )
        classification_guard = (
            single_instance(".classification.lock")
            if not args.prequeue_only
            else nullcontext()
        )
        with prequeue_guard, classification_guard:
            try:
                deletion_sync = auto_sync_manual_deletions(cfg, db)
                if any(deletion_sync.values()):
                    print(
                        "手动删除自动同步："
                        + json.dumps(deletion_sync, ensure_ascii=False)
                    )
            except Exception as exc:
                print(
                    f"手动删除自动同步暂未完成，将在下一轮重试: {exc}"
                )
            reconcile_deleted_sources(db)
            try:
                classified = classify_inbox(cfg)
            except Exception as exc:
                raise SystemExit(
                    f"分类前检查失败，inbox内容未批量处理：{exc}"
                ) from exc
            print(
                "分类完成："
                f"压缩包展开{classified.archives_extracted}｜"
                f"压缩包失败保留{classified.archives_failed}｜"
                f"视频永久删除{classified.videos_deleted}"
                f"（{classified.video_bytes_deleted / 1024**2:.1f}MB）｜"
                f"未知文件保留{classified.unsupported_moved}｜"
                f"下载临时文件等待{classified.transient_skipped}｜"
                f"可入库文件{classified.supported_files}"
            )
            if args.classify_only:
                return
            files = [
                p for p in cfg["folders"]["inbox"].rglob("*")
                if p.is_file() and p.suffix.lower() in SUPPORTED
            ]
            mineru_files = [
                path for path in files
                if path.suffix.casefold() not in DIRECT_TEXT
            ]
            tokens = mineru_tokens()
            if mineru_files and not tokens:
                raise SystemExit(
                    "请在.env或mineru-keys.env中至少填写一个MinerU API Key"
                )
            token_slots = list(tokens) or ["primary"]
            known_by_path = {
                row["source_path"]: row
                for row in db.execute(
                    "SELECT source_path,sha256,batch_id,state FROM files"
                ).fetchall()
            }
            downstream_states = {
                "parsed", "indexing", "verified", "cleanup_pending",
                "completed", "classification_pending",
                "excluded_completed", "excluded_cleanup_pending",
                "user_delete_pending", "user_deleted",
            }
            prequeue_needed = []
            current_digest_owners: dict[str, Path] = {}
            for path in mineru_files:
                known = known_by_path.get(str(path))
                current_digest = stable_sha256(path)
                duplicate_owner = current_digest_owners.get(current_digest)
                if duplicate_owner and duplicate_owner.resolve() != path.resolve():
                    raise SystemExit(
                        "发现两个字节完全相同的待处理源文件；为避免共享批次和状态，"
                        f"请只保留一份后重试: {duplicate_owner} | {path}"
                    )
                current_digest_owners[current_digest] = path
                if known and (
                    decode_batch_ids(known["batch_id"])
                    or known["state"] in downstream_states
                ) and str(known["sha256"] or "").casefold() == current_digest.casefold():
                    continue
                prequeue_needed.append(path)
            print(
                f"MinerU排队清点：总文件{len(mineru_files)}｜"
                f"已有批次或后续结果{len(mineru_files) - len(prequeue_needed)}｜"
                f"本次需提交或重试{len(prequeue_needed)}"
            )
            if files and not args.prequeue_only:
                try:
                    preflight(cfg, db)
                except Exception as exc:
                    raise SystemExit(
                        f"处理前检查失败，尚未提交新的MinerU任务：{exc}"
                    ) from exc
            prequeue_all_mineru(prequeue_needed, cfg, token_slots)
            if args.prequeue_only:
                print(
                    "MinerU只排队模式完成；"
                    "未消费结果、未调用本地模型、未删除源文件"
                )
                return
            print(
                f"MinerU并行通道：{len(token_slots)}｜"
                f"{' + '.join(token_slots)}"
            )
            resources = cfg.get("resource_control") or {}
            recovery_executor = None
            recovery_future = None
            recovery_indexed = False
            if bool(resources.get("source_recovery_background", True)):
                recovery_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="source-recovery",
                )
                recovery_future = recovery_executor.submit(
                    recover_missing_sources_in_background,
                    cfg,
                    max(
                        0,
                        int(resources.get("source_recovery_limit_per_run", 0)),
                    ),
                )
                print(
                    "历史缺源恢复：已启动1路低优先级后台通道；"
                    "仅下载原MinerU批次，不重新提交"
                )
            round_number = 0
            try:
                while True:
                    try:
                        deletion_sync = auto_sync_manual_deletions(cfg, db)
                        if any(deletion_sync.values()):
                            print(
                                "手动删除自动同步："
                                + json.dumps(
                                    deletion_sync, ensure_ascii=False
                                )
                            )
                    except Exception as exc:
                        print(
                            "手动删除自动同步暂未完成，"
                            f"将在下一轮重试: {exc}"
                        )
                    candidates = processing_candidates(cfg, db)
                    if not candidates:
                        if recovery_future is not None and not recovery_future.done():
                            print(
                                "正常资料已暂时排空；等待历史缺源恢复通道，"
                                "不会重新提交MinerU"
                            )
                            time.sleep(30)
                            continue
                        if recovery_future is not None:
                            try:
                                recovery_stats = recovery_future.result()
                            except Exception as exc:
                                recovery_stats = {
                                    "restored": 0,
                                    "recovered": 0,
                                    "waiting": 0,
                                    "unrecoverable": 0,
                                    "error": str(exc),
                                }
                            recovery_future = None
                            print(
                                "历史缺源恢复完成："
                                + json.dumps(recovery_stats, ensure_ascii=False)
                            )
                        if not recovery_indexed:
                            try:
                                recovered_stats = process_recovered_groups(cfg, 0)
                            except Exception as exc:
                                recovered_stats = {
                                    "completed": 0,
                                    "resumed_indexing": 0,
                                    "skipped": 0,
                                    "error": str(exc),
                                }
                            recovery_indexed = True
                            print(
                                "历史恢复资料入库："
                                + json.dumps(recovered_stats, ensure_ascii=False)
                            )
                            candidates = processing_candidates(cfg, db)
                            if candidates:
                                continue
                        print("自动消费完成：没有仍可自动推进的资料")
                        break
                    round_number += 1
                    before_terminal = db.execute(
                        """SELECT count(*) FROM files
                        WHERE state IN (
                            'completed','excluded_completed','failed',
                            'classification_pending','user_deleted'
                        )"""
                    ).fetchone()[0]
                    consume_processing_round(
                        candidates, cfg, db, token_slots, round_number
                    )
                    remaining = processing_candidates(cfg, db)
                    after_terminal = db.execute(
                        """SELECT count(*) FROM files
                        WHERE state IN (
                            'completed','excluded_completed','failed',
                            'classification_pending','user_deleted'
                        )"""
                    ).fetchone()[0]
                    wait_seconds = int(
                        cfg["mineru"].get("consume_round_seconds", 60)
                    )
                    print(
                        f"本轮新增终态{after_terminal - before_terminal}｜"
                        f"仍待自动处理{len(remaining)}｜"
                        f"{wait_seconds}秒后继续轮询，不占用单个工作线程"
                    )
                    time.sleep(max(5, wait_seconds))
            finally:
                if recovery_executor is not None:
                    recovery_executor.shutdown(wait=False, cancel_futures=False)
            if bool(
                resources.get(
                    "historical_content_migration_after_batch", True
                )
            ):
                print(
                    "正常资料和缺源恢复已排空；"
                    "开始低优先级历史三层迁移"
                )
                with single_instance(".content-migration.lock"):
                    migrate_content_structure(
                        cfg,
                        db,
                        max(
                            0,
                            int(
                                resources.get(
                                    "historical_content_migration_limit_per_run",
                                    0,
                                )
                            ),
                        ),
                    )
        remove_empty_inbox_directories(cfg["folders"]["inbox"])


if __name__ == "__main__":
    main()
