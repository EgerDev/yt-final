"""Strict, non-destructive validation for the durable queue and watch stores.

The stores deliberately preserve unknown fields for forward/backward compatibility.
This module validates only the versioned schema that existing readers actually
consume; it never normalizes or rewrites a valid document.
"""

from __future__ import annotations

import copy
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NoReturn, TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from Final.jobspec import JobSpec
else:
    try:
        from .jobspec import JobSpec
    except ImportError:
        from jobspec import JobSpec


_QUEUE_STATUSES = frozenset({"queued", "running", "done", "failed", "cancelled"})

# JobSpec.to_dict() types, split by the actual dataclass serialization contract.
# Missing fields remain compatible with older JobSpec versions. Unknown fields are
# preserved because JobSpec.from_dict() intentionally ignores options it does not know.
_SPEC_REQUIRED_STRINGS = frozenset({
    "quality",
    "encode_mode",
    "profile",
    "download_engine",
    "sub_format",
    "nfo_kind",
    "notify_template",
    "color_tag",
})
_SPEC_OPTIONAL_STRINGS = frozenset({
    "output_dir",
    "codec",
    "section",
    "format_id",
    "cookies_from_browser",
    "cookiefile",
    "source_address",
    "proxy",
    "download_archive",
    "audio_format",
    "audio_quality",
    "audio_codec",
    "audio_bitrate",
    "audio_map",
    "sub_mode",
    "export_markers",
    "sponsorblock_markers",
    "notify_url",
    "sponsorblock",
    "bitrate",
    "output_template",
    "organize_by",
    "on_complete",
})
_SPEC_BOOLEANS = frozenset({
    "section_precise",
    "parallel",
    "speed_priority",
    "use_auth",
    "slow_requests",
    "chapters",
    "embed_metadata",
    "embed_thumbnail",
    "sub_auto",
    "write_sidecar",
    "write_sidecar_text",
    "no_shorts",
    "write_nfo",
    "burn_in",
    "write_artwork",
    "proxy_pair",
    "preserve_hdr",
    "verify_strict",
    "two_pass",
})
_SPEC_OPTIONAL_INTEGERS = frozenset({"workers", "concurrent_fragments"})


class PersistentStateError(RuntimeError):
    """A durable-state document was quarantined instead of silently replaced."""

    def __init__(self, path: Path, backup_path: Path, reason: str):
        self.path = Path(path)
        self.backup_path = Path(backup_path)
        self.reason = str(reason)
        super().__init__(
            f"Invalid persistent state at {self.path}; preserved at "
            f"{self.backup_path}: {self.reason}"
        )


def _fail(location: str, message: str) -> NoReturn:
    raise ValueError(f"{location}: {message}")


def _require_keys(value: dict, keys: tuple[str, ...], location: str) -> None:
    missing = [key for key in keys if key not in value]
    if missing:
        _fail(location, "missing required key(s): " + ", ".join(missing))


def _is_integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        # Downstream timestamp/loudnorm consumers convert to float. Reject integers
        # outside that finite domain here with a schema error, before they escape as
        # OverflowError from claim selection or media processing.
        try:
            value = float(value)
        except OverflowError:
            return False
    return isinstance(value, float) and math.isfinite(value)


def _validate_nonempty_string(value: object, location: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(location, "must be a non-empty string")


def _validate_optional_string(value: object, location: str) -> None:
    if value is not None and not isinstance(value, str):
        _fail(location, "must be a string or null")


def validate_request_id(value: object, location: str = "request_id") -> str:
    """Validate one exact rerun idempotency key without normalizing it."""
    if not isinstance(value, str):
        _fail(location, "must be a string")
    if not 8 <= len(value) <= 128:
        _fail(location, "must be 8 to 128 characters")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        _fail(location, "must contain only printable ASCII characters")
    return value


def _validate_string_list(value: object, location: str) -> None:
    if not isinstance(value, list):
        _fail(location, "must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            _fail(f"{location}[{index}]", "must be a string")


def _validate_timestamp(value: object, location: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not _is_number(value) or value < 0:
        _fail(location, "must be a finite non-negative timestamp")


def _validate_jobspec(value: object, location: str, *, require_urls: bool) -> None:
    if not isinstance(value, dict):
        _fail(location, "must be an object")

    # Current JobSpec serialization always contains `urls`; the singular `url`
    # alias remains accepted for legacy queue rows because JobSpec.from_dict()
    # explicitly supports it.
    if require_urls and "urls" not in value and "url" not in value:
        _fail(location, "missing required key: urls")
    if "urls" in value:
        _validate_string_list(value["urls"], f"{location}.urls")
    if "url" in value:
        url = value["url"]
        if isinstance(url, list):
            _validate_string_list(url, f"{location}.url")
        elif not isinstance(url, str):
            _fail(f"{location}.url", "must be a string or list of strings")

    for field in _SPEC_REQUIRED_STRINGS:
        if field in value and not isinstance(value[field], str):
            _fail(f"{location}.{field}", "must be a string")
    for alias in ("mode", "quality_level"):
        if alias in value and not isinstance(value[alias], str):
            _fail(f"{location}.{alias}", "must be a string")
    for field in _SPEC_OPTIONAL_STRINGS:
        if field in value:
            _validate_optional_string(value[field], f"{location}.{field}")
    for field in _SPEC_BOOLEANS:
        if field in value and not isinstance(value[field], bool):
            _fail(f"{location}.{field}", "must be a boolean")
    for field in _SPEC_OPTIONAL_INTEGERS:
        if field in value:
            field_value = value[field]
            if field_value is not None and not _is_integer(field_value):
                _fail(f"{location}.{field}", "must be an integer or null")
    if "loudnorm" in value:
        loudnorm = value["loudnorm"]
        if loudnorm is not None and not _is_number(loudnorm):
            _fail(f"{location}.loudnorm", "must be a finite number or null")
    if "sub_langs" in value:
        sub_langs = value["sub_langs"]
        if sub_langs is not None:
            _validate_string_list(sub_langs, f"{location}.sub_langs")


def _validate_queue_record(record: object, index: int) -> str:
    location = f"jobs[{index}]"
    if not isinstance(record, dict):
        _fail(location, "must be an object")
    _require_keys(
        record,
        (
            "id",
            "spec",
            "status",
            "attempt_count",
            "last_error",
            "output_paths",
            "created_at",
            "updated_at",
            "label",
        ),
        location,
    )

    _validate_nonempty_string(record["id"], f"{location}.id")
    _validate_jobspec(record["spec"], f"{location}.spec", require_urls=True)
    if (
        not isinstance(record["status"], str)
        or record["status"] not in _QUEUE_STATUSES
    ):
        _fail(f"{location}.status", "is not a supported queue status")
    if not _is_integer(record["attempt_count"]) or record["attempt_count"] < 0:
        _fail(f"{location}.attempt_count", "must be a non-negative integer")
    _validate_optional_string(record["last_error"], f"{location}.last_error")
    _validate_string_list(record["output_paths"], f"{location}.output_paths")
    _validate_timestamp(record["created_at"], f"{location}.created_at")
    _validate_timestamp(record["updated_at"], f"{location}.updated_at")
    if not isinstance(record["label"], str):
        _fail(f"{location}.label", "must be a string")

    if "priority" in record and not _is_integer(record["priority"]):
        _fail(f"{location}.priority", "must be an integer")
    if "last_error_kind" in record:
        _validate_optional_string(record["last_error_kind"], f"{location}.last_error_kind")
    if "cancel_requested" in record and not isinstance(record["cancel_requested"], bool):
        _fail(f"{location}.cancel_requested", "must be a boolean")
    if "result" in record and not isinstance(record["result"], dict):
        _fail(f"{location}.result", "must be an object")
    if "request_id" in record:
        validate_request_id(record["request_id"], f"{location}.request_id")
    return record["id"]


def validate_queue_document(value: object) -> dict:
    """Return *value* unchanged when it is a compatible queue v1 document."""
    if not isinstance(value, dict):
        _fail("queue", "document must be an object")
    _require_keys(value, ("version", "jobs"), "queue")
    if not _is_integer(value["version"]) or value["version"] != 1:
        _fail("queue.version", "must be the supported integer version 1")
    jobs = value["jobs"]
    if not isinstance(jobs, list):
        _fail("queue.jobs", "must be a list")
    seen_ids: set[str] = set()
    for index, record in enumerate(jobs):
        job_id = _validate_queue_record(record, index)
        if job_id in seen_ids:
            _fail(f"jobs[{index}].id", f"duplicate identifier {job_id!r}")
        seen_ids.add(job_id)
    if "paused" in value and not isinstance(value["paused"], bool):
        _fail("queue.paused", "must be a boolean")
    return value


def _validate_watch_filters(value: object, location: str) -> None:
    if not isinstance(value, dict):
        _fail(location, "must be an object")
    for field in ("filter_title", "after"):
        if field in value:
            _validate_optional_string(value[field], f"{location}.{field}")
    for field in ("min_duration", "max_duration"):
        if field in value:
            duration = value[field]
            if duration is not None and not _is_integer(duration):
                _fail(f"{location}.{field}", "must be an integer or null")


def _validate_pending(value: object, location: str) -> None:
    if not isinstance(value, list):
        _fail(location, "must be a list")
    for index, pending in enumerate(value):
        item_location = f"{location}[{index}]"
        if not isinstance(pending, dict):
            _fail(item_location, "must be an object")
        _require_keys(pending, ("id",), item_location)
        _validate_nonempty_string(pending["id"], f"{item_location}.id")
        for field in ("title", "url", "published"):
            if field in pending:
                _validate_optional_string(pending[field], f"{item_location}.{field}")


def _validate_string_keyed_map(
        value: object,
        location: str,
        value_validator: Callable[[object, str], None],
) -> None:
    if not isinstance(value, dict):
        _fail(location, "must be an object")
    for key, item in value.items():
        _validate_nonempty_string(key, f"{location} key")
        value_validator(item, f"{location}[{key!r}]")


def _validate_retry_count(value: object, location: str) -> None:
    if not _is_integer(value) or value < 0:
        _fail(location, "must be a non-negative integer")


def _validate_error_text(value: object, location: str) -> None:
    if not isinstance(value, str):
        _fail(location, "must be a string")


def _validate_watch_record(record: object, index: int) -> str:
    location = f"watches[{index}]"
    if not isinstance(record, dict):
        _fail(location, "must be an object")
    _require_keys(
        record,
        (
            "id",
            "channel_id",
            "label",
            "url",
            "auto_queue",
            "defaults",
            "filters",
            "seen",
            "pending",
            "created_at",
            "last_checked",
            "last_error",
        ),
        location,
    )

    _validate_nonempty_string(record["id"], f"{location}.id")
    _validate_nonempty_string(record["channel_id"], f"{location}.channel_id")
    if not isinstance(record["label"], str):
        _fail(f"{location}.label", "must be a string")
    _validate_optional_string(record["url"], f"{location}.url")
    if not isinstance(record["auto_queue"], bool):
        _fail(f"{location}.auto_queue", "must be a boolean")
    _validate_jobspec(record["defaults"], f"{location}.defaults", require_urls=False)
    _validate_watch_filters(record["filters"], f"{location}.filters")
    _validate_string_list(record["seen"], f"{location}.seen")
    _validate_pending(record["pending"], f"{location}.pending")
    _validate_timestamp(record["created_at"], f"{location}.created_at")
    _validate_timestamp(
        record["last_checked"], f"{location}.last_checked", optional=True)
    _validate_optional_string(record["last_error"], f"{location}.last_error")

    if "auto_queue_fails" in record:
        _validate_string_keyed_map(
            record["auto_queue_fails"],
            f"{location}.auto_queue_fails",
            _validate_retry_count,
        )
    if "auto_queue_errors" in record:
        _validate_string_keyed_map(
            record["auto_queue_errors"],
            f"{location}.auto_queue_errors",
            _validate_error_text,
        )
    return record["id"]


def validate_watch_document(value: object) -> dict:
    """Return *value* unchanged when it is a compatible watch v1 document."""
    if not isinstance(value, dict):
        _fail("watch", "document must be an object")
    _require_keys(value, ("version", "watches"), "watch")
    if not _is_integer(value["version"]) or value["version"] != 1:
        _fail("watch.version", "must be the supported integer version 1")
    watches = value["watches"]
    if not isinstance(watches, list):
        _fail("watch.watches", "must be a list")
    seen_ids: set[str] = set()
    for index, record in enumerate(watches):
        watch_id = _validate_watch_record(record, index)
        if watch_id in seen_ids:
            _fail(f"watches[{index}].id", f"duplicate identifier {watch_id!r}")
        seen_ids.add(watch_id)
    return value


_JOBSPEC_ALIASES = {
    "url": "urls",
    "mode": "encode_mode",
    "quality_level": "quality",
}


def canonicalize_jobspec_fields(value: object) -> dict:
    """Canonicalize documented JobSpec inputs while preserving unknown fields.

    Queue/watch documents deliberately retain fields from future versions. Only
    keys understood by JobSpec are normalized; everything else is copied exactly.
    """
    if isinstance(value, JobSpec):
        return value.to_dict()
    if not isinstance(value, dict):
        _fail("JobSpec", "must be an object")
    original = dict(value)
    converted = JobSpec.from_dict(original).to_dict()
    canonical_fields = {
        _JOBSPEC_ALIASES.get(key, key)
        for key in original
        if _JOBSPEC_ALIASES.get(key, key) in JobSpec._field_names()
    }
    result = copy.deepcopy(original)
    for key in original:
        if _JOBSPEC_ALIASES.get(key, key) in canonical_fields:
            result.pop(key, None)
    for field in canonical_fields:
        result[field] = converted[field]
    return result


def canonicalize_watch_filters(value: object) -> dict:
    """Canonicalize documented duration strings without rewriting unknowns."""
    if not isinstance(value, dict):
        _fail("watch filters", "must be an object")
    result = copy.deepcopy(dict(value))
    for field in ("min_duration", "max_duration"):
        duration = result.get(field)
        if isinstance(duration, str):
            try:
                result[field] = int(duration)
            except ValueError:
                # Leave incompatible input intact so the document validator
                # reports a consistent schema error before persistence.
                pass
    return result


def quarantine_corrupt_state(
    path: Path,
    clock: Callable[[], datetime] | None = None,
) -> Path:
    """Move *path* atomically to a kernel-reserved, no-clobber backup name.

    Store callers hold their sidecar lock across validation and this rename, so
    cooperating actors cannot replace the source pathname between those steps.
    The exclusive reservation also prevents cooperating or racing creators from
    claiming the same backup name. A non-cooperating actor can still replace a
    pathname between any portable re-stat and rename; callers treat identity
    checks against such actors as best-effort rather than an atomic guarantee.
    """
    path = Path(path)
    moment = clock() if clock is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%S%fZ")
    suffix = path.suffix or ".json"
    base_name = path.name[:-len(path.suffix)] if path.suffix else path.name
    candidate = path.with_name(f"{base_name}.corrupt-{stamp}{suffix}")
    collision = 0
    while True:
        try:
            reservation = os.open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            collision += 1
            candidate = path.with_name(
                f"{base_name}.corrupt-{stamp}-{collision}{suffix}")
            continue
        else:
            os.close(reservation)
            break
    try:
        # Replacing our own exclusive reservation is one atomic rename. Exact
        # source identity is guaranteed by the store sidecar protocol; see the
        # docstring for the explicit non-cooperating-actor boundary.
        os.replace(path, candidate)
    except BaseException:
        try:
            os.unlink(candidate)
        except OSError:
            pass
        raise
    return candidate
