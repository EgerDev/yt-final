#!/usr/bin/env python3
"""
CapabilityDetector - Merged GPU/FFmpeg detection from sources.

Detects FFmpeg, the set of available encoders, and which hardware encoders
(NVENC / AMF / QSV, incl. AV1) actually work via a tiny real null-encode probe.
"""

import os
import json
import math
import stat
import threading
import subprocess
import time
import logging
import tempfile
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from .executable_trust import explicit_path_executable
except ImportError:
    from executable_trust import explicit_path_executable

try:
    from .installer_lock import (
        UnsafeInstallerRootError,
        validated_private_root,
    )
except ImportError:
    from installer_lock import (
        UnsafeInstallerRootError,
        validated_private_root,
    )

try:
    from .ffmpeg_updater import (
        cache_dir as _app_cache_dir,
        certified_managed_pair,
        managed_ffmpeg_path,
        probe_pair,
        recover_interrupted_activation,
    )
except ImportError:
    from ffmpeg_updater import (
        cache_dir as _app_cache_dir,
        certified_managed_pair,
        managed_ffmpeg_path,
        probe_pair,
        recover_interrupted_activation,
    )

logger = logging.getLogger(__name__)

# On-disk capability cache: safe-negative entries can skip encoder probes.
# Positive driver/hardware claims are always reproduced by live probes.
def _caps_cache_dir() -> Path:
    try:
        return _app_cache_dir()
    except Exception:
        return Path(os.getenv('LOCALAPPDATA') or (Path.home() / '.cache')) / 'yt_final'


_CACHE_DIR = _caps_cache_dir()
_CACHE_FILE = _CACHE_DIR / 'caps_cache.json'
_CACHE_MAX_AGE_S = 24 * 60 * 60  # 24h
_CACHE_MAX_BYTES = 64 * 1024
_CACHE_MAX_FUTURE_SKEW_S = 5 * 60
_CACHE_MAX_WORKING_HINTS = 6
_CACHE_REVALIDATION_BUDGET_S = 12.0

# Process-wide singleton plumbing (double-checked locking).
_detector_singleton = None
_detector_lock = threading.Lock()

# Serializes concurrent cache writes. Must be distinct from _detector_lock: _save_cache runs inside
# __init__, which get_detector() already calls while holding _detector_lock, so reusing it would
# deadlock (threading.Lock is non-reentrant).
_cache_write_lock = threading.Lock()


def _validated_cache_paths() -> tuple[Path, Path]:
    """Return lexical cache paths only beneath a real private directory chain."""
    cache_dir = validated_private_root(_CACHE_DIR)
    cache_file = Path(
        os.path.abspath(os.fspath(Path(_CACHE_FILE).expanduser()))
    )
    if cache_file.parent != cache_dir:
        raise UnsafeInstallerRootError(
            "Capability cache file must remain beneath its private directory"
        )
    return cache_dir, cache_file


def _unlink_validated_cache_leaf(path: Path) -> None:
    """Unlink a cache leaf only after revalidating its complete parent chain."""
    cache_dir, _cache_file = _validated_cache_paths()
    target = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    if target.parent != cache_dir:
        raise UnsafeInstallerRootError(
            "Capability cache leaf must remain beneath its private directory"
        )
    validated_private_root(cache_dir)
    target.unlink()

# Bump when cache shape / probe set changes so stale caps_cache.json is ignored.
_CAPS_SCHEMA = 3

# Hardware encoders we probe (encoder name, vendor). AV1 for Ada/RDNA3/Arc; VideoToolbox for macOS.
HW_ENCODERS_TO_TEST = [
    ('h264_nvenc', 'nvidia'),
    ('hevc_nvenc', 'nvidia'),
    ('av1_nvenc', 'nvidia'),
    ('h264_amf', 'amd'),
    ('hevc_amf', 'amd'),
    ('av1_amf', 'amd'),
    ('h264_qsv', 'intel'),
    ('hevc_qsv', 'intel'),
    ('av1_qsv', 'intel'),
    # Apple VideoToolbox (Intel Mac + Apple Silicon)
    ('h264_videotoolbox', 'apple'),
    ('hevc_videotoolbox', 'apple'),
    ('prores_videotoolbox', 'apple'),
]

# Software / intermediate encoders we just check for presence (no driver needed).
SW_ENCODERS_OF_INTEREST = [
    'libx264', 'libx265', 'libsvtav1', 'libaom-av1', 'prores_ks', 'dnxhd',
]

_CAPS_CACHE_FIELDS = frozenset({
    'caps_schema',
    'working_encoders',
    'available_encoders',
    'nvidia',
    'amd',
    'intel',
    'apple',
    'gpu_name',
    'cuda_available',
    'ffmpeg_version',
    'timestamp',
    'ffmpeg_path',
    'ffmpeg_mtime',
})
_HW_ENCODER_NAMES = frozenset(name for name, _ in HW_ENCODERS_TO_TEST)
_KNOWN_ENCODER_NAMES = _HW_ENCODER_NAMES | frozenset(SW_ENCODERS_OF_INTEREST)
_CAPABILITY_BOOLEAN_FIELDS = (
    'nvidia',
    'amd',
    'intel',
    'apple',
    'cuda_available',
)


def _finite_number(value) -> bool:
    """True only for a finite JSON number (never bool or numeric text)."""
    return (
        type(value) in (int, float)
        and math.isfinite(float(value))
    )


def _bounded_single_line(value, *, limit: int, allow_none: bool = False) -> bool:
    if allow_none and value is None:
        return True
    return (
        type(value) is str
        and 0 < len(value) <= limit
        and value == value.strip()
        and all(character.isprintable() for character in value)
    )


def _unique_json_object(pairs):
    """Reject duplicate JSON keys instead of silently trusting the last value."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'duplicate JSON key: {key}')
        value[key] = item
    return value


def _validated_cache_payload(
        data, *, ffmpeg_path, ffmpeg_mtime, now):
    """Return an exact, fail-closed cache payload or None.

    The app-data cache is not an authority for positive hardware claims.
    Strictly shaped allowlisted positives may narrow a bounded live reprobe;
    they are never applied directly.
    """
    if type(data) is not dict or frozenset(data) != _CAPS_CACHE_FIELDS:
        return None
    if type(data['caps_schema']) is not int or data['caps_schema'] != _CAPS_SCHEMA:
        return None

    working = data['working_encoders']
    available = data['available_encoders']
    if type(working) is not list or type(available) is not list:
        return None
    if len(working) > _CACHE_MAX_WORKING_HINTS:
        return None
    if len(available) > len(_KNOWN_ENCODER_NAMES):
        return None
    if (
        any(type(name) is not str or name not in _HW_ENCODER_NAMES for name in working)
        or any(type(name) is not str or name not in _KNOWN_ENCODER_NAMES for name in available)
        or len(set(working)) != len(working)
        or len(set(available)) != len(available)
        or not set(working).issubset(available)
    ):
        return None

    if any(type(data[field]) is not bool for field in _CAPABILITY_BOOLEAN_FIELDS):
        return None
    working_vendors = {
        vendor
        for name, vendor in HW_ENCODERS_TO_TEST
        if name in working
    }
    if any(
        data[vendor] is not (vendor in working_vendors)
        for vendor in ('nvidia', 'amd', 'intel', 'apple')
    ):
        return None

    if not _bounded_single_line(data['gpu_name'], limit=256):
        return None
    if not _bounded_single_line(
            data['ffmpeg_version'], limit=512, allow_none=True):
        return None
    if (
        type(data['ffmpeg_path']) is not str
        or not _bounded_single_line(data['ffmpeg_path'], limit=32767)
        or data['ffmpeg_path'] != ffmpeg_path
    ):
        return None

    timestamp = data['timestamp']
    cached_mtime = data['ffmpeg_mtime']
    if not _finite_number(timestamp) or not _finite_number(cached_mtime):
        return None
    if (
        float(timestamp) < 0
        or float(timestamp) > float(now) + _CACHE_MAX_FUTURE_SKEW_S
        or float(now) - float(timestamp) >= _CACHE_MAX_AGE_S
        or float(cached_mtime) < 0
        or float(cached_mtime) != float(ffmpeg_mtime)
    ):
        return None
    return data


@dataclass(frozen=True)
class FFmpegResolution:
    path: str | None
    version_line: str | None
    managed: bool


def resolve_ffmpeg(
        *, managed_path_func=None, probe_pair_func=None, runner=None,
        which=None, cwd=None, platform=None,
        application_dir=None) -> FFmpegResolution:
    """Resolve FFmpeg without GPU probes, shared by runtime and update scans."""
    injected_managed_resolver = (
        managed_path_func is not None or probe_pair_func is not None
    )
    managed_path_func = managed_path_func or managed_ffmpeg_path
    probe_pair_func = probe_pair_func or probe_pair
    runner = runner or subprocess.run
    del cwd  # Retained as a compatibility seam; caller cwd is never executable authority.
    platform = platform or sys.platform
    try:
        recover_interrupted_activation(runner=runner)
    except Exception:
        logger.warning('Managed FFmpeg recovery unavailable; using discovery fallbacks')
    if injected_managed_resolver:
        # Explicit test/application seams retain their supplied resolver contract.
        try:
            managed = Path(managed_path_func())
            version, ffmpeg_path, _ = probe_pair_func(managed.parent.parent)
            return FFmpegResolution(
                str(ffmpeg_path),
                f'ffmpeg version {version}',
                True,
            )
        except Exception:
            pass
    else:
        try:
            version, ffmpeg_path, _ = certified_managed_pair(runner=runner)
            return FFmpegResolution(
                str(ffmpeg_path),
                f'ffmpeg version {version}',
                True,
            )
        except Exception:
            pass

    candidates: list[str] = []
    trusted_application_dir = Path(
        application_dir
        if application_dir is not None
        else Path(__file__).resolve().parent
    )
    if trusted_application_dir.is_absolute():
        for name in ('ffmpeg.exe', 'ffmpeg'):
            bundled = trusted_application_dir / name
            try:
                if bundled.is_file():
                    candidates.append(str(bundled))
            except OSError:
                continue
    if which is None:
        found = explicit_path_executable(
            ('ffmpeg.exe', 'ffmpeg'),
            platform=str(platform),
        )
        if found is not None:
            candidates.append(str(found))
    else:
        # An injected resolver remains a test/application seam, but it still
        # must return a real absolute file so no bare-name execution escapes.
        for name in ('ffmpeg.exe', 'ffmpeg'):
            try:
                found = which(name)
                candidate = Path(found) if found else None
                if (
                    candidate is not None
                    and candidate.is_absolute()
                    and candidate.is_file()
                ):
                    candidates.append(str(candidate))
            except OSError:
                continue
    seen = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen or not Path(candidate).is_file():
            continue
        seen.add(key)
        try:
            result = runner(
                [candidate, '-version'], capture_output=True, text=True, timeout=6)
            if result.returncode == 0:
                output = result.stdout or result.stderr or ''
                line = output.splitlines()[0] if output.splitlines() else None
                return FFmpegResolution(candidate, line, False)
        except Exception:
            continue
    return FFmpegResolution(None, None, False)


class CapabilityDetector:
    def __init__(self):
        self._ffmpeg_version_line = None  # stashed by _find_ffmpeg (gpu_info doesn't exist yet there)
        self.ffmpeg_path = self._find_ffmpeg()
        self.gpu_info = {
            'nvidia': False,
            'amd': False,
            'intel': False,
            'apple': False,
            'working_encoders': [],      # HW encoders verified via real null-encode
            'available_encoders': set(),  # everything ffmpeg -encoders lists (sw + hw)
            'gpu_name': 'Unknown',
            'cuda_available': False,
        }
        # Assign the version line stashed during _find_ffmpeg (avoids a redundant `ffmpeg -version`).
        if self._ffmpeg_version_line:
            self.gpu_info['ffmpeg_version'] = self._ffmpeg_version_line
        self._encoders_text = ''
        self._detect_all()

    def _find_ffmpeg(self):
        resolution = resolve_ffmpeg()
        self._ffmpeg_version_line = resolution.version_line
        if resolution.version_line:
            logger.info(f"[OK] FFmpeg: {resolution.path}")
            return resolution.path
        logger.warning("⚠️ FFmpeg not found in PATH. Some features will be limited.")
        return None

    def _resolve_ffmpeg_mtime(self):
        """Return the selected absolute FFmpeg binary mtime, or None."""
        path = self.ffmpeg_path
        try:
            if path and os.path.isabs(path) and Path(path).is_file():
                return os.path.getmtime(path)
        except Exception:
            pass
        return None

    def _load_cache(self):
        """Return a strictly validated, fail-closed cache dict, or None."""
        if os.getenv('YT_FINAL_NO_CAPS_CACHE'):
            return None
        try:
            path_stat = os.lstat(_CACHE_FILE)
            reparse_flag = getattr(
                stat,
                'FILE_ATTRIBUTE_REPARSE_POINT',
                0x400,
            )
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or stat.S_ISLNK(path_stat.st_mode)
                or (
                    getattr(path_stat, 'st_file_attributes', 0)
                    & reparse_flag
                )
                or path_stat.st_size > _CACHE_MAX_BYTES
            ):
                return None
            with open(_CACHE_FILE, 'rb') as fh:
                opened_stat = os.fstat(fh.fileno())
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or (
                        opened_stat.st_dev,
                        opened_stat.st_ino,
                    ) != (
                        path_stat.st_dev,
                        path_stat.st_ino,
                    )
                    or opened_stat.st_size > _CACHE_MAX_BYTES
                ):
                    return None
                raw = fh.read(_CACHE_MAX_BYTES + 1)
                if len(raw) > _CACHE_MAX_BYTES:
                    return None
            data = json.loads(
                raw.decode('utf-8'),
                object_pairs_hook=_unique_json_object,
            )
            cur_mtime = self._resolve_ffmpeg_mtime()
            if cur_mtime is None:
                return None  # can't resolve ffmpeg -> treat cache as invalid
            return _validated_cache_payload(
                data,
                ffmpeg_path=self.ffmpeg_path,
                ffmpeg_mtime=cur_mtime,
                now=time.time(),
            )
        except Exception:
            return None

    def _apply_cache(self, data):
        """Apply only safe-negative cache claims after fresh encoder listing."""
        del data
        self._reset_hardware_claims()

    def _reset_hardware_claims(self):
        self.gpu_info['working_encoders'] = []
        self.gpu_info['gpu_name'] = 'Unknown'
        self.gpu_info['cuda_available'] = False
        for vendor in ('nvidia', 'amd', 'intel', 'apple'):
            self.gpu_info[vendor] = False

    @staticmethod
    def _cache_has_positive_hints(data):
        return bool(
            data['working_encoders']
            or any(data[field] for field in _CAPABILITY_BOOLEAN_FIELDS)
        )

    def _revalidate_cached_positive_hints(self, data):
        """Reproduce cached positives with bounded live probes.

        The writable cache only narrows which encoders to retry. Fresh
        ``ffmpeg -encoders`` output, live smoke probes, and live CUDA probing
        remain the authority for every capability used by a job.
        """
        hints = list(data['working_encoders'])
        available = set(self.gpu_info.get('available_encoders') or ())
        if (
            not hints
            or len(hints) > _CACHE_MAX_WORKING_HINTS
            or any(name not in available for name in hints)
        ):
            return False

        self._reset_hardware_claims()
        deadline = time.monotonic() + _CACHE_REVALIDATION_BUDGET_S
        if data['cuda_available'] or any('nvenc' in name for name in hints):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            cuda_available = self._probe_cuda(
                timeout=min(4.0, remaining),
            )
            if data['cuda_available'] and not cuda_available:
                return False

        vendors = dict(HW_ENCODERS_TO_TEST)
        for encoder in hints:
            if time.monotonic() >= deadline:
                self._reset_hardware_claims()
                return False
            if not self._test_encoder_comprehensive(
                encoder,
                deadline=deadline,
            ):
                self._reset_hardware_claims()
                return False
            self.gpu_info['working_encoders'].append(encoder)
            self.gpu_info[vendors[encoder]] = True
        return True

    def _save_cache(self):
        """Persist the detected capabilities. Failures never break detection."""
        if os.getenv('YT_FINAL_NO_CAPS_CACHE'):
            return
        try:
            cache_dir, cache_file = _validated_cache_paths()
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_dir, cache_file = _validated_cache_paths()
            payload = {
                'caps_schema': _CAPS_SCHEMA,
                'working_encoders': list(self.gpu_info.get('working_encoders', [])),
                'available_encoders': list(self.gpu_info.get('available_encoders', set())),
                'nvidia': bool(self.gpu_info.get('nvidia', False)),
                'amd': bool(self.gpu_info.get('amd', False)),
                'intel': bool(self.gpu_info.get('intel', False)),
                'apple': bool(self.gpu_info.get('apple', False)),
                'gpu_name': self.gpu_info.get('gpu_name', 'Unknown'),
                'cuda_available': bool(self.gpu_info.get('cuda_available', False)),
                'ffmpeg_version': self.gpu_info.get('ffmpeg_version'),
                'timestamp': time.time(),
                'ffmpeg_path': self.ffmpeg_path,
                'ffmpeg_mtime': self._resolve_ffmpeg_mtime(),
            }
            # Atomic write: dump to a temp file in the same dir, then os.replace onto the cache file.
            # Concurrent CapabilityDetector() constructions (e.g. the threaded /api/probe handler that
            # bypasses the get_detector() singleton) can otherwise interleave plain open()+json.dump
            # calls and leave a truncated/corrupt caps_cache.json. The lock serializes writers within
            # the process; os.replace makes each publish atomic (readers never see a partial file).
            data = json.dumps(payload)
            with _cache_write_lock:
                tmp = None
                try:
                    cache_dir, cache_file = _validated_cache_paths()
                    fd, tmp = tempfile.mkstemp(
                        dir=str(cache_dir),
                        prefix='.caps_cache.',
                        suffix='.tmp',
                    )
                    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                        fh.write(data)
                    cache_dir, cache_file = _validated_cache_paths()
                    os.replace(tmp, cache_file)
                    tmp = None
                except Exception:
                    if tmp is not None:
                        try:
                            _unlink_validated_cache_leaf(Path(tmp))
                        except (OSError, UnsafeInstallerRootError):
                            pass
                    raise
        except Exception:
            pass

    def _detect_all(self):
        # Cache values are hints. Fresh enumeration plus bounded targeted
        # probes reproduce positives before any cached capability is used.
        cached = self._load_cache()
        if cached is not None:
            self._test_basic_ffmpeg()
            self._cache_encoders_list()
            if not self._cache_has_positive_hints(cached):
                self._apply_cache(cached)
                logger.info(
                    "[Detect] Revalidated safe-negative capability cache."
                )
                logger.info("[GPU] Working HW encoders: ['cpu only']")
                return
            if self._revalidate_cached_positive_hints(cached):
                if self.gpu_info.get('nvidia'):
                    self._get_nvidia_details()
                if self.gpu_info.get('apple'):
                    self._get_apple_gpu_details()
                logger.info(
                    "[Detect] Revalidated cached capability hints with live probes."
                )
                logger.info(
                    f"[GPU] Working HW encoders: "
                    f"{self.gpu_info['working_encoders']}"
                )
                self._save_cache()
                return
            # Stale/forged positive hints fall through to the authoritative
            # complete probe, starting from no cached hardware claims.
            self._reset_hardware_claims()

        logger.info("\n[Detect] Detecting hardware & FFmpeg capabilities...")
        self._test_basic_ffmpeg()
        if cached is None:
            self._cache_encoders_list()
        self._detect_cuda_and_encoders()
        self._get_nvidia_details()
        self._get_apple_gpu_details()
        logger.info(f"[GPU] Working HW encoders: {self.gpu_info['working_encoders'] or ['cpu only']}")
        # Persist for next construction. Failures never break detection.
        self._save_cache()

    def _test_basic_ffmpeg(self):
        # Reuse the version line stashed by _find_ffmpeg if present (avoids a redundant probe).
        if self.gpu_info.get('ffmpeg_version') or not self.ffmpeg_path:
            return
        try:
            res = subprocess.run([self.ffmpeg_path, '-version'], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                self.gpu_info['ffmpeg_version'] = res.stdout.splitlines()[0]
        except Exception:
            pass

    def _cache_encoders_list(self):
        """Run `ffmpeg -encoders` ONCE and cache it (avoids N subprocess calls during probing)."""
        if not self.ffmpeg_path:
            self._encoders_text = ''
            self.gpu_info['available_encoders'] = set()
            return
        try:
            res = subprocess.run([self.ffmpeg_path, '-hide_banner', '-encoders'],
                                 capture_output=True, text=True, timeout=8)
            self._encoders_text = res.stdout or ''
        except Exception:
            self._encoders_text = ''
        present = set()
        for enc, _ in HW_ENCODERS_TO_TEST:
            if enc in self._encoders_text:
                present.add(enc)
        for enc in SW_ENCODERS_OF_INTEREST:
            if enc in self._encoders_text:
                present.add(enc)
        self.gpu_info['available_encoders'] = present

    def _detect_cuda_and_encoders(self):
        """Real null-encode probe per HW encoder (build can list an encoder the GPU can't run, e.g.
        av1_nvenc on pre-Ada cards), inspired by video_encoder.py + 2.4."""
        if not self.ffmpeg_path:
            return

        self._probe_cuda()

        for enc_name, vendor in HW_ENCODERS_TO_TEST:
            if enc_name not in self.gpu_info['available_encoders']:
                continue
            if self._test_encoder_comprehensive(enc_name):
                self.gpu_info['working_encoders'].append(enc_name)
                self.gpu_info[vendor] = True

    def _probe_cuda(self, *, timeout=12):
        """Live CUDA initialization probe; cached values never bypass it."""
        if not self.ffmpeg_path or timeout <= 0:
            return False
        try:
            cmd = [self.ffmpeg_path, '-hide_banner', '-loglevel', 'error',
                   '-f', 'lavfi', '-i', 'color=black:size=320x240:duration=0.2',
                   '-init_hw_device', 'cuda', '-f', 'null', '-']
            if subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
            ).returncode == 0:
                self.gpu_info['cuda_available'] = True
                return True
        except Exception:
            pass
        self.gpu_info['cuda_available'] = False
        return False

    def _test_encoder_comprehensive(self, encoder, *, deadline=None):
        """Real encode of a tiny lavfi clip to null with the right pix_fmt; the reliable usability gate."""
        def timeout_for(default):
            if deadline is None:
                return default
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                return None
            return min(float(default), remaining)

        # ProRes VideoToolbox wants 10-bit 422; others accept yuv420p for a smoke probe.
        if encoder == 'prores_videotoolbox':
            pix = 'yuv422p10le'
            bitrate_args = []  # ProRes is quality/profile based, not -b:v
        else:
            pix = 'yuv420p'
            bitrate_args = ['-b:v', '1M']
        # System-memory test encode (works for NVENC/AMF/QSV/VideoToolbox without explicit hwaccel).
        try:
            timeout = timeout_for(20)
            if timeout is None:
                return False
            cmd = [self.ffmpeg_path, '-hide_banner', '-loglevel', 'error',
                   '-f', 'lavfi', '-i', 'color=red:size=320x240:duration=0.3:rate=30',
                   '-c:v', encoder, *bitrate_args, '-pix_fmt', pix, '-f', 'null', '-']
            if subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
            ).returncode == 0:
                return True
        except Exception:
            pass

        # CUDA surfaces attempt (only NVENC + CUDA present)
        if self.gpu_info.get('cuda_available') and 'nvenc' in encoder:
            try:
                timeout = timeout_for(20)
                if timeout is None:
                    return False
                cmd = [self.ffmpeg_path, '-hide_banner', '-loglevel', 'error',
                       '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
                       '-f', 'lavfi', '-i', 'color=blue:size=320x240:duration=0.3:rate=30',
                       '-c:v', encoder, '-b:v', '1M', '-pix_fmt', pix, '-f', 'null', '-']
                if subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout,
                ).returncode == 0:
                    return True
            except Exception:
                pass

        return False

    def _get_nvidia_details(self):
        executable = explicit_path_executable(
            ('nvidia-smi.exe', 'nvidia-smi'),
            platform=sys.platform,
        )
        if executable is None:
            return
        try:
            res = subprocess.run(
                [
                    str(executable),
                    '--query-gpu=name,driver_version',
                    '--format=csv,noheader',
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                self.gpu_info['gpu_name'] = res.stdout.strip().split(',')[0].strip()
        except Exception:
            pass

    def _get_apple_gpu_details(self):
        """Name the GPU on macOS (Apple Silicon / AMD / Intel via system_profiler)."""
        if not str(sys.platform).startswith('darwin'):
            return
        system_profiler = Path('/usr/sbin/system_profiler')
        # Prefer name when VideoToolbox is working; still fill when only SW encoders exist.
        try:
            if not system_profiler.is_file():
                raise FileNotFoundError(system_profiler)
            res = subprocess.run(
                [str(system_profiler), 'SPDisplaysDataType', '-json'],
                capture_output=True, text=True, timeout=20,
            )
            if res.returncode == 0 and (res.stdout or '').strip():
                data = json.loads(res.stdout)
                for entry in data.get('SPDisplaysDataType') or []:
                    name = (
                        entry.get('sppci_model')
                        or entry.get('_name')
                        or entry.get('spdisplays_device-id')
                    )
                    if name:
                        self.gpu_info['gpu_name'] = str(name).strip()
                        break
        except Exception:
            pass
        if self.gpu_info.get('gpu_name') in (None, '', 'Unknown'):
            sysctl = Path('/usr/sbin/sysctl')
            try:
                if not sysctl.is_file():
                    raise FileNotFoundError(sysctl)
                res = subprocess.run(
                    [str(sysctl), '-n', 'machdep.cpu.brand_string'],
                    capture_output=True, text=True, timeout=5,
                )
                brand = (res.stdout or '').strip()
                if brand:
                    # Apple Silicon brand is the chip name; Intel Macs still get a useful label.
                    self.gpu_info['gpu_name'] = brand
            except Exception:
                pass
        self.gpu_info['apple'] = any(
            'videotoolbox' in encoder
            for encoder in self.gpu_info.get('working_encoders') or []
        )

    def encoder_usable(self, encoder):
        """True if this encoder can be used: HW encoders must be in working_encoders;
        software encoders just need to be present in the build."""
        if not encoder or encoder == 'copy':
            return True
        if any(tag in encoder for tag in ('nvenc', '_amf', '_qsv', 'videotoolbox')):
            return encoder in self.gpu_info['working_encoders']
        # software encoder: present in the build is sufficient
        return encoder in self.gpu_info['available_encoders'] or encoder in (
            'libx264',  # libx264 is essentially always present; trust it
        )

    def get_recommended_encoder(self):
        for enc in ['h264_nvenc', 'hevc_nvenc']:
            if enc in self.gpu_info['working_encoders']:
                return 'nvidia', enc
        if 'h264_amf' in self.gpu_info['working_encoders']:
            return 'amd', 'h264_amf'
        if 'h264_qsv' in self.gpu_info['working_encoders']:
            return 'intel', 'h264_qsv'
        if 'h264_videotoolbox' in self.gpu_info['working_encoders']:
            return 'apple', 'h264_videotoolbox'
        if 'hevc_videotoolbox' in self.gpu_info['working_encoders']:
            return 'apple', 'hevc_videotoolbox'
        return 'cpu', 'libx264'

    def benchmark_short_encode(self, encoder='h264_nvenc'):
        """Quick real benchmark (inspired by 2.4)"""
        if not self.ffmpeg_path:
            return None
        logger.info(f"⏱️  Benchmarking {encoder} (short 3s clip)...")
        start = time.time()
        try:
            cmd = [self.ffmpeg_path, '-hide_banner', '-loglevel', 'error',
                   '-f', 'lavfi', '-i', 'color=c=black:s=1920x1080:d=3',
                   '-c:v', encoder, '-b:v', '5M',
                   '-pix_fmt', 'yuv420p', '-f', 'null', '-']
            subprocess.run(cmd, timeout=25)
            elapsed = time.time() - start
            logger.info(f"   → {elapsed:.1f}s for 3s clip")
            return elapsed
        except Exception as e:
            logger.error(f"   Benchmark failed: {e}")
            return None


def get_detector():
    """Return a lazily-created, thread-safe process-wide singleton CapabilityDetector.

    Production callers should use this instead of constructing CapabilityDetector()
    directly so the expensive detection (or cache load) runs at most once per process.
    The constructor itself remains fully usable (tests construct it directly).
    """
    global _detector_singleton
    if _detector_singleton is None:
        with _detector_lock:
            if _detector_singleton is None:
                _detector_singleton = CapabilityDetector()
    return _detector_singleton


def invalidate_capability_cache():
    """Drop process and disk capability caches after replacing managed FFmpeg."""
    global _detector_singleton
    with _detector_lock:
        _detector_singleton = None
    with _cache_write_lock:
        try:
            _cache_dir, cache_file = _validated_cache_paths()
            _unlink_validated_cache_leaf(cache_file)
        except (FileNotFoundError, OSError, UnsafeInstallerRootError):
            pass
