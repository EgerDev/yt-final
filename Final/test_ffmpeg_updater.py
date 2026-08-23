from __future__ import annotations

import io
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import os
import stat
import subprocess
import threading
import urllib.request
import zipfile

import pytest

import capability
import ffmpeg_updater
import installer_lock
import yt_final
from capability import CapabilityDetector
from ffmpeg_updater import (
    FFmpegUpdateError,
    ManagedFFmpegUpdater,
    RELEASE_ARCHIVE_URL,
    fetch_release,
    managed_ffmpeg_path,
    managed_root,
    parse_ffmpeg_version,
    probe_pair,
    version_tuple,
)
from yt_final import FFmpegProcessor
from installer_lock import InstallerBusyError


@pytest.fixture(autouse=True)
def _default_win32_platform_for_gyan_fixtures(monkeypatch, request, tmp_path):
    """Most updater fixtures model the Windows Gyan layout; opt out via test name."""
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local-app-data'))
    name = request.node.name.lower()
    if any(token in name for token in (
            'darwin', 'macos', 'linux', 'unsupported_platform', 'non_windows')):
        return
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    # The managed contract is Windows *x86_64*, not merely Windows, so faking only the OS left
    # the real host architecture in play. On an arm64 runner that made
    # managed_install_supported('win32', 'arm64') False, and every install() below short-circuited
    # to status='unsupported' before reaching the archive/traversal/rollback assertion under test.
    # Pin the architecture the same way the OS is pinned ('AMD64' is what a real Windows host
    # reports, so the manifest alias table is still exercised).
    monkeypatch.setattr(ffmpeg_updater, '_machine', lambda: 'AMD64', raising=False)


class FakeResponse(io.BytesIO):
    def __init__(self, payload, final_url='https://www.gyan.dev/response'):
        super().__init__(payload)
        self._final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def geturl(self):
        return self._final_url


def make_archive(entries):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return payload.getvalue()


def make_release_opener(
    archive_payload,
    *,
    monkeypatch=None,
    checksum=None,
    final_url=RELEASE_ARCHIVE_URL,
):
    actual_checksum = hashlib.sha256(archive_payload).hexdigest()
    release = ffmpeg_updater.FFmpegRelease(
        version='8.1.2',
        archive_url=RELEASE_ARCHIVE_URL,
        sha256=checksum or actual_checksum,
    )
    if monkeypatch is not None:
        monkeypatch.setattr(
            ffmpeg_updater,
            'fetch_release',
            lambda **_kwargs: release,
        )

    def opener(url, timeout):
        assert url == RELEASE_ARCHIVE_URL
        return FakeResponse(archive_payload, final_url=final_url)

    return opener


def successful_runner(command, **kwargs):
    executable = Path(command[0])
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=f'{executable.stem} version 8.1.2-essentials_build-www.gyan.dev\n',
        stderr='',
    )


def run_install(monkeypatch, tmp_path, archive_payload, **kwargs):
    """Drive the Windows Gyan install path (isolated via LOCALAPPDATA + win32/x86_64)."""
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    # Pin the architecture too: the certified artifact is win32/x86_64, so on an arm64 host
    # install() would return 'unsupported' before any of the callers' assertions could run.
    monkeypatch.setattr(ffmpeg_updater, '_machine', lambda: 'AMD64', raising=False)
    return ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(
            archive_payload,
            monkeypatch=monkeypatch,
            **kwargs,
        ),
        runner=successful_runner,
    )


def valid_archive():
    return make_archive(
        [
            ('ffmpeg-8.1.2-essentials_build/bin/ffmpeg.exe', b'new ffmpeg'),
            ('ffmpeg-8.1.2-essentials_build/bin/ffprobe.exe', b'new ffprobe'),
        ]
    )


def test_parse_ffmpeg_version_accepts_gyan_output():
    text = 'ffmpeg version 8.1.2-essentials_build-www.gyan.dev Copyright'
    assert parse_ffmpeg_version(text) == '8.1.2'


@pytest.mark.parametrize('text', ['', 'ffmpeg version n8.1', 'version 8.1.2'])
def test_parse_ffmpeg_version_rejects_non_gyan_output(text):
    assert parse_ffmpeg_version(text) is None


def test_version_tuple_returns_numeric_components_only():
    assert version_tuple('8.1.2') == (8, 1, 2)
    assert version_tuple(None) == ()
    assert version_tuple('8.1-beta') == ()


def test_fetch_release_uses_fixed_manifest_without_metadata_io(monkeypatch):
    """Release selection is a pure host lookup; no mutable metadata endpoint is opened."""
    from tool_manifest import artifact_for

    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    monkeypatch.setattr(ffmpeg_updater, '_machine', lambda: 'AMD64', raising=False)

    class MetadataIOReached(BaseException):
        pass

    release = fetch_release(
        opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MetadataIOReached('fixed manifest lookup opened metadata I/O')
        )
    )
    approved = artifact_for('ffmpeg', 'win32', 'AMD64')

    assert release == ffmpeg_updater.FFmpegRelease(
        version=approved.version,
        archive_url=approved.url,
        sha256=approved.sha256,
    )


@pytest.mark.parametrize(
    ('host_platform', 'host_machine'),
    [
        ('darwin', 'arm64'),
        ('darwin', 'x86_64'),
        ('win32', 'ARM64'),
        ('linux', 'x86_64'),
    ],
)
def test_unsupported_ffmpeg_target_rejects_before_guard_or_any_io(
        monkeypatch, host_platform, host_machine):
    """Only Windows x86_64 may enter the shared installer guard."""
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: host_platform, raising=False)
    monkeypatch.setattr(ffmpeg_updater, '_machine', lambda: host_machine, raising=False)

    class ForbiddenIO(BaseException):
        pass

    def forbidden(*_args, **_kwargs):
        raise ForbiddenIO('unsupported target reached mutation or probe I/O')

    monkeypatch.setattr(ffmpeg_updater, 'installer_guard', forbidden)
    monkeypatch.setattr(ffmpeg_updater, 'managed_root', forbidden)
    monkeypatch.setattr(ffmpeg_updater.tempfile, 'mkdtemp', forbidden)

    result = ManagedFFmpegUpdater().install(
        busy=forbidden,
        opener=forbidden,
        runner=forbidden,
    )

    assert result['ok'] is False
    assert result['status'] == 'unsupported'
    assert result['code'] == 'unsupported'


def test_ffmpeg_download_rejects_wrong_final_url_before_body_or_file_io(
        monkeypatch, tmp_path):
    """An injected opener cannot make the updater read bytes from any unapproved final URL."""
    from tool_manifest import artifact_for

    artifact = artifact_for('ffmpeg', 'win32', 'AMD64')

    class BodyReadReached(BaseException):
        pass

    class WrongFinalResponse(FakeResponse):
        def __init__(self):
            super().__init__(b'', final_url='https://www.gyan.dev/unapproved.zip')

        def read(self, size=-1):
            raise BodyReadReached('wrong final URL body was read')

    destination = tmp_path / 'release.zip'
    with pytest.raises(FFmpegUpdateError):
        ffmpeg_updater._download_url_to_path(
            artifact.url,
            destination,
            artifact.sha256,
            lambda *_args, **_kwargs: WrongFinalResponse(),
        )

    assert not destination.exists()


def test_ffmpeg_download_rejects_hashless_release_before_opener_or_file_io(
        tmp_path):
    """A source URL is never enough provenance without its approved digest."""
    destination = tmp_path / 'release.zip'

    class ForbiddenIO(BaseException):
        pass

    with pytest.raises(FFmpegUpdateError) as raised:
        ffmpeg_updater._download_url_to_path(
            'https://example.invalid/ffmpeg-8.1.2.zip',
            destination,
            '',
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ForbiddenIO('hashless release opened the network')
            ),
        )

    assert raised.value.code == 'checksum'
    assert not destination.exists()


def test_fetch_release_returns_exact_approved_windows_artifact():
    from tool_manifest import artifact_for

    release = fetch_release(platform='win32', machine='AMD64')
    artifact = artifact_for('ffmpeg', 'win32', 'x86_64')

    assert release.version == artifact.version
    assert release.sha256 == artifact.sha256
    assert release.archive_url == artifact.url
    assert release.artifact == artifact


@pytest.mark.parametrize(
    ('platform_name', 'machine_name'),
    [
        ('darwin', 'arm64'),
        ('darwin', 'x86_64'),
        ('win32', 'arm64'),
        ('linux', 'x86_64'),
    ],
)
def test_fetch_release_rejects_uncertified_target_without_opening(
        platform_name, machine_name):
    class OpenerReached(BaseException):
        pass

    with pytest.raises(FFmpegUpdateError) as raised:
        fetch_release(
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OpenerReached('uncertified target opened metadata')
            ),
            platform=platform_name,
            machine=machine_name,
        )
    assert raised.value.code == 'unsupported'


@pytest.mark.parametrize(
    'redirect_url',
    [
        'http://mirror.invalid/intermediate',
        'https://mirror.invalid/intermediate',
        RELEASE_ARCHIVE_URL,
    ],
)
def test_redirect_handler_rejects_every_intermediate_hop(redirect_url):
    handler = ffmpeg_updater.HTTPSOnlyRedirectHandler()
    request = urllib.request.Request(RELEASE_ARCHIVE_URL)

    with pytest.raises(FFmpegUpdateError, match='redirect'):
        handler.redirect_request(
            request,
            None,
            302,
            'Found',
            {},
            redirect_url,
        )


def test_download_rejects_direct_http_archive_before_opening(tmp_path):
    release = ffmpeg_updater.FFmpegRelease(
        '8.1.2',
        'http://www.gyan.dev/ffmpeg/builds/release.zip',
        hashlib.sha256(b'').hexdigest(),
    )

    def unexpected_open(*args, **kwargs):
        raise AssertionError('an insecure archive URL must not be opened')

    with pytest.raises(FFmpegUpdateError, match='HTTPS'):
        ffmpeg_updater._download_archive(release, tmp_path / 'release.zip', unexpected_open)


def test_download_enforces_total_wall_clock_deadline_without_sleeping(
        monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_updater, 'ARCHIVE_TOTAL_TIMEOUT_SECONDS', 10)
    ticks = iter((100.0, 105.0, 111.0))
    reads = []

    class SlowDripResponse(FakeResponse):
        def __init__(self):
            super().__init__(b'', final_url=RELEASE_ARCHIVE_URL)
            self._chunks = iter((b'a', b'b', b''))

        def read(self, size=-1):
            reads.append(size)
            return next(self._chunks)

    release = ffmpeg_updater.FFmpegRelease(
        '8.1.2', RELEASE_ARCHIVE_URL, hashlib.sha256(b'ab').hexdigest())

    with pytest.raises(FFmpegUpdateError, match='deadline'):
        ffmpeg_updater._download_archive(
            release,
            tmp_path / 'release.zip',
            lambda url, timeout: SlowDripResponse(),
            clock=lambda: next(ticks),
        )

    assert reads == [ffmpeg_updater.DOWNLOAD_CHUNK_BYTES] * 2


def test_managed_paths_are_scoped_to_local_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    assert managed_root() == tmp_path / 'local' / 'yt_final' / 'ffmpeg'
    assert managed_ffmpeg_path() == managed_root() / 'current' / 'bin' / 'ffmpeg.exe'


def test_macos_paths_remain_discoverable_but_managed_install_is_disabled(
        monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'darwin', raising=False)
    monkeypatch.setattr(ffmpeg_updater, '_machine', lambda: 'arm64', raising=False)
    monkeypatch.setattr(ffmpeg_updater.Path, 'home', classmethod(lambda cls: tmp_path))
    assert managed_root() == tmp_path / 'Library' / 'Application Support' / 'yt_final' / 'ffmpeg'
    assert managed_ffmpeg_path() == managed_root() / 'current' / 'bin' / 'ffmpeg'
    assert ffmpeg_updater.ffmpeg_binary_name() == 'ffmpeg'
    assert ffmpeg_updater.ffprobe_binary_name() == 'ffprobe'
    assert ffmpeg_updater.managed_install_supported() is False


def test_probe_pair_probes_both_binaries_with_safe_subprocess_arguments(tmp_path):
    root = tmp_path / 'release'
    (root / 'bin').mkdir(parents=True)
    (root / 'bin' / 'ffmpeg.exe').write_bytes(b'ffmpeg')
    (root / 'bin' / 'ffprobe.exe').write_bytes(b'ffprobe')
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return successful_runner(command, **kwargs)

    version, ffmpeg_path, ffprobe_path = probe_pair(root, runner=runner)

    assert version == '8.1.2'
    assert ffmpeg_path == root / 'bin' / 'ffmpeg.exe'
    assert ffprobe_path == root / 'bin' / 'ffprobe.exe'
    assert [call[0] for call in calls] == [
        [str(ffmpeg_path), '-version'],
        [str(ffprobe_path), '-version'],
    ]
    assert all(
        call[1] == {
            'capture_output': True,
            'text': True,
            'timeout': 10,
            'check': True,
        }
        for call in calls
    )


def test_hardware_compatibility_ignores_poisoned_cwd_reference(
        monkeypatch, tmp_path):
    poisoned = tmp_path / 'download-folder'
    poisoned.mkdir()
    (poisoned / 'ffmpeg.exe').write_bytes(b'poison')
    candidate = tmp_path / 'candidate-ffmpeg.exe'
    candidate.write_bytes(b'candidate')
    monkeypatch.chdir(poisoned)
    monkeypatch.setenv('PATH', '')
    monkeypatch.setattr(
        ffmpeg_updater.shutil,
        'which',
        lambda _name: str(poisoned / 'ffmpeg.exe'),
    )
    executed = []

    def forbidden_runner(command, **_kwargs):
        executed.append(list(command))
        return type('Completed', (), {'returncode': 0})()

    ffmpeg_updater.verify_hardware_compatibility(
        candidate,
        runner=forbidden_runner,
        platform='win32',
    )

    assert executed == []


def test_install_rejects_parent_directory_member(monkeypatch, tmp_path):
    archive = make_archive([('../escape.exe', b'escape')])
    result = run_install(monkeypatch, tmp_path, archive)
    assert result['ok'] is False
    assert result['code'] == 'archive'
    assert not (tmp_path / 'escape.exe').exists()


def test_install_rejects_absolute_member_path(monkeypatch, tmp_path):
    archive = make_archive([('/absolute.exe', b'escape')])
    result = run_install(monkeypatch, tmp_path, archive)
    assert result['ok'] is False


def test_install_rejects_drive_prefix_member_path(monkeypatch, tmp_path):
    archive = make_archive([('C:/absolute.exe', b'escape')])
    result = run_install(monkeypatch, tmp_path, archive)
    assert result['ok'] is False


def test_install_rejects_link_like_unix_mode_member(monkeypatch, tmp_path):
    link = zipfile.ZipInfo('ffmpeg-8.1.2-essentials_build/bin/ffmpeg.exe')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = make_archive(
        [
            (link, b'../../outside.exe'),
            ('ffmpeg-8.1.2-essentials_build/bin/ffprobe.exe', b'probe'),
        ]
    )
    result = run_install(monkeypatch, tmp_path, archive)
    assert result['ok'] is False


def test_install_rejects_more_than_500_members(monkeypatch, tmp_path):
    archive = make_archive([(f'payload/member-{index}', b'') for index in range(501)])
    result = run_install(monkeypatch, tmp_path, archive)
    assert result['ok'] is False


def test_install_rejects_more_than_750_mib_declared_expanded_size(monkeypatch, tmp_path):
    archive = bytearray(make_archive([('payload/oversized.bin', b'x')]))
    central_header = archive.index(b'PK\x01\x02')
    archive[central_header + 24:central_header + 28] = (750 * 1024 * 1024 + 1).to_bytes(4, 'little')
    result = run_install(monkeypatch, tmp_path, bytes(archive))
    assert result['ok'] is False


def test_install_rejects_checksum_mismatch(monkeypatch, tmp_path):
    result = run_install(monkeypatch, tmp_path, valid_archive(), checksum='0' * 64)
    assert result['ok'] is False
    assert result['code'] == 'checksum'
    assert result['checksum_verified'] is False
    assert not managed_ffmpeg_path().exists()


def test_checksum_mismatch_preserves_active_pair_and_never_extracts_or_probes(
        monkeypatch, tmp_path):
    """Unverified bytes cannot reach extraction, probes, activation, or metadata."""
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')

    class ForbiddenPostVerifyIO(BaseException):
        pass

    monkeypatch.setattr(
        ffmpeg_updater,
        '_extract_archive',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ForbiddenPostVerifyIO('checksum mismatch reached extraction')
        ),
    )
    result = run_install(
        monkeypatch,
        tmp_path,
        valid_archive(),
        checksum='0' * 64,
    )

    assert result['code'] == 'checksum'
    assert managed_ffmpeg_path().read_bytes() == b'old ffmpeg'
    assert managed_ffmpeg_path().with_name('ffprobe.exe').read_bytes() == b'old ffprobe'
    assert not (managed_root() / 'metadata.json').exists()


def test_install_rejects_http_final_archive_redirect(monkeypatch, tmp_path):
    result = run_install(
        monkeypatch,
        tmp_path,
        valid_archive(),
        final_url='http://www.gyan.dev/ffmpeg-release-essentials.zip',
    )
    assert result['ok'] is False
    assert 'https' in result['message'].lower()


def test_install_stops_when_streaming_download_crosses_cap(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, 'MAX_ARCHIVE_BYTES', 5)
    read_sizes = []

    class ChunkedResponse(FakeResponse):
        def __init__(self):
            super().__init__(b'', final_url=RELEASE_ARCHIVE_URL)
            self._chunks = iter((b'abcd', b'efgh'))

        def read(self, size=-1):
            read_sizes.append(size)
            return next(self._chunks, b'')

    def opener(url, timeout):
        assert url == RELEASE_ARCHIVE_URL
        return ChunkedResponse()

    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=opener,
        runner=successful_runner,
    )
    assert result['ok'] is False
    assert 'download limit' in result['message'].lower()
    assert read_sizes == [ffmpeg_updater.DOWNLOAD_CHUNK_BYTES] * 2


def test_install_rejects_missing_ffprobe(monkeypatch, tmp_path):
    archive = make_archive([('ffmpeg-8.1.2/bin/ffmpeg.exe', b'ffmpeg')])
    result = run_install(monkeypatch, tmp_path, archive)
    assert result['ok'] is False


def test_install_rejects_multiple_candidate_binary_pairs(monkeypatch, tmp_path):
    archive = make_archive(
        [
            ('first/bin/ffmpeg.exe', b'one'),
            ('first/bin/ffprobe.exe', b'one'),
            ('second/bin/ffmpeg.exe', b'two'),
            ('second/bin/ffprobe.exe', b'two'),
        ]
    )
    result = run_install(monkeypatch, tmp_path, archive)
    assert result['ok'] is False


def test_windows_archive_requires_the_approved_single_release_root(
        monkeypatch, tmp_path):
    """A valid pair under an arbitrary archive root is not the approved Gyan layout."""
    archive = make_archive(
        [
            ('unexpected-root/bin/ffmpeg.exe', b'new ffmpeg'),
            ('unexpected-root/bin/ffprobe.exe', b'new ffprobe'),
        ]
    )

    result = run_install(monkeypatch, tmp_path, archive)

    assert result['ok'] is False
    assert result['code'] == 'archive'
    assert not managed_ffmpeg_path().exists()


def test_metadata_records_the_supplied_checksum_verification_state(tmp_path):
    """Metadata must persist the verified result, never hardcode success."""
    root = tmp_path / 'ffmpeg'
    root.mkdir()
    release = ffmpeg_updater.FFmpegRelease(
        version='8.1.2',
        archive_url='https://example.invalid/ffmpeg-8.1.2.zip',
        sha256='a' * 64,
    )

    ffmpeg_updater._write_metadata(
        root,
        release,
        root / 'current' / 'bin' / 'ffmpeg.exe',
        checksum_verified=False,
    )

    metadata = json.loads((root / 'metadata.json').read_text(encoding='utf-8'))
    assert metadata['checksum_verified'] is False


def test_install_activates_valid_release_over_old_current(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')

    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=successful_runner,
    )

    assert result == {
        'ok': True,
        'status': 'installed',
        'version': '8.1.2',
        'path': str(managed_ffmpeg_path()),
        'checksum_verified': True,
        'message': 'Managed FFmpeg 8.1.2 installed',
    }
    assert managed_ffmpeg_path().read_bytes() == b'new ffmpeg'
    assert managed_ffmpeg_path().with_name('ffprobe.exe').read_bytes() == b'new ffprobe'
    assert json.loads((managed_root() / 'metadata.json').read_text(encoding='utf-8'))['version'] == '8.1.2'
    assert not (managed_root() / ffmpeg_updater.ACTIVATION_MARKER_NAME).exists()


def test_install_rejects_release_that_breaks_available_nvidia_encoding(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    system_ffmpeg = tmp_path / 'system' / 'ffmpeg.exe'
    system_ffmpeg.parent.mkdir()
    system_ffmpeg.write_bytes(b'trusted system ffmpeg')
    # A real PATH executable carries the exec bit, and explicit_path_executable() requires
    # os.access(..., X_OK) on POSIX. Without it the NVENC reference resolves to None on
    # macOS/Linux, verify_hardware_compatibility() returns early, and the install is never
    # rejected — so this test asserted ok is False against an install that legitimately
    # succeeded. Windows ignores the mode, so this is a no-op there.
    system_ffmpeg.chmod(0o755)
    monkeypatch.setenv('PATH', str(system_ffmpeg.parent))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')

    def incompatible_runner(command, **kwargs):
        if 'nvidia-smi' in str(command[0]).lower():
            return subprocess.CompletedProcess(command, 0, stdout='NVIDIA GeForce RTX 4090\n', stderr='')
        if (
            '-c:v' in command
            and 'h264_nvenc' in command
            and Path(command[0]) != system_ffmpeg
        ):
            return subprocess.CompletedProcess(
                command, 1, stdout='',
                stderr='Driver does not support the required nvenc API version')
        return successful_runner(command, **kwargs)

    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=incompatible_runner,
    )

    assert result['ok'] is False
    assert result['code'] == 'compatibility'
    assert managed_ffmpeg_path().read_bytes() == b'old ffmpeg'
    assert managed_ffmpeg_path().with_name('ffprobe.exe').read_bytes() == b'old ffprobe'


def test_install_restores_old_current_when_post_swap_probe_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')

    def post_swap_failure(command, **kwargs):
        executable = Path(command[0])
        if executable.parent.parent == managed_root() / 'current' and executable.read_bytes().startswith(b'new'):
            raise subprocess.CalledProcessError(1, command)
        return successful_runner(command, **kwargs)

    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=post_swap_failure,
    )

    assert result['ok'] is False
    assert managed_ffmpeg_path().read_bytes() == b'old ffmpeg'
    assert managed_ffmpeg_path().with_name('ffprobe.exe').read_bytes() == b'old ffprobe'
    assert not (managed_root() / ffmpeg_updater.ACTIVATION_MARKER_NAME).exists()


def test_install_preserves_old_current_when_rollback_swap_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    old_metadata = '{"version":"7.1.1","sentinel":"keep"}'
    (managed_root() / 'metadata.json').write_text(old_metadata, encoding='utf-8')
    real_replace = ffmpeg_updater.os.replace

    def fail_rollback_swap(source, destination):
        if (Path(source) == managed_root() / 'current'
                and Path(destination).name.startswith('.rollback-')):
            raise OSError('simulated rollback swap failure')
        return real_replace(source, destination)

    monkeypatch.setattr(ffmpeg_updater.os, 'replace', fail_rollback_swap)
    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=successful_runner,
    )

    assert result['ok'] is False
    assert managed_ffmpeg_path().is_file()
    assert managed_ffmpeg_path().read_bytes() == b'old ffmpeg'
    assert managed_ffmpeg_path().with_name('ffprobe.exe').read_bytes() == b'old ffprobe'
    assert (managed_root() / 'metadata.json').read_text(encoding='utf-8') == old_metadata
    assert list(managed_root().glob('.rollback-*')) == []


def test_install_restores_old_current_when_staging_swap_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    real_replace = ffmpeg_updater.os.replace

    def fail_staging_swap(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == managed_root() / 'current' and '.ffmpeg-update-' in str(source_path):
            raise OSError('simulated staging swap failure')
        return real_replace(source, destination)

    monkeypatch.setattr(ffmpeg_updater.os, 'replace', fail_staging_swap)
    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=successful_runner,
    )

    assert result['ok'] is False
    assert managed_ffmpeg_path().read_bytes() == b'old ffmpeg'
    assert managed_ffmpeg_path().with_name('ffprobe.exe').read_bytes() == b'old ffprobe'


def test_install_keeps_new_commit_when_rollback_cleanup_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    real_rmtree = ffmpeg_updater.shutil.rmtree

    def fail_rollback_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith('.rollback-'):
            raise OSError('simulated rollback cleanup failure')
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(ffmpeg_updater.shutil, 'rmtree', fail_rollback_cleanup)
    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=successful_runner,
    )

    assert result['ok'] is True
    assert managed_ffmpeg_path().read_bytes() == b'new ffmpeg'
    assert json.loads((managed_root() / 'metadata.json').read_text(encoding='utf-8'))['version'] == '8.1.2'
    rollback = next(managed_root().glob('.rollback-*'))
    assert str(rollback) not in result['message']
    assert 'cleanup deferred' in result['message']
    assert (rollback / 'bin' / 'ffmpeg.exe').read_bytes() == b'old ffmpeg'
    assert (managed_root() / ffmpeg_updater.ACTIVATION_MARKER_NAME).exists()


def test_install_keeps_committed_metadata_and_binaries_when_marker_cleanup_fails(
        monkeypatch, tmp_path):
    """Metadata replacement commits the matching new binary tree before cleanup."""
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    old_metadata = '{"version":"7.1.1","sentinel":"keep"}'
    (managed_root() / 'metadata.json').write_text(old_metadata, encoding='utf-8')

    def fail_marker_cleanup(_root):
        raise OSError('simulated activation marker cleanup failure')

    monkeypatch.setattr(
        ffmpeg_updater,
        '_remove_activation_marker',
        fail_marker_cleanup,
    )
    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=successful_runner,
    )

    assert result['ok'] is True
    assert 'cleanup deferred' in result['message']
    assert managed_ffmpeg_path().read_bytes() == b'new ffmpeg'
    assert managed_ffmpeg_path().with_name('ffprobe.exe').read_bytes() == b'new ffprobe'
    metadata = json.loads(
        (managed_root() / 'metadata.json').read_text(encoding='utf-8'))
    assert metadata['version'] == '8.1.2'
    assert metadata['checksum_verified'] is True


def test_install_reports_preserved_rollback_when_recovery_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    real_replace = ffmpeg_updater.os.replace

    def fail_rollback_restore(source, destination):
        if Path(source).name.startswith('.rollback-') and Path(destination) == managed_root() / 'current':
            raise OSError('simulated rollback recovery failure')
        return real_replace(source, destination)

    def post_swap_failure(command, **kwargs):
        executable = Path(command[0])
        if executable.parent.parent == managed_root() / 'current' and executable.read_bytes().startswith(b'new'):
            raise subprocess.CalledProcessError(1, command)
        return successful_runner(command, **kwargs)

    monkeypatch.setattr(ffmpeg_updater.os, 'replace', fail_rollback_restore)
    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=post_swap_failure,
    )

    assert result['ok'] is False
    rollback = next(managed_root().glob('.rollback-*'))
    assert str(rollback) not in result['message']
    assert result['code'] == 'recovery'
    assert (rollback / 'bin' / 'ffmpeg.exe').read_bytes() == b'old ffmpeg'
    assert not managed_ffmpeg_path().exists()


def test_install_rejects_activation_while_busy(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)

    def unexpected_opener(*args, **kwargs):
        raise AssertionError('network must not be used while busy')

    result = ManagedFFmpegUpdater().install(busy=lambda: True, opener=unexpected_opener)
    assert result['ok'] is False
    assert result['status'] == 'busy'
    assert 'busy' in result['message'].lower()


def test_install_shared_guard_busy_precedes_activity_probe(
        monkeypatch, tmp_path):
    root = tmp_path / 'local' / 'yt_final'
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    monkeypatch.setattr(installer_lock, '_default_root', lambda: root)
    ready = threading.Event()
    release = threading.Event()

    def own_guard():
        with installer_lock.installer_guard(root):
            ready.set()
            assert release.wait(5)

    owner = threading.Thread(target=own_guard)
    owner.start()
    assert ready.wait(5)
    try:
        with pytest.raises(InstallerBusyError):
            ManagedFFmpegUpdater().install(
                busy=lambda: pytest.fail(
                    'shared busy must precede FFmpeg activity probe'
                ),
                opener=lambda *_a, **_k: pytest.fail(
                    'shared busy must precede release fetch'
                ),
                runner=lambda *_a, **_k: pytest.fail(
                    'shared busy must precede subprocess probes'
                ),
            )
    finally:
        release.set()
        owner.join(timeout=5)
    assert not owner.is_alive()


def test_active_transcode_busy_releases_shared_installer_guard(
        monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    monkeypatch.setattr(installer_lock, '_default_root', lambda: tmp_path)

    result = ManagedFFmpegUpdater().install(busy=lambda: True)

    assert result['status'] == 'busy'
    with installer_lock.installer_guard(tmp_path):
        pass


def test_handled_ffmpeg_failure_releases_shared_installer_guard(
        monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    root = tmp_path / 'local' / 'yt_final'
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    monkeypatch.setattr(installer_lock, '_default_root', lambda: root)
    monkeypatch.setattr(
        ffmpeg_updater,
        'fetch_release',
        lambda **_kwargs: (_ for _ in ()).throw(
            FFmpegUpdateError('offline', code='network')
        ),
    )

    result = ManagedFFmpegUpdater().install(busy=lambda: False)

    assert result['code'] == 'network'
    with installer_lock.installer_guard(root):
        pass


def test_direct_recovery_shared_guard_busy_precedes_marker_probe(
        monkeypatch, tmp_path):
    root = tmp_path / 'local' / 'yt_final'
    ffmpeg_root = root / 'ffmpeg'
    monkeypatch.setattr(installer_lock, '_default_root', lambda: root)
    ready = threading.Event()
    release = threading.Event()

    def own_guard():
        with installer_lock.installer_guard(root):
            ready.set()
            assert release.wait(5)

    owner = threading.Thread(target=own_guard)
    owner.start()
    assert ready.wait(5)
    try:
        with pytest.raises(InstallerBusyError):
            ffmpeg_updater.recover_interrupted_activation(root=ffmpeg_root)
    finally:
        release.set()
        owner.join(timeout=5)
    assert not owner.is_alive()
    assert not ffmpeg_root.exists()


def test_install_rejects_unsupported_platform_before_callbacks_or_filesystem_mutation(
        monkeypatch, tmp_path):
    local = tmp_path / 'local'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'linux', raising=False)

    def unexpected(*args, **kwargs):
        raise AssertionError('unsupported install must stop before callbacks')

    result = ManagedFFmpegUpdater().install(
        busy=unexpected,
        opener=unexpected,
        runner=unexpected,
    )

    assert result['ok'] is False
    assert result['status'] == 'unsupported'
    assert result['code'] == 'unsupported'
    assert 'windows' in result['message'].lower()
    assert not local.exists()


def test_install_keeps_windows_install_path_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)

    result = ManagedFFmpegUpdater().install(
        busy=lambda: True,
        opener=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('busy Windows install must not reach the network')),
    )

    assert result['status'] == 'busy'


def test_install_rejects_restricted_macos_artifact_before_any_io(
        monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'darwin', raising=False)
    monkeypatch.setattr(ffmpeg_updater, '_machine', lambda: 'arm64', raising=False)
    monkeypatch.setattr(ffmpeg_updater.Path, 'home', classmethod(lambda cls: tmp_path))

    reached = []

    def forbidden(*_args, **_kwargs):
        reached.append(True)
        raise AssertionError('restricted macOS artifact reached callback I/O')

    result = ManagedFFmpegUpdater().install(
        busy=forbidden,
        opener=forbidden,
        runner=forbidden,
    )

    assert result['status'] == 'unsupported'
    assert result['code'] == 'unsupported'
    assert reached == []
    assert not (tmp_path / 'Library' / 'Application Support' / 'yt_final').exists()


def test_install_stops_before_activation_when_ffmpeg_becomes_busy(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    busy_states = iter((False, True))

    result = ManagedFFmpegUpdater().install(
        busy=lambda: next(busy_states),
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=successful_runner,
    )

    assert result['ok'] is False
    assert result['status'] == 'busy'
    assert result['version'] == '8.1.2'
    assert managed_ffmpeg_path().read_bytes() == b'old ffmpeg'
    assert managed_ffmpeg_path().with_name('ffprobe.exe').read_bytes() == b'old ffprobe'
    assert not (managed_root() / 'metadata.json').exists()


def test_install_never_executes_unproven_existing_managed_binary(
        monkeypatch, tmp_path):
    """Hardware comparison cannot turn a forged old app-data tree into code."""
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    old_ffmpeg = managed_ffmpeg_path()
    old_ffmpeg.parent.mkdir(parents=True)
    old_ffmpeg.write_bytes(b'forged old ffmpeg')
    old_ffmpeg.with_name('ffprobe.exe').write_bytes(b'forged old ffprobe')
    executed_old = []

    def tracking_runner(command, **kwargs):
        if (
            Path(command[0]) == old_ffmpeg
            and old_ffmpeg.read_bytes().startswith(b'forged')
        ):
            executed_old.append(list(command))
        return successful_runner(command, **kwargs)

    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=tracking_runner,
    )

    assert result['ok'] is True
    assert executed_old == []


def test_certified_pair_rejects_reparse_app_root_before_execution(
        monkeypatch, tmp_path):
    """A safe-looking FFmpeg subtree cannot authorize code through a linked app root."""
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=successful_runner,
    )
    assert result['ok'] is True
    app_root = managed_root().parent
    real_lstat = Path.lstat

    class ReparseMetadata:
        def __init__(self, metadata):
            self._metadata = metadata
            self.st_mode = metadata.st_mode
            self.st_file_attributes = (
                getattr(metadata, 'st_file_attributes', 0) | 0x400
            )

        def __getattr__(self, name):
            return getattr(self._metadata, name)

    def reparse_app_root(path):
        metadata = real_lstat(path)
        if path == app_root:
            return ReparseMetadata(metadata)
        return metadata

    monkeypatch.setattr(Path, 'lstat', reparse_app_root)
    executed = []

    def forbidden_runner(command, **_kwargs):
        executed.append(command)
        raise AssertionError('reparse-backed managed FFmpeg was executed')

    with pytest.raises(FFmpegUpdateError):
        ffmpeg_updater.certified_managed_pair(runner=forbidden_runner)

    assert executed == []


def test_certified_pair_hashes_manifest_binaries_without_reopening_archive(
        monkeypatch, tmp_path):
    """Job-time handoff must not decompress the large retained archive."""
    root = tmp_path / 'local' / 'yt_final' / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    ffmpeg_bytes = b'approved-ffmpeg'
    ffprobe_bytes = b'approved-ffprobe'
    ffmpeg_path = current_bin / 'ffmpeg.exe'
    ffprobe_path = current_bin / 'ffprobe.exe'
    ffmpeg_path.write_bytes(ffmpeg_bytes)
    ffprobe_path.write_bytes(ffprobe_bytes)
    artifact = ffmpeg_updater.ToolArtifact(
        'ffmpeg',
        '8.1.2',
        'win32',
        'x86_64',
        RELEASE_ARCHIVE_URL,
        'a' * 64,
        'zip',
        hashlib.sha256(ffmpeg_bytes).hexdigest(),
        hashlib.sha256(ffprobe_bytes).hexdigest(),
    )
    release = ffmpeg_updater._release_from_artifacts(artifact)
    monkeypatch.setattr(
        ffmpeg_updater,
        'fetch_release',
        lambda **_kwargs: release,
    )
    monkeypatch.setattr(
        ffmpeg_updater,
        '_read_checked_regular_bytes',
        lambda *_args, **_kwargs: pytest.fail(
            'execution handoff reopened the retained archive'
        ),
    )

    version, selected_ffmpeg, selected_ffprobe = (
        ffmpeg_updater.certified_managed_pair(
            root=root,
            runner=successful_runner,
        )
    )

    assert version == '8.1.2'
    assert selected_ffmpeg == ffmpeg_path
    assert selected_ffprobe == ffprobe_path


def test_certified_pair_rejects_binary_not_matching_manifest_before_probe(
        monkeypatch, tmp_path):
    """A valid-looking app-data tree cannot override executable digests."""
    root = tmp_path / 'local' / 'yt_final' / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    (current_bin / 'ffmpeg.exe').write_bytes(b'preseeded-ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'preseeded-ffprobe')
    artifact = ffmpeg_updater.ToolArtifact(
        'ffmpeg',
        '8.1.2',
        'win32',
        'x86_64',
        RELEASE_ARCHIVE_URL,
        'a' * 64,
        'zip',
        hashlib.sha256(b'approved-ffmpeg').hexdigest(),
        hashlib.sha256(b'approved-ffprobe').hexdigest(),
    )
    monkeypatch.setattr(
        ffmpeg_updater,
        'fetch_release',
        lambda **_kwargs: ffmpeg_updater._release_from_artifacts(artifact),
    )
    executed = []

    with pytest.raises(FFmpegUpdateError) as caught:
        ffmpeg_updater.certified_managed_pair(
            root=root,
            runner=lambda *args, **kwargs: executed.append((args, kwargs)),
        )

    assert caught.value.code == 'checksum'
    assert executed == []


def test_capability_detector_prefers_valid_managed_pair(monkeypatch, tmp_path):
    managed = tmp_path / 'managed' / 'bin' / 'ffmpeg.exe'
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b'ffmpeg')
    managed.with_name('ffprobe.exe').write_bytes(b'ffprobe')

    monkeypatch.setattr(
        capability,
        'certified_managed_pair',
        lambda **_kwargs: ('8.1.2', managed, managed.with_name('ffprobe.exe')),
        raising=False,
    )
    monkeypatch.setattr(
        capability.subprocess,
        'run',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('system FFmpeg must not be probed when managed FFmpeg is valid')
        ),
    )
    monkeypatch.setattr(CapabilityDetector, '_detect_all', lambda self: None)

    detector = CapabilityDetector()

    assert detector.ffmpeg_path == str(managed)


def test_capability_detector_falls_back_when_managed_pair_is_invalid(monkeypatch, tmp_path):
    managed = tmp_path / 'managed' / 'bin' / 'ffmpeg.exe'
    system_ffmpeg = tmp_path / 'system' / 'ffmpeg.exe'
    system_ffmpeg.parent.mkdir()
    system_ffmpeg.write_bytes(b'system ffmpeg')
    # A real PATH executable carries the exec bit, and explicit_path_executable() correctly
    # requires os.access(..., X_OK) on POSIX. Without it the fallback finds nothing on macOS/Linux
    # and ffmpeg_path is None, so the assertion below blows up instead of testing the fallback.
    # Windows ignores the mode, so this is a no-op there.
    system_ffmpeg.chmod(0o755)
    monkeypatch.setenv('PATH', str(system_ffmpeg.parent))
    probed_roots = []

    def invalid_pair(**_kwargs):
        probed_roots.append(managed.parent.parent)
        raise FFmpegUpdateError('invalid managed pair')

    class Completed:
        returncode = 0
        stdout = 'ffmpeg version system\n'

    monkeypatch.setattr(
        capability,
        'certified_managed_pair',
        invalid_pair,
        raising=False,
    )
    monkeypatch.setattr(capability.subprocess, 'run', lambda *args, **kwargs: Completed())
    monkeypatch.setattr(CapabilityDetector, '_detect_all', lambda self: None)

    detector = CapabilityDetector()

    assert probed_roots == [managed.parent.parent]
    assert os.path.normcase(detector.ffmpeg_path) == os.path.normcase(
        str(system_ffmpeg)
    )


def test_default_capability_resolution_never_probes_unproven_managed_binaries(
        monkeypatch, tmp_path):
    """A forged app-data tree is data, not an executable trust anchor."""
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    managed = managed_ffmpeg_path()
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b'forged ffmpeg')
    managed.with_name('ffprobe.exe').write_bytes(b'forged ffprobe')
    managed_probe_roots = []

    def forged_pair_would_look_valid(root):
        managed_probe_roots.append(Path(root))
        return ('8.1.2', managed, managed.with_name('ffprobe.exe'))

    class Completed:
        returncode = 0
        stdout = 'ffmpeg version system\n'
        stderr = ''

    system_ffmpeg = tmp_path / 'system-ffmpeg.exe'
    system_ffmpeg.write_bytes(b'system ffmpeg')
    monkeypatch.setattr(capability, 'probe_pair', forged_pair_would_look_valid)
    resolution = capability.resolve_ffmpeg(
        runner=lambda *_args, **_kwargs: Completed(),
        which=lambda name: (
            str(system_ffmpeg) if name == 'ffmpeg.exe' else None
        ),
        cwd=tmp_path,
        platform='win32',
        application_dir=tmp_path / 'empty-app',
    )

    assert managed_probe_roots == []
    assert resolution.managed is False
    assert resolution.path == str(system_ffmpeg)


def test_capability_resolution_ignores_poisoned_cwd_and_has_no_bare_fallback(
        monkeypatch, tmp_path):
    """A missing trusted/PATH FFmpeg must remain non-executable."""
    poisoned = tmp_path / 'download-folder'
    poisoned.mkdir()
    (poisoned / 'ffmpeg.exe').write_bytes(b'poison')
    monkeypatch.chdir(poisoned)
    monkeypatch.setenv('PATH', '')
    monkeypatch.setattr(
        capability,
        'recover_interrupted_activation',
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        capability,
        'certified_managed_pair',
        lambda **_kwargs: (_ for _ in ()).throw(
            FFmpegUpdateError('no managed pair')
        ),
    )
    executed = []

    def forbidden_runner(command, **_kwargs):
        executed.append(list(command))
        return type('Completed', (), {
            'returncode': 0,
            'stdout': 'ffmpeg version poison\n',
            'stderr': '',
        })()

    resolution = capability.resolve_ffmpeg(
        runner=forbidden_runner,
        cwd=poisoned,
        platform='win32',
    )

    assert resolution.path is None
    assert resolution.version_line is None
    assert executed == []


def test_capability_resolution_accepts_only_the_trusted_application_binary(
        monkeypatch, tmp_path):
    poisoned = tmp_path / 'download-folder'
    poisoned.mkdir()
    (poisoned / 'ffmpeg.exe').write_bytes(b'poison')
    application_dir = tmp_path / 'installed-app'
    application_dir.mkdir()
    bundled = application_dir / 'ffmpeg.exe'
    bundled.write_bytes(b'bundled')
    monkeypatch.chdir(poisoned)
    monkeypatch.setenv('PATH', '')
    monkeypatch.setattr(
        capability,
        'recover_interrupted_activation',
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        capability,
        'certified_managed_pair',
        lambda **_kwargs: (_ for _ in ()).throw(
            FFmpegUpdateError('no managed pair')
        ),
    )
    executed = []

    def runner(command, **_kwargs):
        executed.append(list(command))
        return type('Completed', (), {
            'returncode': 0,
            'stdout': 'ffmpeg version 8.1.2\n',
            'stderr': '',
        })()

    resolution = capability.resolve_ffmpeg(
        runner=runner,
        cwd=poisoned,
        platform='win32',
        application_dir=application_dir,
    )

    assert resolution.path == str(bundled)
    assert executed == [[str(bundled), '-version']]


def test_nvidia_probe_ignores_poisoned_cwd_when_not_explicitly_on_path(
        monkeypatch, tmp_path):
    poisoned = tmp_path / 'download-folder'
    poisoned.mkdir()
    (poisoned / 'nvidia-smi.exe').write_bytes(b'poison')
    monkeypatch.chdir(poisoned)
    monkeypatch.setenv('PATH', '')
    detector = CapabilityDetector.__new__(CapabilityDetector)
    detector.gpu_info = {'gpu_name': 'Unknown'}
    executed = []

    def forbidden_runner(command, **_kwargs):
        executed.append(list(command))
        return type('Completed', (), {
            'returncode': 0,
            'stdout': 'attacker GPU, attacker driver\n',
            'stderr': '',
        })()

    monkeypatch.setattr(capability.subprocess, 'run', forbidden_runner)

    detector._get_nvidia_details()

    assert detector.gpu_info['gpu_name'] == 'Unknown'
    assert executed == []


def test_recovery_never_probes_unproven_forged_rollback(monkeypatch, tmp_path):
    """Recovery must fail closed before executing an app-data rollback tree."""
    root = tmp_path / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    (current_bin / 'ffmpeg.exe').write_bytes(b'uncommitted ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'uncommitted ffprobe')
    rollback = _make_rollback(root, '.rollback-forged')
    _write_interrupted_marker(root, rollback)
    executed = []

    def forbidden_runner(command, **_kwargs):
        executed.append(command)
        return successful_runner(command)

    with pytest.raises(FFmpegUpdateError) as raised:
        ffmpeg_updater.recover_interrupted_activation(
            root=root,
            runner=forbidden_runner,
        )

    assert raised.value.code == 'recovery'
    assert executed == []
    assert rollback.exists()


class FakeFFmpegProcess:
    def __init__(self, stderr=()):
        self.stderr = stderr
        self.returncode = None
        self.killed = False

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class RaisingStderr:
    def __init__(self, error):
        self.error = error

    def __iter__(self):
        return self

    def __next__(self):
        raise self.error


def make_processor():
    detector = type('Detector', (), {'ffmpeg_path': 'ffmpeg'})()
    return FFmpegProcessor(detector)


def test_ffmpeg_count_is_visible_only_while_fake_process_blocks(monkeypatch):
    launched = threading.Event()
    release = threading.Event()

    class BlockingStderr:
        def __init__(self):
            self.finished = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.finished:
                raise StopIteration
            self.finished = True
            release.wait(timeout=2)
            raise StopIteration

    def popen(*args, **kwargs):
        process = FakeFFmpegProcess(BlockingStderr())
        launched.set()
        return process

    monkeypatch.setattr(yt_final.subprocess, 'Popen', popen)
    processor = make_processor()
    results = []
    worker = threading.Thread(target=lambda: results.append(processor._run_ffmpeg(['ffmpeg'])))

    worker.start()
    assert launched.wait(timeout=1)
    assert FFmpegProcessor.active_process_count() == 1
    assert FFmpegProcessor.ffmpeg_busy() is True

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results == [True]
    assert FFmpegProcessor.active_process_count() == 0
    assert FFmpegProcessor.ffmpeg_busy() is False


def test_ffmpeg_count_decrements_after_success(monkeypatch):
    monkeypatch.setattr(
        yt_final.subprocess,
        'Popen',
        lambda *args, **kwargs: FakeFFmpegProcess(),
    )

    assert make_processor()._run_ffmpeg(['ffmpeg']) is True
    assert FFmpegProcessor.active_process_count() == 0


def test_ffmpeg_count_decrements_after_ordinary_exception(monkeypatch):
    process = FakeFFmpegProcess(RaisingStderr(RuntimeError('read failed')))
    monkeypatch.setattr(yt_final.subprocess, 'Popen', lambda *args, **kwargs: process)

    assert make_processor()._run_ffmpeg(['ffmpeg']) is False
    assert process.killed is True
    assert FFmpegProcessor.active_process_count() == 0


def test_ffmpeg_count_decrements_after_keyboard_interrupt(monkeypatch):
    process = FakeFFmpegProcess(RaisingStderr(KeyboardInterrupt()))
    monkeypatch.setattr(yt_final.subprocess, 'Popen', lambda *args, **kwargs: process)

    with pytest.raises(KeyboardInterrupt):
        make_processor()._run_ffmpeg(['ffmpeg'])
    assert process.killed is True
    assert FFmpegProcessor.active_process_count() == 0


def test_ffmpeg_count_decrements_after_timeout(monkeypatch):
    timeout = subprocess.TimeoutExpired(['ffmpeg'], timeout=1)
    process = FakeFFmpegProcess(RaisingStderr(timeout))
    monkeypatch.setattr(yt_final.subprocess, 'Popen', lambda *args, **kwargs: process)

    assert make_processor()._run_ffmpeg(['ffmpeg']) is False
    assert process.killed is True
    assert FFmpegProcessor.active_process_count() == 0


def test_ffmpeg_count_decrements_when_popen_itself_raises(monkeypatch):
    monkeypatch.setattr(
        yt_final.subprocess,
        'Popen',
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('launch failed')),
    )

    assert make_processor()._run_ffmpeg(['ffmpeg']) is False
    assert FFmpegProcessor.active_process_count() == 0


def test_ffmpeg_count_decrements_when_process_wait_times_out(monkeypatch):
    class WaitTimeoutProcess(FakeFFmpegProcess):
        def __init__(self):
            super().__init__(())
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(['ffmpeg'], timeout=1)
            return super().wait()

    process = WaitTimeoutProcess()
    monkeypatch.setattr(yt_final.subprocess, 'Popen', lambda *args, **kwargs: process)

    assert make_processor()._run_ffmpeg(['ffmpeg']) is False
    assert process.killed is True
    assert process.wait_calls == 2
    assert FFmpegProcessor.active_process_count() == 0


def test_loudnorm_measurement_uses_tracked_ffmpeg_lifecycle(monkeypatch, tmp_path):
    launched = threading.Event()
    release = threading.Event()

    # Pass-1 now runs via Popen + a stderr drain thread (so a cancel can interrupt the whole-audio
    # measurement). The activity-tracking lifecycle it protects is unchanged — _begin/_end still
    # bracket the measurement — so the test drives the new Popen path and asserts the same invariant.
    class _BlockingStderr:
        def __iter__(self):
            return self
        def __next__(self):
            launched.set()
            release.wait(timeout=2)
            raise StopIteration

    class _FakeProc:
        def __init__(self):
            self.returncode = None
            self.stderr = _BlockingStderr()
        def poll(self):
            return self.returncode
        def kill(self):
            self.returncode = -9
            release.set()
        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 1            # rc != 0 -> normalize_audio returns False
            return self.returncode

    monkeypatch.setattr(yt_final.subprocess, 'Popen', lambda *a, **k: _FakeProc())
    processor = make_processor()
    results = []
    worker = threading.Thread(
        target=lambda: results.append(processor.normalize_audio(
            tmp_path / 'source.mp4', tmp_path / 'output.mp4')))

    worker.start()
    assert launched.wait(timeout=1)
    assert FFmpegProcessor.active_process_count() == 1
    release.set()
    worker.join(timeout=2)

    assert results == [False]
    assert FFmpegProcessor.active_process_count() == 0


def test_final_busy_check_and_activation_swap_share_launch_gate(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    old_bin = managed_root() / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    state = {'held': False, 'entries': 0, 'busy_calls': 0}

    @contextmanager
    def observed_gate():
        assert state['held'] is False
        state['held'] = True
        state['entries'] += 1
        try:
            yield
        finally:
            state['held'] = False

    def busy():
        state['busy_calls'] += 1
        if state['busy_calls'] == 2:
            assert state['held'] is True
        return False

    real_replace = ffmpeg_updater.os.replace

    def observed_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == managed_root() / 'current' and '.ffmpeg-update-' in str(source_path):
            assert state['held'] is True
        return real_replace(source, destination)

    monkeypatch.setattr(ffmpeg_updater, 'ffmpeg_launch_gate', observed_gate, raising=False)
    monkeypatch.setattr(ffmpeg_updater.os, 'replace', observed_replace)

    result = ManagedFFmpegUpdater().install(
        busy=busy,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=successful_runner,
    )

    assert result['ok'] is True
    assert state['entries'] == 1


def _make_rollback(root: Path, name: str, *, valid: bool = True) -> Path:
    rollback = root / name
    (rollback / 'bin').mkdir(parents=True)
    (rollback / 'bin' / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    if valid:
        (rollback / 'bin' / 'ffprobe.exe').write_bytes(b'old ffprobe')
    return rollback


def _write_interrupted_marker(root: Path, rollback: Path) -> Path:
    marker = root / ffmpeg_updater.ACTIVATION_MARKER_NAME
    marker.write_text(
        json.dumps({'version': 1, 'rollback': rollback.name}),
        encoding='utf-8',
    )
    return marker


def _write_certified_metadata(
    root: Path,
    release: ffmpeg_updater.FFmpegRelease | None = None,
) -> Path:
    ffmpeg_path = root / 'current' / 'bin' / 'ffmpeg.exe'
    release = release or ffmpeg_updater.FFmpegRelease(
        version='8.1.2',
        archive_url=RELEASE_ARCHIVE_URL,
        sha256=(
            'db580001caa24ac104c8cb856cd113a87'
            'b0a443f7bdf47d8c12b1d740584a2ec'
        ),
    )
    metadata = root / 'metadata.json'
    metadata.write_text(
        json.dumps({
            'version': release.version,
            'path': str(ffmpeg_path),
            'sha256': release.sha256,
            'checksum_verified': True,
        }),
        encoding='utf-8',
    )
    return metadata


def _approve_release_tree(monkeypatch, release_root: Path):
    """Give a synthetic test tree the same immutable archive proof as production."""
    ffmpeg_bytes = (release_root / 'bin' / 'ffmpeg.exe').read_bytes()
    ffprobe_bytes = (release_root / 'bin' / 'ffprobe.exe').read_bytes()
    archive_payload = make_archive([
        (
            'ffmpeg-8.1.2-essentials_build/bin/ffmpeg.exe',
            ffmpeg_bytes,
        ),
        (
            'ffmpeg-8.1.2-essentials_build/bin/ffprobe.exe',
            ffprobe_bytes,
        ),
    ])
    (release_root / ffmpeg_updater.APPROVED_FFMPEG_ARCHIVE).write_bytes(
        archive_payload
    )
    release = ffmpeg_updater.FFmpegRelease(
        version='8.1.2',
        archive_url=RELEASE_ARCHIVE_URL,
        sha256=hashlib.sha256(archive_payload).hexdigest(),
    )
    monkeypatch.setattr(
        ffmpeg_updater,
        'fetch_release',
        lambda **_kwargs: release,
    )
    return release


def _seed_complete_persistent_layout(root: Path) -> dict[str, Path]:
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    current_ffmpeg = current_bin / 'ffmpeg.exe'
    current_ffprobe = current_bin / 'ffprobe.exe'
    current_ffmpeg.write_bytes(b'committed ffmpeg')
    current_ffprobe.write_bytes(b'committed ffprobe')
    rollback = _make_rollback(root, '.rollback-selected')
    marker = _write_interrupted_marker(root, rollback)
    metadata = _write_certified_metadata(root)
    return {
        'root': root,
        'current': root / 'current',
        'current-bin': current_bin,
        'current-ffmpeg': current_ffmpeg,
        'current-ffprobe': current_ffprobe,
        'rollback': rollback,
        'rollback-bin': rollback / 'bin',
        'rollback-ffmpeg': rollback / 'bin' / 'ffmpeg.exe',
        'rollback-ffprobe': rollback / 'bin' / 'ffprobe.exe',
        'marker': marker,
        'metadata': metadata,
    }


def _install_with_forbidden_persistent_side_effects(monkeypatch) -> tuple[dict, list[str]]:
    reached: list[str] = []

    def forbidden(label):
        def fail(*_args, **_kwargs):
            reached.append(label)
            raise AssertionError(f'{label} must not be reached')
        return fail

    make_release_opener(valid_archive(), monkeypatch=monkeypatch)
    monkeypatch.setattr(ffmpeg_updater, '_download_archive', forbidden('network'))
    monkeypatch.setattr(ffmpeg_updater, '_extract_archive', forbidden('extract'))
    monkeypatch.setattr(ffmpeg_updater, '_write_metadata', forbidden('metadata-write'))
    monkeypatch.setattr(
        ffmpeg_updater,
        '_write_activation_marker',
        forbidden('marker-write'),
    )
    monkeypatch.setattr(
        ffmpeg_updater,
        '_remove_activation_marker',
        forbidden('marker-delete'),
    )
    monkeypatch.setattr(ffmpeg_updater.tempfile, 'mkdtemp', forbidden('staging-write'))
    monkeypatch.setattr(ffmpeg_updater.os, 'replace', forbidden('rename'))
    monkeypatch.setattr(ffmpeg_updater.shutil, 'rmtree', forbidden('delete'))

    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=forbidden('network'),
        runner=forbidden('probe'),
    )
    return result, reached


@pytest.mark.parametrize('unsafe_component', [
    'root',
    'current',
    'current-bin',
    'current-ffmpeg',
    'current-ffprobe',
    'rollback',
    'rollback-bin',
    'rollback-ffmpeg',
    'rollback-ffprobe',
    'marker',
    'metadata',
])
def test_install_rejects_reparse_in_every_persistent_component_before_side_effects(
        monkeypatch, tmp_path, unsafe_component):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    root = managed_root()
    targets = _seed_complete_persistent_layout(root)
    unsafe_path = targets[unsafe_component]
    outside_sentinel = tmp_path / 'outside' / 'keep.txt'
    outside_sentinel.parent.mkdir()
    outside_sentinel.write_text('unchanged', encoding='utf-8')
    real_lstat = Path.lstat

    class ReparseMetadata:
        def __init__(self, metadata):
            self._metadata = metadata
            self.st_mode = metadata.st_mode
            self.st_file_attributes = (
                getattr(metadata, 'st_file_attributes', 0) | 0x400
            )

        def __getattr__(self, name):
            return getattr(self._metadata, name)

    def reparse_lstat(candidate):
        metadata = real_lstat(candidate)
        if candidate == unsafe_path:
            return ReparseMetadata(metadata)
        return metadata

    monkeypatch.setattr(Path, 'lstat', reparse_lstat)

    result, reached = _install_with_forbidden_persistent_side_effects(monkeypatch)

    assert result['ok'] is False
    assert result['code'] == 'recovery'
    assert reached == []
    assert outside_sentinel.read_text(encoding='utf-8') == 'unchanged'


@pytest.mark.parametrize('unsafe_kind', ['symlink', 'junction', 'outside'])
def test_install_rejects_linked_or_outside_persistent_chain_before_side_effects(
        monkeypatch, tmp_path, unsafe_kind):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    root = managed_root()
    targets = _seed_complete_persistent_layout(root)
    unsafe_path = targets['current-bin']
    outside_sentinel = tmp_path / 'outside' / 'keep.txt'
    outside_sentinel.parent.mkdir()
    outside_sentinel.write_text('unchanged', encoding='utf-8')

    if unsafe_kind == 'symlink':
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            'is_symlink',
            lambda candidate: (
                candidate == unsafe_path or real_is_symlink(candidate)
            ),
        )
    elif unsafe_kind == 'junction':
        real_is_junction = getattr(Path, 'is_junction', lambda _path: False)
        monkeypatch.setattr(
            Path,
            'is_junction',
            lambda candidate: (
                candidate == unsafe_path or real_is_junction(candidate)
            ),
            raising=False,
        )
    else:
        real_resolve = Path.resolve
        outside_path = outside_sentinel.parent.resolve()

        def outside_resolve(candidate, *args, **kwargs):
            if candidate == unsafe_path:
                return outside_path
            return real_resolve(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, 'resolve', outside_resolve)

    result, reached = _install_with_forbidden_persistent_side_effects(monkeypatch)

    assert result['ok'] is False
    assert result['code'] == 'recovery'
    assert reached == []
    assert outside_sentinel.read_text(encoding='utf-8') == 'unchanged'


def test_recovery_restores_unambiguous_marker_selected_rollback(
        monkeypatch, tmp_path):
    root = tmp_path / 'ffmpeg'
    rollback = _make_rollback(root, '.rollback-a')
    _approve_release_tree(monkeypatch, rollback)
    marker = _write_interrupted_marker(root, rollback)

    assert ffmpeg_updater.recover_interrupted_activation(
        root=root, runner=successful_runner) is True

    assert (root / 'current' / 'bin' / 'ffmpeg.exe').read_bytes() == b'old ffmpeg'
    assert not rollback.exists()
    assert not marker.exists()


def test_recovery_restores_selected_rollback_over_uncommitted_current(
        monkeypatch, tmp_path):
    root = tmp_path / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    (current_bin / 'ffmpeg.exe').write_bytes(b'uncommitted ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'uncommitted ffprobe')
    rollback = _make_rollback(root, '.rollback-a')
    _approve_release_tree(monkeypatch, rollback)
    marker = _write_interrupted_marker(root, rollback)

    assert ffmpeg_updater.recover_interrupted_activation(
        root=root, runner=successful_runner) is True

    assert (root / 'current' / 'bin' / 'ffmpeg.exe').read_bytes() == b'old ffmpeg'
    assert not rollback.exists()
    assert not marker.exists()


def test_recovery_clears_pre_swap_marker_when_no_rollback_exists(tmp_path):
    root = tmp_path / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    (current_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    marker = _write_interrupted_marker(root, root / '.rollback-not-created')

    assert ffmpeg_updater.recover_interrupted_activation(
        root=root, runner=successful_runner) is False

    assert (root / 'current' / 'bin' / 'ffmpeg.exe').read_bytes() == b'old ffmpeg'
    assert not marker.exists()


def test_recovery_keeps_current_when_certified_metadata_proves_commit(
        monkeypatch, tmp_path):
    root = tmp_path / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    current_ffmpeg = current_bin / 'ffmpeg.exe'
    current_ffmpeg.write_bytes(b'committed ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'committed ffprobe')
    rollback = _make_rollback(root, '.rollback-a')
    marker = _write_interrupted_marker(root, rollback)
    release = _approve_release_tree(monkeypatch, root / 'current')
    _write_certified_metadata(root, release)

    assert ffmpeg_updater.recover_interrupted_activation(
        root=root, runner=successful_runner) is False

    assert current_ffmpeg.read_bytes() == b'committed ffmpeg'
    assert not rollback.exists()
    assert not marker.exists()


def test_metadata_certified_recovery_cleans_partial_rollback_before_marker(
        monkeypatch, tmp_path):
    root = tmp_path / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    current_ffmpeg = current_bin / 'ffmpeg.exe'
    current_ffmpeg.write_bytes(b'committed ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'committed ffprobe')
    rollback = _make_rollback(root, '.rollback-partial', valid=False)
    marker = _write_interrupted_marker(root, rollback)
    release = _approve_release_tree(monkeypatch, root / 'current')
    _write_certified_metadata(root, release)

    assert ffmpeg_updater.recover_interrupted_activation(
        root=root, runner=successful_runner) is False

    assert current_ffmpeg.read_bytes() == b'committed ffmpeg'
    assert not rollback.exists()
    assert not marker.exists()


def test_metadata_cleanup_failure_blocks_install_before_new_download(
        monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    root = managed_root()
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    current_ffmpeg = current_bin / 'ffmpeg.exe'
    current_ffmpeg.write_bytes(b'committed ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'committed ffprobe')
    rollback = _make_rollback(root, '.rollback-selected')
    marker = _write_interrupted_marker(root, rollback)
    release = _approve_release_tree(monkeypatch, root / 'current')
    _write_certified_metadata(root, release)
    cleanup_attempts = []
    network_attempts = []
    real_rmtree = ffmpeg_updater.shutil.rmtree

    def fail_selected_cleanup(path, *args, **kwargs):
        if Path(path) == rollback:
            cleanup_attempts.append(Path(path))
            raise OSError('simulated committed cleanup failure')
        return real_rmtree(path, *args, **kwargs)

    def forbidden_opener(*_args, **_kwargs):
        network_attempts.append(True)
        raise AssertionError('new download must not start')

    monkeypatch.setattr(ffmpeg_updater.shutil, 'rmtree', fail_selected_cleanup)

    result = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=forbidden_opener,
        runner=successful_runner,
    )

    assert result['ok'] is False
    assert result['code'] == 'recovery'
    assert cleanup_attempts == [rollback]
    assert network_attempts == []
    assert marker.exists()
    assert rollback.exists()
    assert current_ffmpeg.read_bytes() == b'committed ffmpeg'


def test_committed_cleanup_retry_leaves_next_activation_unambiguous(
        monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    root = managed_root()
    old_bin = root / 'current' / 'bin'
    old_bin.mkdir(parents=True)
    (old_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (old_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    real_rmtree = ffmpeg_updater.shutil.rmtree
    failed_once = False

    def fail_first_rollback_cleanup(path, *args, **kwargs):
        nonlocal failed_once
        if Path(path).name.startswith('.rollback-') and not failed_once:
            failed_once = True
            raise OSError('simulated first committed cleanup failure')
        return real_rmtree(path, *args, **kwargs)

    opener = make_release_opener(valid_archive(), monkeypatch=monkeypatch)
    monkeypatch.setattr(
        ffmpeg_updater.shutil,
        'rmtree',
        fail_first_rollback_cleanup,
    )

    first = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=opener,
        runner=successful_runner,
    )

    marker = root / ffmpeg_updater.ACTIVATION_MARKER_NAME
    rollback = next(root.glob('.rollback-*'))
    assert first['ok'] is True
    assert marker.exists() and rollback.exists()

    assert ffmpeg_updater.recover_interrupted_activation(
        root=root, runner=successful_runner) is False
    assert not marker.exists()
    assert list(root.glob('.rollback-*')) == []

    second = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=opener,
        runner=successful_runner,
    )

    assert second['ok'] is True
    assert managed_ffmpeg_path().read_bytes() == b'new ffmpeg'
    assert not marker.exists()
    assert list(root.glob('.rollback-*')) == []


def test_recovery_with_current_rejects_invalid_marker_before_clearing(tmp_path):
    root = tmp_path / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    (current_bin / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'old ffprobe')
    marker = root / ffmpeg_updater.ACTIVATION_MARKER_NAME
    marker.write_text(
        json.dumps({'version': 999, 'rollback': '.rollback-not-created'}),
        encoding='utf-8',
    )

    with pytest.raises(FFmpegUpdateError) as exc_info:
        ffmpeg_updater.recover_interrupted_activation(
            root=root, runner=successful_runner)

    assert exc_info.value.code == 'recovery'
    assert marker.exists()
    assert (root / 'current' / 'bin' / 'ffmpeg.exe').read_bytes() == b'old ffmpeg'


def test_recovery_with_current_fails_closed_on_ambiguous_rollbacks(tmp_path):
    root = tmp_path / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    (current_bin / 'ffmpeg.exe').write_bytes(b'uncommitted ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'uncommitted ffprobe')
    selected = _make_rollback(root, '.rollback-a')
    other = _make_rollback(root, '.rollback-b')
    marker = _write_interrupted_marker(root, selected)

    with pytest.raises(FFmpegUpdateError) as exc_info:
        ffmpeg_updater.recover_interrupted_activation(
            root=root, runner=successful_runner)

    assert exc_info.value.code == 'recovery'
    assert selected.exists() and other.exists() and marker.exists()
    assert (root / 'current' / 'bin' / 'ffmpeg.exe').read_bytes() == (
        b'uncommitted ffmpeg')


def test_recovery_with_current_fails_closed_on_corrupt_selected_rollback(tmp_path):
    root = tmp_path / 'ffmpeg'
    current_bin = root / 'current' / 'bin'
    current_bin.mkdir(parents=True)
    (current_bin / 'ffmpeg.exe').write_bytes(b'uncommitted ffmpeg')
    (current_bin / 'ffprobe.exe').write_bytes(b'uncommitted ffprobe')
    rollback = _make_rollback(root, '.rollback-a', valid=False)
    marker = _write_interrupted_marker(root, rollback)

    with pytest.raises(FFmpegUpdateError) as exc_info:
        ffmpeg_updater.recover_interrupted_activation(
            root=root, runner=successful_runner)

    assert exc_info.value.code == 'recovery'
    assert rollback.exists() and marker.exists()
    assert (root / 'current' / 'bin' / 'ffmpeg.exe').read_bytes() == (
        b'uncommitted ffmpeg')


def test_recovery_never_guesses_between_ambiguous_valid_rollbacks(tmp_path):
    root = tmp_path / 'ffmpeg'
    selected = _make_rollback(root, '.rollback-a')
    other = _make_rollback(root, '.rollback-b')
    marker = _write_interrupted_marker(root, selected)

    with pytest.raises(FFmpegUpdateError) as exc_info:
        ffmpeg_updater.recover_interrupted_activation(root=root, runner=successful_runner)

    assert exc_info.value.code == 'recovery'
    assert selected.exists() and other.exists() and marker.exists()
    assert not (root / 'current').exists()


def test_recovery_rejects_corrupt_marker_selected_rollback(tmp_path):
    root = tmp_path / 'ffmpeg'
    rollback = _make_rollback(root, '.rollback-a', valid=False)
    marker = _write_interrupted_marker(root, rollback)

    with pytest.raises(FFmpegUpdateError) as exc_info:
        ffmpeg_updater.recover_interrupted_activation(root=root, runner=successful_runner)

    assert exc_info.value.code == 'recovery'
    assert rollback.exists() and marker.exists()
    assert not (root / 'current').exists()


def test_install_returns_allowlisted_network_failure_and_sanitized_log(
        monkeypatch, tmp_path, caplog):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    secret = 'https://secret.invalid/file?token=abc C:\\Temp\\.ffmpeg-update-secret'

    def failing_open(*args, **kwargs):
        raise OSError(secret)

    with caplog.at_level('WARNING', logger='ffmpeg_updater'):
        result = ManagedFFmpegUpdater().install(
            busy=lambda: False, opener=failing_open, runner=successful_runner)

    assert result['code'] == 'network'
    assert result['message'] == ffmpeg_updater.PUBLIC_FAILURE_MESSAGES['network']
    assert secret not in caplog.text
    assert 'secret.invalid' not in caplog.text
    assert len(caplog.text) < 1000


def test_install_classifies_disk_and_probe_failures(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(
        ffmpeg_updater.tempfile,
        'mkdtemp',
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')),
    )
    disk = ManagedFFmpegUpdater().install(busy=lambda: False)
    assert disk['code'] == 'disk'

    monkeypatch.undo()
    # Re-apply Windows isolation after undo (host may be darwin). monkeypatch.undo() also drops
    # the autouse fixture's patches, so the architecture has to be re-pinned as well: the managed
    # contract is win32/x86_64 and an arm64 host would otherwise get 'unsupported' here.
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local-2'))
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    monkeypatch.setattr(ffmpeg_updater, '_machine', lambda: 'AMD64', raising=False)
    probe = ManagedFFmpegUpdater().install(
        busy=lambda: False,
        opener=make_release_opener(valid_archive(), monkeypatch=monkeypatch),
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, args[0])),
    )
    assert probe['code'] == 'probe'


def test_fetch_darwin_release_fails_closed_before_callbacks():
    class ForbiddenIO(BaseException):
        pass

    with pytest.raises(FFmpegUpdateError) as raised:
        ffmpeg_updater.fetch_darwin_release(
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ForbiddenIO('restricted macOS provider reached network I/O')
            ),
            machine='arm64',
        )

    assert raised.value.code == 'unsupported'
    assert 'homebrew' in str(raised.value).lower()


def test_macos_launcher_script_invokes_serve():
    launcher = Path(__file__).resolve().parent / 'Start yt_final.command'
    assert launcher.is_file()
    text = launcher.read_text(encoding='utf-8')
    assert 'yt_final.py serve' in text
    assert 'python3' in text
    # Exec bit only exists on POSIX. CPython synthesizes st_mode execute bits on Windows solely for
    # .bat/.cmd/.exe/.com, so a .command file always reports 0o100666 and this would fail there
    # unconditionally — a red suite on Windows for a macOS-only property.
    if os.name != 'nt':
        assert launcher.stat().st_mode & 0o111  # executable bit set


def test_public_failure_codes_are_fixed_and_actionable():
    assert set(ffmpeg_updater.PUBLIC_FAILURE_MESSAGES) == {
        'network', 'checksum', 'archive', 'disk', 'probe', 'compatibility', 'recovery',
    }
    assert all(message and '<' not in message for message in
               ffmpeg_updater.PUBLIC_FAILURE_MESSAGES.values())


def test_recover_pre_commit_swaps_under_the_launch_gate_and_respects_busy(monkeypatch, tmp_path):
    """CONFIRMED: the post-activation failure recovery rmtree'd/replaced `current` with NO
    ffmpeg_launch_gate and NO busy() re-check — unlike the forward swap (which holds the gate and
    re-checks busy) and recover_interrupted_activation (which holds the gate). A job that launched
    ffmpeg from `current` right after activation could see the tree half-deleted mid-recovery
    (rmtree stops at the in-use exe on Windows), with the intact rollback never restored."""
    root = tmp_path / 'root'
    current = root / 'current'
    (current / 'bin').mkdir(parents=True)
    (current / 'bin' / 'ffmpeg.exe').write_bytes(b'new broken build')
    rollback = tmp_path / 'root' / '.rollback-precommit'
    (rollback / 'bin').mkdir(parents=True)
    (rollback / 'bin' / 'ffmpeg.exe').write_bytes(b'old good build')
    state = {'held': False, 'entries': 0}

    @contextmanager
    def observed_gate():
        assert state['held'] is False
        state['held'] = True
        state['entries'] += 1
        try:
            yield
        finally:
            state['held'] = False

    real_rmtree = ffmpeg_updater.shutil.rmtree
    real_replace = ffmpeg_updater.os.replace

    def observed_rmtree(path, *a, **k):
        if Path(path) == current:
            assert state['held'] is True, 'current must only be removed under the launch gate'
        return real_rmtree(path, *a, **k)

    def observed_replace(src, dst):
        if Path(dst) == current:
            assert state['held'] is True, 'current must only be replaced under the launch gate'
        return real_replace(src, dst)

    monkeypatch.setattr(ffmpeg_updater, 'ffmpeg_launch_gate', observed_gate, raising=False)
    monkeypatch.setattr(ffmpeg_updater.shutil, 'rmtree', observed_rmtree)
    monkeypatch.setattr(ffmpeg_updater.os, 'replace', observed_replace)

    ffmpeg_updater._recover_pre_commit(root, rollback, activated=True)
    assert state['entries'] >= 1, '_recover_pre_commit never took the launch gate'
    assert current.exists() and not rollback.exists()
    assert (current / 'bin' / 'ffmpeg.exe').read_bytes() == b'old good build'

    # ...and a busy ffmpeg aborts recovery BEFORE any tree surgery: no half-deleted current.
    rollback2 = root / '.rollback-precommit2'
    (rollback2 / 'bin').mkdir(parents=True)
    (rollback2 / 'bin' / 'ffmpeg.exe').write_bytes(b'older build')
    with pytest.raises(ffmpeg_updater.FFmpegUpdateError):
        ffmpeg_updater._recover_pre_commit(root, rollback2, activated=True,
                                           busy=lambda: True)
    assert (current / 'bin' / 'ffmpeg.exe').read_bytes() == b'old good build', \
        'busy recovery must leave current untouched'
    assert rollback2.exists(), 'busy recovery must not consume the rollback'
