"""Trust-boundary regressions for the on-disk FFmpeg capability cache."""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from Final import capability as cap


def _safe_cache_payload(ffmpeg_path: Path) -> dict[str, object]:
    return {
        "caps_schema": cap._CAPS_SCHEMA,
        "working_encoders": [],
        "available_encoders": ["libx264", "libx265"],
        "nvidia": False,
        "amd": False,
        "intel": False,
        "apple": False,
        "gpu_name": "Unknown",
        "cuda_available": False,
        "ffmpeg_version": "ffmpeg version 8.1.2",
        "timestamp": time.time(),
        "ffmpeg_path": str(ffmpeg_path),
        "ffmpeg_mtime": 123.25,
    }


def _load_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> dict[str, object] | None:
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    ffmpeg_path.write_bytes(b"approved binary placeholder")
    cache_file = tmp_path / "caps_cache.json"
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(cap, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cap, "_CACHE_FILE", cache_file)
    monkeypatch.delenv("YT_FINAL_NO_CAPS_CACHE", raising=False)

    detector = cap.CapabilityDetector.__new__(cap.CapabilityDetector)
    detector.ffmpeg_path = str(ffmpeg_path)
    monkeypatch.setattr(detector, "_resolve_ffmpeg_mtime", lambda: 123.25)
    return detector._load_cache()


def test_exact_safe_negative_cache_shape_can_be_reused(tmp_path, monkeypatch):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    payload = _safe_cache_payload(ffmpeg_path)

    loaded = _load_payload(tmp_path, monkeypatch, payload)

    assert loaded == payload


@pytest.mark.parametrize(
    ("field", "poisoned"),
    [
        ("caps_schema", True),
        ("working_encoders", "h264_nvenc"),
        ("available_encoders", {"libx264": True}),
        ("nvidia", 1),
        ("amd", "false"),
        ("intel", None),
        ("apple", []),
        ("cuda_available", "0"),
        ("gpu_name", 7),
        ("gpu_name", "GPU\nforged"),
        ("gpu_name", "G" * 257),
        ("ffmpeg_version", 8),
        ("ffmpeg_version", "ffmpeg version forged\nsecond line"),
        ("ffmpeg_version", "v" * 513),
        ("timestamp", "123.25"),
        ("timestamp", math.nan),
        ("ffmpeg_mtime", "123.25"),
        ("ffmpeg_mtime", math.inf),
    ],
)
def test_cache_rejects_wrong_primitive_shapes_and_bounds(
    tmp_path,
    monkeypatch,
    field,
    poisoned,
):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    payload = _safe_cache_payload(ffmpeg_path)
    payload[field] = poisoned

    assert _load_payload(tmp_path, monkeypatch, payload) is None


@pytest.mark.parametrize("missing", list(_safe_cache_payload(Path("ffmpeg.exe"))))
def test_cache_rejects_every_missing_required_field(
    tmp_path,
    monkeypatch,
    missing,
):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    payload = _safe_cache_payload(ffmpeg_path)
    del payload[missing]

    assert _load_payload(tmp_path, monkeypatch, payload) is None


def test_cache_rejects_unknown_fields(tmp_path, monkeypatch):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    payload = _safe_cache_payload(ffmpeg_path)
    payload["future_claim"] = True

    assert _load_payload(tmp_path, monkeypatch, payload) is None


def test_cache_rejects_duplicate_json_keys_instead_of_using_last_value(
    tmp_path,
    monkeypatch,
):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    ffmpeg_path.write_bytes(b"approved binary placeholder")
    payload = _safe_cache_payload(ffmpeg_path)
    raw = json.dumps(payload).replace(
        '"nvidia": false',
        '"nvidia": true, "nvidia": false',
    )
    cache_file = tmp_path / "caps_cache.json"
    cache_file.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(cap, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cap, "_CACHE_FILE", cache_file)
    monkeypatch.delenv("YT_FINAL_NO_CAPS_CACHE", raising=False)
    detector = cap.CapabilityDetector.__new__(cap.CapabilityDetector)
    detector.ffmpeg_path = str(ffmpeg_path)
    monkeypatch.setattr(detector, "_resolve_ffmpeg_mtime", lambda: 123.25)

    assert detector._load_cache() is None


@pytest.mark.parametrize(
    ("field", "encoders"),
    [
        ("working_encoders", ["-f", "lavfi"]),
        ("available_encoders", ["libx264", "evil_encoder"]),
        ("available_encoders", ["libx264", "libx264"]),
    ],
)
def test_cache_rejects_encoder_poison_outside_exact_unique_allowlists(
    tmp_path,
    monkeypatch,
    field,
    encoders,
):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    payload = _safe_cache_payload(ffmpeg_path)
    payload[field] = encoders

    assert _load_payload(tmp_path, monkeypatch, payload) is None


def test_cache_rejects_working_encoder_not_listed_as_available(
    tmp_path,
    monkeypatch,
):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    payload = _safe_cache_payload(ffmpeg_path)
    payload["working_encoders"] = ["h264_nvenc"]

    assert _load_payload(tmp_path, monkeypatch, payload) is None


def test_allowlisted_positive_hardware_cache_is_only_a_validated_hint(
    tmp_path,
    monkeypatch,
):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    payload = _safe_cache_payload(ffmpeg_path)
    payload.update(
        {
            "working_encoders": ["h264_nvenc"],
            "available_encoders": ["h264_nvenc", "libx264"],
            "nvidia": True,
            "cuda_available": True,
            "gpu_name": "Forged but bounded",
        }
    )

    assert _load_payload(tmp_path, monkeypatch, payload) == payload


def _detector_for_revalidation(ffmpeg_path: Path):
    detector = cap.CapabilityDetector.__new__(cap.CapabilityDetector)
    detector.ffmpeg_path = str(ffmpeg_path)
    detector._encoders_text = ""
    detector.gpu_info = {
        "nvidia": False,
        "amd": False,
        "intel": False,
        "apple": False,
        "working_encoders": [],
        "available_encoders": set(),
        "gpu_name": "Unknown",
        "cuda_available": False,
        "ffmpeg_version": "ffmpeg version live",
    }
    return detector


def test_positive_cache_revalidates_only_hinted_encoders_and_derives_claims(
    tmp_path,
    monkeypatch,
):
    """A writable positive cache narrows probes but never supplies truth."""
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    ffmpeg_path.write_bytes(b"approved binary placeholder")
    payload = _safe_cache_payload(ffmpeg_path)
    payload.update(
        {
            "working_encoders": ["h264_nvenc"],
            "available_encoders": [
                "h264_nvenc",
                "h264_qsv",
                "libx264",
            ],
            "nvidia": True,
            "cuda_available": True,
            "gpu_name": "Untrusted cached name",
        }
    )
    detector = _detector_for_revalidation(ffmpeg_path)
    monkeypatch.setattr(detector, "_load_cache", lambda: payload)
    monkeypatch.setattr(detector, "_test_basic_ffmpeg", lambda: None)

    def refresh_encoders():
        detector.gpu_info["available_encoders"] = {
            "h264_nvenc",
            "h264_qsv",
            "libx264",
        }

    monkeypatch.setattr(detector, "_cache_encoders_list", refresh_encoders)
    cuda_calls = []
    monkeypatch.setattr(
        detector,
        "_probe_cuda",
        lambda **kwargs: cuda_calls.append(kwargs) or True,
        raising=False,
    )
    encoder_calls = []
    monkeypatch.setattr(
        detector,
        "_test_encoder_comprehensive",
        lambda name, **kwargs: encoder_calls.append((name, kwargs)) or True,
    )
    monkeypatch.setattr(
        detector,
        "_get_nvidia_details",
        lambda: detector.gpu_info.update(gpu_name="Live GPU"),
    )
    monkeypatch.setattr(detector, "_get_apple_gpu_details", lambda: None)
    saved = []
    monkeypatch.setattr(detector, "_save_cache", lambda: saved.append(True))

    detector._detect_all()

    assert [name for name, _kwargs in encoder_calls] == ["h264_nvenc"]
    assert encoder_calls[0][1].get("deadline") is not None
    assert len(cuda_calls) == 1
    assert detector.gpu_info["working_encoders"] == ["h264_nvenc"]
    assert detector.gpu_info["nvidia"] is True
    assert detector.gpu_info["amd"] is False
    assert detector.gpu_info["intel"] is False
    assert detector.gpu_info["gpu_name"] == "Live GPU"
    assert saved == [True]


def test_failed_positive_hint_falls_back_to_full_detection(
    tmp_path,
    monkeypatch,
):
    """A stale or forged hint cannot suppress the authoritative full probe."""
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    ffmpeg_path.write_bytes(b"approved binary placeholder")
    payload = _safe_cache_payload(ffmpeg_path)
    payload.update(
        {
            "working_encoders": ["h264_nvenc"],
            "available_encoders": ["h264_nvenc"],
            "nvidia": True,
        }
    )
    detector = _detector_for_revalidation(ffmpeg_path)
    monkeypatch.setattr(detector, "_load_cache", lambda: payload)
    monkeypatch.setattr(detector, "_test_basic_ffmpeg", lambda: None)
    monkeypatch.setattr(
        detector,
        "_cache_encoders_list",
        lambda: detector.gpu_info.update(available_encoders={"libx264"}),
    )
    full_probes = []

    def full_detection():
        full_probes.append(True)
        detector.gpu_info["working_encoders"] = ["h264_qsv"]
        detector.gpu_info["intel"] = True

    monkeypatch.setattr(detector, "_detect_cuda_and_encoders", full_detection)
    monkeypatch.setattr(detector, "_get_nvidia_details", lambda: None)
    monkeypatch.setattr(detector, "_get_apple_gpu_details", lambda: None)
    monkeypatch.setattr(detector, "_save_cache", lambda: None)

    detector._detect_all()

    assert full_probes == [True]
    assert detector.gpu_info["working_encoders"] == ["h264_qsv"]
    assert detector.gpu_info["nvidia"] is False
    assert detector.gpu_info["intel"] is True


def test_positive_hint_count_is_bounded_before_any_live_probe(
    tmp_path,
    monkeypatch,
):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    payload = _safe_cache_payload(ffmpeg_path)
    payload["working_encoders"] = [
        name
        for name, _vendor in cap.HW_ENCODERS_TO_TEST[
            : cap._CACHE_MAX_WORKING_HINTS + 1
        ]
    ]
    payload["available_encoders"] = list(payload["working_encoders"])
    for name, vendor in cap.HW_ENCODERS_TO_TEST:
        if name in payload["working_encoders"]:
            payload[vendor] = True

    assert _load_payload(tmp_path, monkeypatch, payload) is None


def test_future_dated_cache_cannot_extend_its_own_lifetime(
    tmp_path,
    monkeypatch,
):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    payload = _safe_cache_payload(ffmpeg_path)
    payload["timestamp"] = time.time() + 301

    assert _load_payload(tmp_path, monkeypatch, payload) is None


def test_oversized_cache_is_rejected_before_json_loading(tmp_path, monkeypatch):
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    ffmpeg_path.write_bytes(b"approved binary placeholder")
    cache_file = tmp_path / "caps_cache.json"
    cache_file.write_text(" " * (64 * 1024 + 1), encoding="utf-8")
    monkeypatch.setattr(cap, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cap, "_CACHE_FILE", cache_file)
    monkeypatch.delenv("YT_FINAL_NO_CAPS_CACHE", raising=False)
    detector = cap.CapabilityDetector.__new__(cap.CapabilityDetector)
    detector.ffmpeg_path = str(ffmpeg_path)
    monkeypatch.setattr(detector, "_resolve_ffmpeg_mtime", lambda: 123.25)

    assert detector._load_cache() is None


def _cache_writer(monkeypatch, cache_dir: Path):
    cache_file = cache_dir / "caps_cache.json"
    monkeypatch.setattr(cap, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(cap, "_CACHE_FILE", cache_file)
    monkeypatch.delenv("YT_FINAL_NO_CAPS_CACHE", raising=False)
    detector = cap.CapabilityDetector.__new__(cap.CapabilityDetector)
    detector.ffmpeg_path = str(cache_dir.parent / "ffmpeg.exe")
    detector.gpu_info = {
        "working_encoders": [],
        "available_encoders": {"libx264"},
        "nvidia": False,
        "amd": False,
        "intel": False,
        "apple": False,
        "gpu_name": "Unknown",
        "cuda_available": False,
        "ffmpeg_version": "ffmpeg version 8.1.2",
    }
    monkeypatch.setattr(detector, "_resolve_ffmpeg_mtime", lambda: 123.25)
    return detector, cache_file


def _mark_path_link_like(monkeypatch, poisoned: Path, kind: str) -> None:
    if kind == "symlink":
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda candidate: (
                candidate == poisoned or real_is_symlink(candidate)
            ),
        )
        return
    if kind == "junction":
        real_is_junction = getattr(Path, "is_junction", lambda _path: False)
        monkeypatch.setattr(
            Path,
            "is_junction",
            lambda candidate: (
                candidate == poisoned or real_is_junction(candidate)
            ),
            raising=False,
        )
        return

    real_lstat = Path.lstat

    class ReparseMetadata:
        def __init__(self, metadata):
            self._metadata = metadata
            self.st_mode = metadata.st_mode
            self.st_file_attributes = (
                getattr(metadata, "st_file_attributes", 0) | 0x400
            )

        def __getattr__(self, name):
            return getattr(self._metadata, name)

    def reparse_lstat(candidate):
        metadata = real_lstat(candidate)
        if candidate == poisoned:
            return ReparseMetadata(metadata)
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse_lstat)


@pytest.mark.parametrize("poison_kind", ["symlink", "junction", "reparse"])
def test_cache_write_rejects_poisoned_existing_parent_before_mkdir(
    tmp_path,
    monkeypatch,
    poison_kind,
):
    poisoned = tmp_path / "linked-app-data"
    poisoned.mkdir()
    cache_dir = poisoned / "yt_final"
    detector, cache_file = _cache_writer(monkeypatch, cache_dir)
    _mark_path_link_like(monkeypatch, poisoned, poison_kind)

    detector._save_cache()

    assert not cache_dir.exists()
    assert not cache_file.exists()


def test_cache_write_revalidates_directory_after_mkdir(tmp_path, monkeypatch):
    cache_dir = tmp_path / "yt_final"
    detector, cache_file = _cache_writer(monkeypatch, cache_dir)
    real_mkdir = Path.mkdir
    poisoned_after_mkdir = False

    def poison_created_directory(path, *args, **kwargs):
        nonlocal poisoned_after_mkdir
        result = real_mkdir(path, *args, **kwargs)
        if path == cache_dir:
            poisoned_after_mkdir = True
        return result

    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "mkdir", poison_created_directory)
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: (
            poisoned_after_mkdir
            and candidate == cache_dir
        ) or real_is_symlink(candidate),
    )

    detector._save_cache()

    assert poisoned_after_mkdir is True
    assert cache_dir.is_dir()
    assert not cache_file.exists()


@pytest.mark.parametrize("poison_kind", ["symlink", "junction", "reparse"])
def test_cache_invalidation_refuses_unlink_through_poisoned_parent(
    tmp_path,
    monkeypatch,
    poison_kind,
):
    poisoned = tmp_path / "linked-app-data"
    cache_dir = poisoned / "yt_final"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "caps_cache.json"
    cache_file.write_bytes(b"external-cache-sentinel")
    monkeypatch.setattr(cap, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(cap, "_CACHE_FILE", cache_file)
    monkeypatch.setattr(cap, "_detector_singleton", object())
    _mark_path_link_like(monkeypatch, poisoned, poison_kind)

    cap.invalidate_capability_cache()

    assert cap._detector_singleton is None
    assert cache_file.read_bytes() == b"external-cache-sentinel"
