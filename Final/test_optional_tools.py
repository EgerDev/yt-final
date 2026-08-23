"""Certified Node/Deno installers and manual-only optional-tool contracts."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import threading
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

import pytest

import installer_lock
import optional_tools as ot
from installer_lock import (
    INSTALLER_BUSY_CODE,
    INSTALLER_BUSY_MESSAGE,
    InstallerBusyError,
)

MANUAL_ONLY_RESULTS = {
    'aria2c': {
        'ok': False,
        'tool': 'aria2c',
        'status': 'manual',
        'code': 'unsupported',
        'message': (
            'Automatic aria2c installation is unavailable because no certified '
            'binary digest is published; install aria2c manually.'
        ),
    },
    'N_m3u8DL-RE': {
        'ok': False,
        'tool': 'N_m3u8DL-RE',
        'status': 'manual',
        'code': 'unsupported',
        'message': (
            'Automatic N_m3u8DL-RE installation is unavailable because its '
            'download redirects cannot be pinned; install it manually.'
        ),
    },
    'playwright': {
        'ok': False,
        'tool': 'playwright',
        'status': 'manual',
        'code': 'unsupported',
        'message': (
            'Automatic Playwright browser installation is unavailable without '
            'a packaged immutable browser manifest; install it manually.'
        ),
    },
    'bgutil-server': {
        'ok': False,
        'tool': 'bgutil-server',
        'status': 'manual',
        'code': 'unsupported',
        'message': (
            'Automatic bgutil server installation is unavailable without a '
            'certified immutable server artifact; install it manually.'
        ),
    },
}

PYTHON_LOCK_ONLY_RESULT = {
    'ok': False,
    'tool': 'bgutil-plugin',
    'status': 'reviewed-release',
    'code': 'certified-runtime-unavailable',
    'message': (
        'Install a future reviewed yt_final release to change the bgutil '
        'Python provider; in-app Python mutation is disabled in 1.0.0.'
    ),
}


class ForbiddenInstallerIO(BaseException):
    pass


@pytest.fixture(autouse=True)
def _isolate_optional_tool_app_data(monkeypatch, tmp_path):
    """No optional-runtime test may read or mutate the user's live app-data."""
    monkeypatch.setattr(ot, 'app_data_dir', lambda: tmp_path / 'app-data')


def _forbid_installer_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO('manual/unsupported path performed installer I/O')

    monkeypatch.setattr(ot, 'installer_guard', forbidden)
    monkeypatch.setattr(ot.shutil, 'which', forbidden)
    monkeypatch.setattr(ot.urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(ot, 'managed_tools_dir', forbidden)
    monkeypatch.setattr(ot, 'managed_aria2_path', forbidden)
    monkeypatch.setattr(ot, 'managed_turbo_path', forbidden)
    monkeypatch.setattr(ot, 'managed_bgutil_repo_dir', forbidden)
    monkeypatch.setattr(ot, 'find_bgutil_script_path', forbidden)
    return forbidden


@pytest.mark.parametrize(
    ('installer_name', 'tool'),
    [
        ('install_aria2c', 'aria2c'),
        ('install_n_m3u8dl', 'N_m3u8DL-RE'),
        ('install_playwright', 'playwright'),
        ('install_bgutil_script', 'bgutil-server'),
        ('install_bgutil_server', 'bgutil-server'),
    ],
)
def test_manual_only_installer_returns_stable_guidance_before_any_io(
        monkeypatch, installer_name, tool):
    """Removing the early return would expose an uncertified mutation path."""
    forbidden = _forbid_installer_io(monkeypatch)

    result = getattr(ot, installer_name)(runner=forbidden)

    assert result == MANUAL_ONLY_RESULTS[tool]


@pytest.mark.parametrize(
    ('requested', 'canonical'),
    [
        ('aria2', 'aria2c'),
        ('turbo', 'N_m3u8DL-RE'),
        ('playwright', 'playwright'),
        ('bgutil-server', 'bgutil-server'),
    ],
)
def test_manual_only_dispatch_returns_guidance_before_guard_or_installer(
        monkeypatch, requested, canonical):
    """The API dispatcher must not acquire a lock or enter legacy installers for guidance."""
    _forbid_installer_io(monkeypatch)

    result = ot.install_optional_tool(requested)

    assert result == MANUAL_ONLY_RESULTS[canonical]


def test_bgutil_plugin_direct_install_requires_whole_python_lock_before_any_io(
        monkeypatch):
    """The provider must never escape the certified lock through a package-only helper."""
    forbidden = _forbid_installer_io(monkeypatch)

    result = ot.install_bgutil_plugin(runner=forbidden)

    assert result == PYTHON_LOCK_ONLY_RESULT


@pytest.mark.parametrize(
    'requested',
    ['bgutil', 'bgutil-plugin', 'bgutil-ytdlp-pot-provider'],
)
def test_bgutil_plugin_aliases_require_whole_python_lock_before_any_io(
        monkeypatch, requested):
    """Every legacy alias must close the same per-package mutation path."""
    _forbid_installer_io(monkeypatch)

    result = ot.install_optional_tool(requested)

    assert result == PYTHON_LOCK_ONLY_RESULT


def test_only_certified_manifest_tools_are_advertised_as_automatically_installable():
    """The UI must not render an automatic action for a manual-only helper."""
    assert ot.installable_tools() == frozenset({'node', 'deno'})


@pytest.mark.parametrize('installer_name', ['install_node', 'install_deno'])
def test_uncertified_optional_host_returns_manual_guidance_before_any_io(
        monkeypatch, installer_name):
    """A host with no manifest match must stop before lock/path/network/process activity."""
    forbidden = _forbid_installer_io(monkeypatch)
    monkeypatch.setattr(ot.sys, 'platform', 'linux')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'x86_64')

    result = getattr(ot, installer_name)(runner=forbidden, opener=forbidden)

    assert result['ok'] is False
    assert result['status'] == 'manual'
    assert result['code'] == 'unsupported'
    assert 'manually' in result['message'].lower()


class ArtifactResponse(io.BytesIO):
    def __init__(self, payload: bytes, final_url: str):
        super().__init__(payload)
        self._final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def geturl(self):
        return self._final_url


def test_download_total_deadline_stops_a_slow_drip_response():
    """A peer yielding forever must not retain the global installer guard forever."""
    import tool_manifest

    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        '0' * 64,
        'zip',
        binary_sha256='1' * 64,
    )
    elapsed = 0.0
    reads = []

    def clock():
        return elapsed

    class SlowDripResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return artifact.url

        def read(self, size):
            nonlocal elapsed
            reads.append(size)
            elapsed += 31.0
            return b'x'

    with pytest.raises(ot.OptionalToolError) as caught:
        ot._download_verified_artifact(
            artifact,
            opener=lambda *_args: SlowDripResponse(),
            clock=clock,
            total_timeout=60.0,
        )

    assert caught.value.code == 'network'
    assert str(caught.value) == 'Certified managed artifact download failed'
    assert 1 <= len(reads) <= 2


def test_download_rejects_unapproved_final_url_before_read():
    """Moving the body read ahead of final-URL validation would consume substituted bytes."""
    import tool_manifest

    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        '0' * 64,
        'zip',
        binary_sha256='1' * 64,
    )

    class UnapprovedResponse(ArtifactResponse):
        def read(self, *_args, **_kwargs):
            raise ForbiddenInstallerIO('unapproved response body was read')

    with pytest.raises(tool_manifest.UnsupportedManagedInstall):
        ot._download_verified_artifact(
            artifact,
            opener=lambda *_args: UnapprovedResponse(
                b'substituted',
                'https://downloads.example.test/mirror/node-1.2.3.zip',
            ),
        )


@pytest.mark.parametrize(
    ('declared_kind', 'actual_kind'),
    [
        ('zip', 'tar.gz'),
        ('tar.gz', 'zip'),
    ],
)
def test_certified_runtime_rejects_archive_kind_mismatch_before_probe(
        monkeypatch, tmp_path, declared_kind, actual_kind):
    """Falling through to a second parser would ignore the signed manifest's archive contract."""
    import tool_manifest

    payload = _archive_payload(actual_kind, 'node.exe', b'candidate-node')
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/node-1.2.3.{declared_kind}',
        hashlib.sha256(payload).hexdigest(),
        declared_kind,
        binary_sha256=hashlib.sha256(b'candidate-node').hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)

    def probe_forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO('archive-kind mismatch reached a probe')

    with pytest.raises(ot.OptionalToolError) as caught:
        ot.install_node(
            runner=probe_forbidden,
            opener=lambda *_args: ArtifactResponse(payload, artifact.url),
        )

    assert caught.value.code == 'archive'
    assert not (tools / 'node' / 'current').exists()
    versions = tools / 'node' / 'versions'
    assert not versions.exists() or not tuple(versions.iterdir())


def _archive_payload(kind: str, member: str, content: bytes) -> bytes:
    stream = io.BytesIO()
    if kind == 'zip':
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(member, content)
    else:
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            info = tarfile.TarInfo(member)
            info.size = len(content)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def _runtime_relative_path(tool: str, platform_name: str) -> Path:
    if tool == 'node':
        return Path('node.exe') if platform_name.startswith('win') else Path('bin/node')
    return Path('bin/deno.exe') if platform_name.startswith('win') else Path('bin/deno')


def _write_active_runtime(
    tools: Path,
    tool: str,
    platform_name: str,
    *,
    release: str = '0.9.0-' + ('a' * 64),
    content: bytes = b'old-certified-binary',
) -> tuple[Path, Path]:
    root = tools / tool
    binary = root / 'versions' / release / _runtime_relative_path(
        tool,
        platform_name,
    )
    binary.parent.mkdir(parents=True)
    binary.write_bytes(content)
    pointer = root / 'current'
    pointer.write_text(release + '\n', encoding='ascii')
    return binary, pointer


def _mark_path_as_symlink(monkeypatch, unsafe_path: Path) -> None:
    real_is_symlink = Path.is_symlink

    def marked(candidate):
        if candidate == unsafe_path:
            return True
        return real_is_symlink(candidate)

    monkeypatch.setattr(Path, 'is_symlink', marked)


@pytest.mark.parametrize(
    (
        'tool', 'platform_name', 'machine', 'kind', 'member',
        'relative_binary', 'version_output',
    ),
    [
        (
            'node', 'win32', 'AMD64', 'zip',
            'node-v1.2.3-win-x64/node.exe',
            Path('node.exe'), 'v1.2.3\n',
        ),
        (
            'node', 'darwin', 'arm64', 'tar.gz',
            'node-v1.2.3-darwin-arm64/bin/node',
            Path('bin/node'), 'v1.2.3\n',
        ),
        (
            'deno', 'win32', 'x64', 'zip',
            'deno.exe',
            Path('bin/deno.exe'), 'deno 1.2.3\nv8 test\ntypescript test\n',
        ),
        (
            'deno', 'darwin', 'aarch64', 'zip',
            'deno',
            Path('bin/deno'), 'deno 1.2.3\nv8 test\ntypescript test\n',
        ),
    ],
)
def test_certified_node_and_deno_verify_then_probe_and_atomically_activate(
        monkeypatch, tmp_path, tool, platform_name, machine, kind, member,
        relative_binary, version_output):
    """Wrong verification/probe/activation ordering would let untrusted bytes execute."""
    import tool_manifest

    binary = f'{tool}-verified-binary'.encode('ascii')
    archive = _archive_payload(kind, member, binary)
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        platform_name,
        'arm64' if machine.lower() in {'arm64', 'aarch64'} else 'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.{kind}',
        digest,
        kind,
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', platform_name)
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: machine)
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tmp_path / 'tools')
    release = f'1.2.3-{digest}'
    destination = (
        tmp_path / 'tools' / tool / 'versions' / release / relative_binary
    )
    pointer = tmp_path / 'tools' / tool / 'current'

    opened = []

    def opener(url, timeout):
        opened.append((url, timeout))
        return ArtifactResponse(archive, artifact.url)

    probes = []

    def runner(command, **kwargs):
        executable = Path(command[0])
        assert executable.read_bytes() == binary
        assert executable != destination
        assert not destination.exists()
        probes.append(tuple(str(part) for part in command))
        return mock.Mock(returncode=0, stdout=version_output, stderr='')

    result = getattr(ot, f'install_{tool}')(runner=runner, opener=opener)

    assert result['ok'] is True
    assert result['method'] == 'verified_manifest'
    assert result['version'] == '1.2.3'
    assert result['checksum_verified'] is True
    assert result['path'] == str(destination)
    assert destination.read_bytes() == binary
    assert pointer.read_text(encoding='ascii') == release + '\n'
    assert ot.managed_runtime_path(tool) == destination
    assert ot.discover_managed_runtime(tool) == {
        'path': str(destination),
        'verified': True,
        'source': 'certified-pointer',
    }
    assert opened == [(artifact.url, ot.DOWNLOAD_TIMEOUT)]
    assert len(probes) == 1
    assert probes[0][1:] == ('--version',)
    assert not tuple((tmp_path / 'tools' / tool).glob('.current-*'))
    assert not tuple(
        (tmp_path / 'tools' / tool / 'versions').glob(f'.{tool}-verified-*')
    )


@pytest.mark.parametrize(
    ('tool', 'member'),
    [
        ('node', 'node-v1.2.3-win-x64/node.exe'),
        ('deno', 'deno.exe'),
    ],
)
def test_optional_checksum_mismatch_never_probes_and_preserves_old_binary(
        monkeypatch, tmp_path, tool, member):
    """Digest failure must happen before probe or replacement of a working install."""
    import tool_manifest

    approved_archive = _archive_payload('zip', member, b'approved')
    altered_archive = _archive_payload('zip', member, b'altered')
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        hashlib.sha256(approved_archive).hexdigest(),
        'zip',
        binary_sha256=hashlib.sha256(b'approved').hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tmp_path / 'tools')
    destination, pointer = _write_active_runtime(
        tmp_path / 'tools',
        tool,
        'win32',
    )
    old_pointer = pointer.read_bytes()

    def opener(url, timeout):
        return ArtifactResponse(altered_archive, artifact.url)

    def probe_forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO('checksum mismatch reached a binary probe')

    with pytest.raises(tool_manifest.ChecksumError):
        getattr(ot, f'install_{tool}')(
            runner=probe_forbidden,
            opener=opener,
        )

    assert destination.read_bytes() == b'old-certified-binary'
    assert pointer.read_bytes() == old_pointer
    assert ot.managed_runtime_path(tool) is None


@pytest.mark.parametrize('tool', ['node', 'deno'])
def test_optional_artifact_redirect_is_rejected_before_probe_or_activation(
        monkeypatch, tmp_path, tool):
    """Even a same-origin redirect must not select mutable or substituted bytes."""
    import tool_manifest

    member = 'node.exe' if tool == 'node' else 'deno.exe'
    archive = _archive_payload('zip', member, b'candidate')
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        hashlib.sha256(archive).hexdigest(),
        'zip',
        binary_sha256=hashlib.sha256(b'candidate').hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tmp_path / 'tools')

    def opener(url, timeout):
        return ArtifactResponse(
            archive,
            f'https://downloads.example.test/mirror/{tool}-1.2.3.zip',
        )

    def probe_forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO('redirected artifact reached a binary probe')

    with pytest.raises(tool_manifest.UnsupportedManagedInstall):
        getattr(ot, f'install_{tool}')(
            runner=probe_forbidden,
            opener=opener,
        )

    assert not (tmp_path / 'tools').exists()


@pytest.mark.parametrize('installer_name', ['install_node', 'install_deno'])
def test_certified_runtime_guard_busy_precedes_download(
        monkeypatch, tmp_path, installer_name):
    """Task 6 serialization must remain the first I/O boundary after pure lookup."""
    monkeypatch.setattr(installer_lock, '_default_root', lambda: tmp_path)
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')

    def download_forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO('busy certified installer started a download')

    monkeypatch.setattr(ot, '_download_verified_artifact', download_forbidden)
    ready = threading.Event()
    release = threading.Event()

    def own_guard():
        with installer_lock.installer_guard(tmp_path):
            ready.set()
            assert release.wait(5)

    owner = threading.Thread(target=own_guard)
    owner.start()
    assert ready.wait(5)
    try:
        with pytest.raises(InstallerBusyError):
            getattr(ot, installer_name)()
    finally:
        release.set()
        owner.join(timeout=5)
    assert not owner.is_alive()


def test_certified_runtime_dispatcher_normalizes_guard_busy(
        monkeypatch, tmp_path):
    """The public tool dispatcher must preserve Task 6's stable busy response."""
    monkeypatch.setattr(installer_lock, '_default_root', lambda: tmp_path)
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')

    def download_forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO('busy dispatcher started a download')

    monkeypatch.setattr(ot, '_download_verified_artifact', download_forbidden)
    ready = threading.Event()
    release = threading.Event()

    def own_guard():
        with installer_lock.installer_guard(tmp_path):
            ready.set()
            assert release.wait(5)

    owner = threading.Thread(target=own_guard)
    owner.start()
    assert ready.wait(5)
    try:
        result = ot.install_optional_tool('node')
    finally:
        release.set()
        owner.join(timeout=5)

    assert result == {
        'ok': False,
        'tool': 'node',
        'code': INSTALLER_BUSY_CODE,
        'error': INSTALLER_BUSY_MESSAGE,
    }


@pytest.mark.parametrize('failure', ['checksum', 'unsupported'])
def test_certified_runtime_dispatcher_normalizes_real_manifest_failures(
        monkeypatch, tmp_path, failure):
    """The dispatcher must normalize real verification failures, not a mocked installer."""
    import tool_manifest

    approved_archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        b'approved-node',
    )
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        hashlib.sha256(approved_archive).hexdigest(),
        'zip',
        binary_sha256=hashlib.sha256(b'approved-node').hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tmp_path / 'private-tools')

    if failure == 'checksum':
        response = ArtifactResponse(
            _archive_payload(
                'zip',
                'node-v1.2.3-win-x64/node.exe',
                b'altered-secret-node',
            ),
            artifact.url,
        )
        code = 'checksum'
        message = 'Certified managed artifact failed checksum verification'
    else:
        class ReadForbiddenResponse(ArtifactResponse):
            def read(self, *_args, **_kwargs):
                raise ForbiddenInstallerIO(
                    'redirected private response body was read'
                )

        response = ReadForbiddenResponse(
            b'private-secret-body',
            'https://downloads.example.test/private/node-1.2.3.zip',
        )
        code = 'unsupported'
        message = (
            'Certified managed installation is unavailable for this '
            'platform or artifact'
        )

    def probe_forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO('invalid artifact reached a process probe')

    result = ot.install_optional_tool(
        'node',
        runner=probe_forbidden,
        opener=lambda *_args: response,
    )

    assert result == {
        'ok': False,
        'tool': 'node',
        'code': code,
        'error': message,
    }
    assert 'private' not in str(result).lower()
    assert 'secret' not in str(result).lower()


def test_certified_runtime_dispatcher_normalizes_opener_io_failure(
        monkeypatch, tmp_path):
    """Public API errors must not expose private paths or downloader details."""
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(
        ot,
        'managed_tools_dir',
        lambda: tmp_path / 'private-tools',
    )
    private_detail = r'C:\Users\private\cache\download-token.zip'

    def opener(*_args, **_kwargs):
        raise OSError(private_detail)

    result = ot.install_optional_tool(
        'node',
        runner=lambda *_args, **_kwargs: pytest.fail(
            'network failure reached the runtime probe'
        ),
        opener=opener,
    )

    assert result == {
        'ok': False,
        'tool': 'node',
        'code': 'network',
        'error': 'Certified managed artifact download failed',
    }
    assert private_detail not in str(result)
    assert 'private' not in str(result).lower()


def test_certified_runtime_probe_failure_preserves_old_binary_and_cleans_stage(
        monkeypatch, tmp_path):
    """A verified but unusable candidate must not replace the active runtime."""
    import tool_manifest

    archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        b'candidate-node',
    )
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        hashlib.sha256(archive).hexdigest(),
        'zip',
        binary_sha256=hashlib.sha256(b'candidate-node').hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tmp_path / 'tools')
    destination, pointer = _write_active_runtime(
        tmp_path / 'tools',
        'node',
        'win32',
        content=b'old-certified-node',
    )
    old_pointer = pointer.read_bytes()

    def opener(url, timeout):
        return ArtifactResponse(archive, artifact.url)

    result = mock.Mock(returncode=1, stdout='', stderr='cannot execute')
    with pytest.raises(ot.OptionalToolError) as caught:
        ot.install_node(runner=lambda *_a, **_k: result, opener=opener)

    assert caught.value.code == 'probe'
    assert destination.read_bytes() == b'old-certified-node'
    assert pointer.read_bytes() == old_pointer
    assert ot.managed_runtime_path('node') is None
    assert not tuple(
        (tmp_path / 'tools' / 'node' / 'versions').glob('.node-verified-*')
    )


@pytest.mark.parametrize('denied_phase', ['promotion', 'pointer'])
def test_activation_replace_denial_preserves_old_active_and_cleans_candidate(
        monkeypatch, tmp_path, denied_phase):
    """A Windows sharing denial must not strand or select a partially activated runtime."""
    import tool_manifest

    binary = b'candidate-node'
    archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        binary,
    )
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    old_binary, pointer = _write_active_runtime(
        tools,
        'node',
        'win32',
        content=b'old-active-node',
    )
    old_pointer = pointer.read_bytes()
    candidate = tools / 'node' / 'versions' / f'1.2.3-{digest}'
    denied_destination = candidate if denied_phase == 'promotion' else pointer
    real_replace = os.replace

    def deny_selected_replace(source, destination):
        if Path(destination) == denied_destination:
            raise PermissionError(13, 'sharing violation at private path')
        return real_replace(source, destination)

    monkeypatch.setattr(ot.os, 'replace', deny_selected_replace)
    probe = mock.Mock(returncode=0, stdout='v1.2.3\n', stderr='')

    with pytest.raises(ot.OptionalToolError) as caught:
        ot.install_node(
            runner=lambda *_args, **_kwargs: probe,
            opener=lambda *_args: ArtifactResponse(archive, artifact.url),
        )

    assert caught.value.code == 'activate'
    assert str(caught.value) == (
        'Certified runtime activation failed; previous installation remains unchanged'
    )
    assert pointer.read_bytes() == old_pointer
    assert old_binary.read_bytes() == b'old-active-node'
    assert ot.managed_runtime_path('node') is None
    assert not candidate.exists()
    assert not tuple((tools / 'node').glob('.current-*'))
    assert not tuple(
        (tools / 'node' / 'versions').glob('.node-verified-*')
    )


def test_post_pointer_replace_validation_failure_restores_previous_selection(
        monkeypatch, tmp_path):
    """A fault after pointer promotion must not leave current selecting deleted bytes."""
    import tool_manifest

    binary = b'candidate-node'
    archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        binary,
    )
    digest = hashlib.sha256(archive).hexdigest()
    release = f'1.2.3-{digest}'
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    old_binary, pointer = _write_active_runtime(
        tools,
        'node',
        'win32',
        content=b'old-active-node',
    )
    old_pointer = pointer.read_bytes()
    candidate = tools / 'node' / 'versions' / release
    real_require = ot._require_safe_regular_beneath
    injected = []

    def fail_once_after_pointer_replace(trusted_root, path):
        if (
            not injected
            and Path(path) == pointer
            and pointer.read_bytes() == (release + '\n').encode('ascii')
        ):
            injected.append(True)
            raise ot._activation_error()
        return real_require(trusted_root, path)

    monkeypatch.setattr(
        ot,
        '_require_safe_regular_beneath',
        fail_once_after_pointer_replace,
    )

    with pytest.raises(ot.OptionalToolError) as caught:
        ot.install_node(
            runner=lambda *_args, **_kwargs: mock.Mock(
                returncode=0,
                stdout='v1.2.3\n',
                stderr='',
            ),
            opener=lambda *_args: ArtifactResponse(archive, artifact.url),
        )

    assert injected == [True]
    assert caught.value.code == 'activate'
    assert str(caught.value) == (
        'Certified runtime activation failed; previous installation remains unchanged'
    )
    assert pointer.read_bytes() == old_pointer
    assert old_binary.read_bytes() == b'old-active-node'
    assert ot.managed_runtime_path('node') is None
    assert not candidate.exists()
    assert not tuple((tools / 'node').glob('.current-*'))


def test_failed_pointer_rollback_retains_selected_candidate_without_dangling(
        monkeypatch, tmp_path):
    """If rollback itself fails, never delete the release selected by current."""
    import tool_manifest

    binary = b'candidate-node'
    archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        binary,
    )
    digest = hashlib.sha256(archive).hexdigest()
    release = f'1.2.3-{digest}'
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    _old_binary, pointer = _write_active_runtime(
        tools,
        'node',
        'win32',
        content=b'old-active-node',
    )
    candidate = tools / 'node' / 'versions' / release
    candidate_binary = candidate / 'node.exe'
    real_require = ot._require_safe_regular_beneath
    injected = []

    def fail_once_after_pointer_replace(trusted_root, path):
        if (
            not injected
            and Path(path) == pointer
            and pointer.read_bytes() == (release + '\n').encode('ascii')
        ):
            injected.append(True)
            raise ot._activation_error()
        return real_require(trusted_root, path)

    monkeypatch.setattr(
        ot,
        '_require_safe_regular_beneath',
        fail_once_after_pointer_replace,
    )
    monkeypatch.setattr(
        ot,
        '_restore_runtime_pointer',
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(ot.OptionalToolError) as caught:
        ot.install_node(
            runner=lambda *_args, **_kwargs: mock.Mock(
                returncode=0,
                stdout='v1.2.3\n',
                stderr='',
            ),
            opener=lambda *_args: ArtifactResponse(archive, artifact.url),
        )

    assert injected == [True]
    assert caught.value.code == 'activate'
    assert str(caught.value) == (
        'Certified runtime activation could not be verified; '
        'candidate files were retained'
    )
    assert pointer.read_text(encoding='ascii') == release + '\n'
    assert candidate_binary.read_bytes() == binary
    assert ot.managed_runtime_path('node') == candidate_binary


def test_reinstall_reuses_identical_version_directory_without_replacing_binary(
        monkeypatch, tmp_path):
    """Reinstalling an approved version must not replace its potentially in-use executable."""
    import tool_manifest

    binary = b'candidate-node'
    archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        binary,
    )
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    _old_binary, pointer = _write_active_runtime(tools, 'node', 'win32')
    release = f'1.2.3-{digest}'
    existing = tools / 'node' / 'versions' / release / 'node.exe'
    existing.parent.mkdir(parents=True)
    existing.write_bytes(binary)
    (existing.parent / 'approved-artifact.zip').write_bytes(archive)
    real_replace = os.replace

    def forbid_binary_directory_replace(source, destination):
        if Path(destination) == existing.parent:
            raise ForbiddenInstallerIO('in-use version directory was replaced')
        return real_replace(source, destination)

    monkeypatch.setattr(ot.os, 'replace', forbid_binary_directory_replace)

    result = ot.install_node(
        runner=lambda *_args, **_kwargs: mock.Mock(
            returncode=0,
            stdout='v1.2.3\n',
            stderr='',
        ),
        opener=lambda *_args: ArtifactResponse(archive, artifact.url),
    )

    assert result['ok'] is True
    assert result['path'] == str(existing)
    assert existing.read_bytes() == binary
    assert pointer.read_text(encoding='ascii') == release + '\n'


@pytest.mark.parametrize(
    ('tool', 'archive_member', 'relative_binary', 'probe_output'),
    [
        (
            'node',
            'node-v1.2.3-win-x64/node.exe',
            Path('node.exe'),
            'v1.2.3\n',
        ),
        (
            'deno',
            'deno.exe',
            Path('bin/deno.exe'),
            'deno 1.2.3\n',
        ),
    ],
)
def test_reinstall_self_heals_safe_exact_release_missing_expected_binary(
        monkeypatch, tmp_path, tool, archive_member, relative_binary,
        probe_output):
    """A safely contained partial exact release is replaced by verified bytes."""
    import tool_manifest

    binary = f'candidate-{tool}'.encode('ascii')
    archive = _archive_payload('zip', archive_member, binary)
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    _old_binary, pointer = _write_active_runtime(
        tools,
        tool,
        'win32',
        content=f'old-active-{tool}'.encode('ascii'),
    )
    old_pointer = pointer.read_bytes()
    release = f'{artifact.version}-{artifact.sha256}'
    exact_directory = tools / tool / 'versions' / release
    exact_directory.mkdir(parents=True)
    partial_marker = exact_directory / 'partial-download.marker'
    partial_marker.write_bytes(b'incomplete')
    probes = []

    def runner(command, **_kwargs):
        candidate = Path(command[0])
        assert candidate.read_bytes() == binary
        probes.append(candidate)
        return mock.Mock(returncode=0, stdout=probe_output, stderr='')

    installer = ot.install_node if tool == 'node' else ot.install_deno
    result = installer(
        runner=runner,
        opener=lambda *_args: ArtifactResponse(archive, artifact.url),
    )

    exact_binary = exact_directory / relative_binary
    assert result['ok'] is True
    assert result['path'] == str(exact_binary)
    assert exact_binary.read_bytes() == binary
    assert not partial_marker.exists()
    assert pointer.read_text(encoding='ascii') == release + '\n'
    assert pointer.read_bytes() != old_pointer
    assert len(probes) == 1
    assert not tuple((tools / tool / 'versions').glob('.corrupt-*'))


@pytest.mark.parametrize(
    ('tool', 'archive_member', 'relative_binary', 'probe_output'),
    [
        (
            'node',
            'node-v1.2.3-win-x64/node.exe',
            Path('node.exe'),
            'v1.2.3\n',
        ),
        (
            'deno',
            'deno.exe',
            Path('bin/deno.exe'),
            'deno 1.2.3\n',
        ),
    ],
)
def test_reinstall_self_heals_selected_exact_release_missing_expected_binary(
        monkeypatch, tmp_path, tool, archive_member, relative_binary,
        probe_output):
    """The exact selected partial release is recoverable without trusting it."""
    import tool_manifest

    binary = f'candidate-{tool}'.encode('ascii')
    archive = _archive_payload('zip', archive_member, binary)
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    release = f'{artifact.version}-{artifact.sha256}'
    exact_directory = tools / tool / 'versions' / release
    exact_directory.mkdir(parents=True)
    partial_marker = exact_directory / 'partial-download.marker'
    partial_marker.write_bytes(b'incomplete')
    pointer = tools / tool / 'current'
    pointer.write_text(release + '\n', encoding='ascii')

    installer = ot.install_node if tool == 'node' else ot.install_deno
    result = installer(
        runner=lambda *_args, **_kwargs: mock.Mock(
            returncode=0,
            stdout=probe_output,
            stderr='',
        ),
        opener=lambda *_args: ArtifactResponse(archive, artifact.url),
    )

    exact_binary = exact_directory / relative_binary
    assert result['ok'] is True
    assert result['path'] == str(exact_binary)
    assert exact_binary.read_bytes() == binary
    assert not partial_marker.exists()
    assert pointer.read_text(encoding='ascii') == release + '\n'
    assert not tuple((tools / tool / 'versions').glob('.corrupt-*'))


@pytest.mark.parametrize(
    ('tool', 'archive_member', 'relative_binary', 'probe_output'),
    [
        (
            'node',
            'node-v1.2.3-win-x64/node.exe',
            Path('node.exe'),
            'v1.2.3\n',
        ),
        (
            'deno',
            'deno.exe',
            Path('bin/deno.exe'),
            'deno 1.2.3\n',
        ),
    ],
)
def test_reinstall_recovers_selected_exact_release_when_directory_is_absent(
        monkeypatch, tmp_path, tool, archive_member, relative_binary,
        probe_output):
    """An exact dangling pointer may be repaired only from verified staged bytes."""
    import tool_manifest

    binary = f'candidate-{tool}'.encode('ascii')
    archive = _archive_payload('zip', archive_member, binary)
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    release = f'{artifact.version}-{artifact.sha256}'
    versions = tools / tool / 'versions'
    versions.mkdir(parents=True)
    exact_directory = versions / release
    pointer = tools / tool / 'current'
    pointer.write_text(release + '\n', encoding='ascii')
    assert not exact_directory.exists()

    installer = ot.install_node if tool == 'node' else ot.install_deno
    result = installer(
        runner=lambda *_args, **_kwargs: mock.Mock(
            returncode=0,
            stdout=probe_output,
            stderr='',
        ),
        opener=lambda *_args: ArtifactResponse(archive, artifact.url),
    )

    exact_binary = exact_directory / relative_binary
    assert result['ok'] is True
    assert result['path'] == str(exact_binary)
    assert exact_binary.read_bytes() == binary
    assert pointer.read_text(encoding='ascii') == release + '\n'


@pytest.mark.parametrize(
    ('tool', 'archive_member'),
    [
        ('node', 'node-v1.2.3-win-x64/node.exe'),
        ('deno', 'deno.exe'),
    ],
)
def test_reinstall_rejects_dangling_pointer_to_nonselected_missing_release(
        monkeypatch, tmp_path, tool, archive_member):
    """A dangling pointer for any other release remains a pre-I/O failure."""
    import tool_manifest

    binary = f'candidate-{tool}'.encode('ascii')
    archive = _archive_payload('zip', archive_member, binary)
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    versions = tools / tool / 'versions'
    versions.mkdir(parents=True)
    pointer = tools / tool / 'current'
    other_release = f'9.9.9-{"b" * 64}'
    pointer.write_text(other_release + '\n', encoding='ascii')
    io_attempts = []

    installer = ot.install_node if tool == 'node' else ot.install_deno
    with pytest.raises(ot.OptionalToolError) as caught:
        installer(
            runner=lambda *_args, **_kwargs: io_attempts.append('probe'),
            opener=lambda *_args: io_attempts.append('download'),
        )

    assert caught.value.code == 'activate'
    assert io_attempts == []
    assert pointer.read_text(encoding='ascii') == other_release + '\n'
    assert not (versions / other_release).exists()


@pytest.mark.parametrize(
    ('tool', 'archive_member', 'relative_binary', 'probe_output'),
    [
        (
            'node',
            'node-v1.2.3-win-x64/node.exe',
            Path('node.exe'),
            'v1.2.3\n',
        ),
        (
            'deno',
            'deno.exe',
            Path('bin/deno.exe'),
            'deno 1.2.3\n',
        ),
    ],
)
def test_partial_exact_release_and_active_pointer_are_restored_when_promotion_fails(
        monkeypatch, tmp_path, tool, archive_member, relative_binary,
        probe_output):
    """A failed replacement restores the exact safe partial directory and pointer."""
    import tool_manifest

    binary = f'candidate-{tool}'.encode('ascii')
    archive = _archive_payload('zip', archive_member, binary)
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    old_binary, pointer = _write_active_runtime(
        tools,
        tool,
        'win32',
        content=f'old-active-{tool}'.encode('ascii'),
    )
    old_pointer = pointer.read_bytes()
    old_binary_bytes = old_binary.read_bytes()
    release = f'{artifact.version}-{artifact.sha256}'
    exact_directory = tools / tool / 'versions' / release
    exact_directory.mkdir(parents=True)
    partial_marker = exact_directory / 'partial-download.marker'
    partial_marker.write_bytes(b'incomplete')
    real_replace = os.replace
    promotion_attempted = []

    def deny_candidate_promotion(source, destination):
        source = Path(source)
        destination = Path(destination)
        if destination == exact_directory and source.name == 'release':
            promotion_attempted.append(True)
            raise PermissionError('promotion denied')
        return real_replace(source, destination)

    monkeypatch.setattr(ot.os, 'replace', deny_candidate_promotion)
    installer = ot.install_node if tool == 'node' else ot.install_deno

    with pytest.raises(ot.OptionalToolError) as caught:
        installer(
            runner=lambda *_args, **_kwargs: mock.Mock(
                returncode=0,
                stdout=probe_output,
                stderr='',
            ),
            opener=lambda *_args: ArtifactResponse(archive, artifact.url),
        )

    assert caught.value.code == 'activate'
    assert promotion_attempted == [True]
    assert not (exact_directory / relative_binary).exists()
    assert partial_marker.read_bytes() == b'incomplete'
    assert old_binary.read_bytes() == old_binary_bytes
    assert pointer.read_bytes() == old_pointer
    assert not tuple((tools / tool / 'versions').glob('.corrupt-*'))


@pytest.mark.parametrize(
    ('tool', 'archive_member', 'relative_binary', 'probe_output'),
    [
        (
            'node',
            'node-v1.2.3-win-x64/node.exe',
            Path('node.exe'),
            'v1.2.3\n',
        ),
        (
            'deno',
            'deno.exe',
            Path('bin/deno.exe'),
            'deno 1.2.3\n',
        ),
    ],
)
def test_selected_partial_release_and_pointer_are_restored_when_promotion_fails(
        monkeypatch, tmp_path, tool, archive_member, relative_binary,
        probe_output):
    """A failed repair restores the exact partial directory selected on entry."""
    import tool_manifest

    binary = f'candidate-{tool}'.encode('ascii')
    archive = _archive_payload('zip', archive_member, binary)
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    release = f'{artifact.version}-{artifact.sha256}'
    exact_directory = tools / tool / 'versions' / release
    exact_directory.mkdir(parents=True)
    partial_marker = exact_directory / 'partial-download.marker'
    partial_marker.write_bytes(b'incomplete')
    pointer = tools / tool / 'current'
    pointer.write_text(release + '\n', encoding='ascii')
    previous_pointer = pointer.read_bytes()
    real_replace = os.replace
    promotion_attempted = []

    def deny_candidate_promotion(source, destination):
        source = Path(source)
        destination = Path(destination)
        if destination == exact_directory and source.name == 'release':
            promotion_attempted.append(True)
            raise PermissionError('promotion denied')
        return real_replace(source, destination)

    monkeypatch.setattr(ot.os, 'replace', deny_candidate_promotion)
    installer = ot.install_node if tool == 'node' else ot.install_deno

    with pytest.raises(ot.OptionalToolError) as caught:
        installer(
            runner=lambda *_args, **_kwargs: mock.Mock(
                returncode=0,
                stdout=probe_output,
                stderr='',
            ),
            opener=lambda *_args: ArtifactResponse(archive, artifact.url),
        )

    assert caught.value.code == 'activate'
    assert promotion_attempted == [True]
    assert not (exact_directory / relative_binary).exists()
    assert partial_marker.read_bytes() == b'incomplete'
    assert pointer.read_bytes() == previous_pointer
    assert not tuple((tools / tool / 'versions').glob('.corrupt-*'))


@pytest.mark.parametrize(
    ('tool', 'archive_member', 'probe_output'),
    [
        (
            'node',
            'node-v1.2.3-win-x64/node.exe',
            'v1.2.3\n',
        ),
        (
            'deno',
            'deno.exe',
            'deno 1.2.3\n',
        ),
    ],
)
def test_exact_dangling_pointer_is_preserved_when_initial_promotion_fails(
        monkeypatch, tmp_path, tool, archive_member, probe_output):
    """A failed exact-pointer repair leaves the original absent target untouched."""
    import tool_manifest

    binary = f'candidate-{tool}'.encode('ascii')
    archive = _archive_payload('zip', archive_member, binary)
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    release = f'{artifact.version}-{artifact.sha256}'
    versions = tools / tool / 'versions'
    versions.mkdir(parents=True)
    exact_directory = versions / release
    pointer = tools / tool / 'current'
    pointer.write_text(release + '\n', encoding='ascii')
    previous_pointer = pointer.read_bytes()
    real_replace = os.replace
    promotion_attempted = []

    def deny_candidate_promotion(source, destination):
        source = Path(source)
        destination = Path(destination)
        if destination == exact_directory and source.name == 'release':
            promotion_attempted.append(True)
            raise PermissionError('promotion denied')
        return real_replace(source, destination)

    monkeypatch.setattr(ot.os, 'replace', deny_candidate_promotion)
    installer = ot.install_node if tool == 'node' else ot.install_deno

    with pytest.raises(ot.OptionalToolError) as caught:
        installer(
            runner=lambda *_args, **_kwargs: mock.Mock(
                returncode=0,
                stdout=probe_output,
                stderr='',
            ),
            opener=lambda *_args: ArtifactResponse(archive, artifact.url),
        )

    assert caught.value.code == 'activate'
    assert promotion_attempted == [True]
    assert pointer.read_bytes() == previous_pointer
    assert not exact_directory.exists()
    assert not tuple(versions.glob('.corrupt-*'))


def test_reinstall_self_heals_safe_corrupt_exact_release(
        monkeypatch, tmp_path):
    """A bad exact-version cache must not permanently brick certified reinstall."""
    import tool_manifest

    binary = b'candidate-node'
    archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        binary,
    )
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    _old_binary, pointer = _write_active_runtime(tools, 'node', 'win32')
    old_pointer = pointer.read_bytes()
    release = f'{artifact.version}-{artifact.sha256}'
    exact_binary = tools / 'node' / 'versions' / release / 'node.exe'
    exact_binary.parent.mkdir(parents=True)
    exact_binary.write_bytes(b'corrupt-cached-node')
    (exact_binary.parent / 'approved-artifact.zip').write_bytes(archive)
    probes = []

    def runner(command, **_kwargs):
        candidate = Path(command[0])
        assert candidate.read_bytes() == binary
        assert candidate != exact_binary
        probes.append(candidate)
        return mock.Mock(returncode=0, stdout='v1.2.3\n', stderr='')

    result = ot.install_node(
        runner=runner,
        opener=lambda *_args: ArtifactResponse(archive, artifact.url),
    )

    assert result['ok'] is True
    assert exact_binary.read_bytes() == binary
    assert pointer.read_text(encoding='ascii') == release + '\n'
    assert pointer.read_bytes() != old_pointer
    assert len(probes) == 1
    assert not tuple((tools / 'node' / 'versions').glob('.corrupt-*'))


@pytest.mark.parametrize(
    ('tool', 'archive_member', 'relative_binary', 'probe_output'),
    [
        (
            'node',
            'node-v1.2.3-win-x64/node.exe',
            Path('node.exe'),
            'v1.2.3\n',
        ),
        (
            'deno',
            'deno.exe',
            Path('bin/deno.exe'),
            'deno 1.2.3\n',
        ),
    ],
)
def test_denied_initial_quarantine_move_reports_normal_activation_failure(
        monkeypatch, tmp_path, tool, archive_member, relative_binary,
        probe_output):
    """A quarantine path is not rollback state until the first rename succeeds."""
    import tool_manifest

    candidate_binary = f'candidate-{tool}'.encode('ascii')
    corrupt_binary = f'corrupt-cached-{tool}'.encode('ascii')
    archive = _archive_payload('zip', archive_member, candidate_binary)
    artifact = tool_manifest.ToolArtifact(
        tool,
        '1.2.3',
        'win32',
        'x86_64',
        f'https://downloads.example.test/{tool}-1.2.3.zip',
        hashlib.sha256(archive).hexdigest(),
        'zip',
        binary_sha256=hashlib.sha256(candidate_binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)

    active_binary, pointer = _write_active_runtime(
        tools,
        tool,
        'win32',
        content=f'old-certified-{tool}'.encode('ascii'),
    )
    active_binary_before = active_binary.read_bytes()
    pointer_before = pointer.read_bytes()
    release = f'{artifact.version}-{artifact.sha256}'
    versions = tools / tool / 'versions'
    exact_release = versions / release
    exact_binary = exact_release / relative_binary
    exact_binary.parent.mkdir(parents=True)
    exact_binary.write_bytes(corrupt_binary)
    exact_archive = exact_release / 'approved-artifact.zip'
    exact_archive.write_bytes(archive)
    release_marker = exact_release / 'existing-release.marker'
    release_marker.write_bytes(b'existing-release')
    exact_release_before = {
        path.relative_to(exact_release): path.read_bytes()
        for path in exact_release.rglob('*')
        if path.is_file()
    }
    expected_release_names = {
        active_binary.parents[len(relative_binary.parts) - 1].name,
        release,
    }
    real_replace = os.replace
    quarantine_attempts = []

    def deny_initial_quarantine_move(source, destination):
        source = Path(source)
        destination = Path(destination)
        if source == exact_release and destination.name.startswith('.corrupt-'):
            quarantine_attempts.append((source, destination))
            raise PermissionError('quarantine denied')
        return real_replace(source, destination)

    monkeypatch.setattr(ot.os, 'replace', deny_initial_quarantine_move)
    installer = ot.install_node if tool == 'node' else ot.install_deno

    with pytest.raises(ot.OptionalToolError) as caught:
        installer(
            runner=lambda *_args, **_kwargs: mock.Mock(
                returncode=0,
                stdout=probe_output,
                stderr='',
            ),
            opener=lambda *_args: ArtifactResponse(archive, artifact.url),
        )

    assert caught.value.code == 'activate'
    assert str(caught.value) == (
        'Certified runtime activation failed; previous installation remains unchanged'
    )
    assert len(quarantine_attempts) == 1
    assert active_binary.read_bytes() == active_binary_before
    assert pointer.read_bytes() == pointer_before
    assert {
        path.relative_to(exact_release): path.read_bytes()
        for path in exact_release.rglob('*')
        if path.is_file()
    } == exact_release_before
    assert exact_binary.read_bytes() == corrupt_binary
    assert exact_archive.read_bytes() == archive
    assert release_marker.read_bytes() == b'existing-release'
    assert not tuple(versions.glob('.corrupt-*'))
    assert not tuple(versions.glob(f'.{tool}-verified-*'))
    assert {path.name for path in versions.iterdir()} == expected_release_names


def test_corrupt_exact_release_is_restored_when_replacement_promotion_fails(
        monkeypatch, tmp_path):
    """Quarantining a corrupt cache remains rollback-safe until promotion succeeds."""
    import tool_manifest

    binary = b'candidate-node'
    corrupt_binary = b'corrupt-cached-node'
    archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        binary,
    )
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    _old_binary, pointer = _write_active_runtime(tools, 'node', 'win32')
    old_pointer = pointer.read_bytes()
    release = f'{artifact.version}-{artifact.sha256}'
    exact_binary = tools / 'node' / 'versions' / release / 'node.exe'
    exact_binary.parent.mkdir(parents=True)
    exact_binary.write_bytes(corrupt_binary)
    (exact_binary.parent / 'approved-artifact.zip').write_bytes(archive)
    real_replace = os.replace
    promotion_attempted = []

    def deny_candidate_promotion(source, destination):
        source = Path(source)
        destination = Path(destination)
        if destination == exact_binary.parent and source.name == 'release':
            promotion_attempted.append(True)
            raise PermissionError('promotion denied')
        return real_replace(source, destination)

    monkeypatch.setattr(ot.os, 'replace', deny_candidate_promotion)

    with pytest.raises(ot.OptionalToolError) as caught:
        ot.install_node(
            runner=lambda *_args, **_kwargs: mock.Mock(
                returncode=0,
                stdout='v1.2.3\n',
                stderr='',
            ),
            opener=lambda *_args: ArtifactResponse(archive, artifact.url),
        )

    assert caught.value.code == 'activate'
    assert promotion_attempted == [True]
    assert exact_binary.read_bytes() == corrupt_binary
    assert pointer.read_bytes() == old_pointer
    assert not tuple((tools / 'node' / 'versions').glob('.corrupt-*'))


@pytest.mark.parametrize(
    ('tool', 'platform_name', 'legacy_relative'),
    [
        ('node', 'win32', Path('node/node.exe')),
        ('node', 'darwin', Path('node/bin/node')),
        ('deno', 'win32', Path('deno/bin/deno.exe')),
        ('deno', 'darwin', Path('deno/bin/deno')),
    ],
)
def test_legacy_runtime_is_reported_unverified_but_never_returned_for_execution(
        monkeypatch, tmp_path, tool, platform_name, legacy_relative):
    """A legacy report may remain visible, but path-only consumers must never execute it."""
    monkeypatch.setattr(ot.sys, 'platform', platform_name)
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tmp_path / 'tools')
    legacy = tmp_path / 'tools' / legacy_relative
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b'legacy-runtime')

    assert ot.managed_runtime_path(tool) is None
    assert ot.discover_managed_runtime(tool) == {
        'path': str(legacy),
        'verified': False,
        'source': 'legacy',
    }


@pytest.mark.parametrize(
    ('release_mode', 'preserve_approved_archive'),
    [
        ('approved-name-without-archive', False),
        ('approved-name-tampered-binary', True),
        ('old-unapproved-release', True),
    ],
)
def test_execution_resolver_rejects_preseeded_runtime_without_archive_proof(
        monkeypatch, tmp_path, release_mode, preserve_approved_archive):
    """Writable app-data names and retained archives cannot certify wrong bytes."""
    import tool_manifest

    approved_binary = b'approved-node-binary'
    archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        approved_binary,
    )
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-v1.2.3.zip',
        digest,
        'zip',
        binary_sha256=hashlib.sha256(approved_binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    release = (
        f'{artifact.version}-{artifact.sha256}'
        if release_mode != 'old-unapproved-release'
        else '0.9.0-' + ('a' * 64)
    )
    binary, pointer = _write_active_runtime(
        tools,
        'node',
        'win32',
        release=release,
        content=b'attacker-controlled-node',
    )
    if preserve_approved_archive:
        (binary.parent / 'approved-artifact.zip').write_bytes(archive)

    assert pointer.read_text(encoding='ascii').strip() == release
    assert ot.managed_runtime_path('node') is None
    assert ot.discover_managed_runtime('node')['verified'] is False


def test_execution_resolver_uses_manifest_digest_without_reopening_archive(
        monkeypatch, tmp_path):
    """Per-job runtime handoff hashes the binary and never decompresses its archive."""
    import tool_manifest

    approved_binary = b'approved-node'
    approved_archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        approved_binary,
    )
    malicious_archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        b'attacker-node',
    )
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-v1.2.3.zip',
        hashlib.sha256(approved_archive).hexdigest(),
        'zip',
        binary_sha256=hashlib.sha256(approved_binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    release = f'{artifact.version}-{artifact.sha256}'
    binary, _pointer = _write_active_runtime(
        tools,
        'node',
        'win32',
        release=release,
        content=approved_binary,
    )
    archive_path = binary.parent / 'approved-artifact.zip'
    archive_path.write_bytes(approved_archive)
    archive_path.write_bytes(malicious_archive)
    monkeypatch.setattr(
        ot,
        '_read_safe_regular_bytes',
        lambda *_args, **_kwargs: pytest.fail(
            'execution handoff reopened the retained archive'
        ),
    )

    assert ot.managed_runtime_path('node') == binary


def test_execution_resolver_stream_hashes_installed_binary(
        monkeypatch, tmp_path):
    """Certification must not materialize a potentially 256 MiB executable."""
    import tool_manifest

    approved_binary = b'approved-node'
    approved_archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        approved_binary,
    )
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-v1.2.3.zip',
        hashlib.sha256(approved_archive).hexdigest(),
        'zip',
        binary_sha256=hashlib.sha256(approved_binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    release = f'{artifact.version}-{artifact.sha256}'
    binary, _pointer = _write_active_runtime(
        tools,
        'node',
        'win32',
        release=release,
        content=approved_binary,
    )
    monkeypatch.setattr(
        ot,
        '_read_safe_regular_bytes',
        lambda *_args, **_kwargs: pytest.fail(
            'execution handoff materialized a file instead of stream-hashing'
        ),
    )

    assert ot.managed_runtime_path('node') == binary


def test_execution_resolver_rejects_reparse_app_root_before_provenance_or_probe(
        monkeypatch, tmp_path):
    """The raw app-data directory is part of the executable trust boundary."""
    import tool_manifest

    approved_binary = b'approved-node'
    approved_archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        approved_binary,
    )
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-v1.2.3.zip',
        hashlib.sha256(approved_archive).hexdigest(),
        'zip',
        binary_sha256=hashlib.sha256(approved_binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    app_root = tmp_path / 'yt_final'
    tools = app_root / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    release = f'{artifact.version}-{artifact.sha256}'
    binary, _pointer = _write_active_runtime(
        tools,
        'node',
        'win32',
        release=release,
        content=approved_binary,
    )
    (binary.parent / 'approved-artifact.zip').write_bytes(approved_archive)
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

    assert ot.managed_runtime_path('node') is None
    assert ot.discover_managed_runtime('node')['verified'] is False


@pytest.mark.parametrize(
    'unsafe_component',
    ['tools-root', 'tool-root', 'versions', 'current', 'release', 'binary'],
)
def test_execution_resolver_rejects_symlinked_managed_path_component(
        monkeypatch, tmp_path, unsafe_component):
    """No path-only consumer may execute through a linked trust-boundary component."""
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    binary, pointer = _write_active_runtime(tools, 'node', 'win32')
    release = binary.parent
    targets = {
        'tools-root': tools,
        'tool-root': tools / 'node',
        'versions': tools / 'node' / 'versions',
        'current': pointer,
        'release': release,
        'binary': binary,
    }
    _mark_path_as_symlink(monkeypatch, targets[unsafe_component])

    assert ot.managed_runtime_path('node') is None


def test_execution_resolver_rejects_malicious_macos_bin_symlink(
        monkeypatch, tmp_path):
    """A regular binary reached through a malicious macOS bin symlink is untrusted."""
    monkeypatch.setattr(ot.sys, 'platform', 'darwin')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    binary, _pointer = _write_active_runtime(tools, 'node', 'darwin')
    _mark_path_as_symlink(monkeypatch, binary.parent)

    assert ot.managed_runtime_path('node') is None


def test_execution_resolver_rejects_windows_reparse_versions_directory(
        monkeypatch, tmp_path):
    """Windows junctions may not report as symlinks; the reparse attribute is authoritative."""
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    _binary, _pointer = _write_active_runtime(tools, 'node', 'win32')
    versions = tools / 'node' / 'versions'
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
        if candidate == versions:
            return ReparseMetadata(metadata)
        return metadata

    monkeypatch.setattr(Path, 'lstat', reparse_lstat)

    assert ot.managed_runtime_path('node') is None


@pytest.mark.parametrize(
    'pointer_bytes',
    [
        b'',
        b'../outside\n',
        b'1.2.3-' + (b'a' * 64),
        b' 1.2.3-' + (b'a' * 64) + b'\n',
        b'1.2.3-' + (b'a' * 64) + b'\nextra\n',
        b'\xff\n',
    ],
)
def test_execution_resolver_rejects_corrupt_pointer(
        monkeypatch, tmp_path, pointer_bytes):
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    _binary, pointer = _write_active_runtime(tools, 'node', 'win32')
    pointer.write_bytes(pointer_bytes)

    assert ot.managed_runtime_path('node') is None


def test_execution_resolver_rejects_nonregular_pointer(monkeypatch, tmp_path):
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    _binary, pointer = _write_active_runtime(tools, 'node', 'win32')
    pointer.unlink()
    pointer.mkdir()

    assert ot.managed_runtime_path('node') is None


@pytest.mark.parametrize('unsafe_component', ['tool-root', 'versions', 'current'])
def test_installer_rejects_unsafe_managed_component_before_probe(
        monkeypatch, tmp_path, unsafe_component):
    """A link/reparse in the persistent layout must stop before executing staged bytes."""
    import tool_manifest

    binary = b'candidate-node'
    archive = _archive_payload(
        'zip',
        'node-v1.2.3-win-x64/node.exe',
        binary,
    )
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'win32',
        'x86_64',
        'https://downloads.example.test/node-v1.2.3.zip',
        hashlib.sha256(archive).hexdigest(),
        'zip',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'win32')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'AMD64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    _old_binary, pointer = _write_active_runtime(tools, 'node', 'win32')
    targets = {
        'tool-root': tools / 'node',
        'versions': tools / 'node' / 'versions',
        'current': pointer,
    }
    _mark_path_as_symlink(monkeypatch, targets[unsafe_component])

    def io_forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO(
            'unsafe managed layout reached network or binary process I/O'
        )

    with pytest.raises(ot.OptionalToolError) as caught:
        ot.install_node(
            runner=io_forbidden,
            opener=io_forbidden,
        )

    assert caught.value.code == 'activate'
    assert str(caught.value) == (
        'Certified runtime activation failed; previous installation remains unchanged'
    )


def test_installer_rejects_existing_release_with_macos_bin_symlink(
        monkeypatch, tmp_path):
    """An existing release reached through bin linkage cannot be reused or selected."""
    import tool_manifest

    binary = b'candidate-node'
    archive = _archive_payload(
        'tar.gz',
        'node-v1.2.3-darwin-arm64/bin/node',
        binary,
    )
    digest = hashlib.sha256(archive).hexdigest()
    artifact = tool_manifest.ToolArtifact(
        'node',
        '1.2.3',
        'darwin',
        'arm64',
        'https://downloads.example.test/node-v1.2.3.tar.gz',
        digest,
        'tar.gz',
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    monkeypatch.setattr(tool_manifest, 'ARTIFACTS', (artifact,))
    monkeypatch.setattr(ot.sys, 'platform', 'darwin')
    monkeypatch.setattr(ot.py_platform, 'machine', lambda: 'arm64')
    tools = tmp_path / 'tools'
    monkeypatch.setattr(ot, 'managed_tools_dir', lambda: tools)
    old_binary, pointer = _write_active_runtime(tools, 'node', 'darwin')
    old_pointer = pointer.read_bytes()
    candidate = (
        tools / 'node' / 'versions' / f'1.2.3-{digest}' / 'bin' / 'node'
    )
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(binary)
    _mark_path_as_symlink(monkeypatch, candidate.parent)

    with pytest.raises(ot.OptionalToolError) as caught:
        ot.install_node(
            runner=lambda *_args, **_kwargs: mock.Mock(
                returncode=0,
                stdout='v1.2.3\n',
                stderr='',
            ),
            opener=lambda *_args: ArtifactResponse(archive, artifact.url),
        )

    assert caught.value.code == 'activate'
    assert pointer.read_bytes() == old_pointer
    assert old_binary.read_bytes() == b'old-certified-binary'


@pytest.mark.parametrize(
    'snippet',
    [
        '/releases/latest',
        'curl -fsSL',
        "'pull', '--ff-only'",
        "'clone', '--depth'",
        "'npm', 'install'",
        "'npm', 'ci'",
        "'docker', 'pull'",
        "'winget', 'install'",
        "'brew', 'install'",
        "'playwright', 'install', 'chromium'",
    ],
)
def test_mutable_optional_installer_source_pattern_is_absent(snippet):
    """Each retired bootstrap family must remain absent from shipped runtime code."""
    source = Path(ot.__file__).read_text(encoding='utf-8')
    compact = ' '.join(source.split())

    assert snippet not in compact


@pytest.mark.parametrize(
    'name',
    [
        '_github_latest_assets',
        '_pick_asset',
        '_extract_binary_from_archive',
        '_download_bytes',
        '_open_https',
        '_machine_tag',
        '_guarded_installer',
        '_spawnable',
        '_windows_env_path_dirs',
        '_which_refreshed',
        '_brew_available',
        '_official_node_lts_latest',
        '_localhost_bind_patch',
        '_socket_bind_is_loopback_only',
        '_start_bgutil_http_native',
    ],
)
def test_retired_mutable_optional_installer_helper_is_absent(name):
    """A removed helper must not remain callable as an accidental bypass."""
    assert not hasattr(ot, name)


def test_find_bgutil_script_path_managed_and_home(tmp_path, monkeypatch):
    managed = tmp_path / 'managed' / 'bgutil-ytdlp-pot-provider' / 'server' / 'build'
    managed.mkdir(parents=True)
    script = managed / 'generate_once.js'
    script.write_text('// pot')
    monkeypatch.setattr(ot, 'managed_bgutil_script_path', lambda: script)
    monkeypatch.setattr(ot.Path, 'home', classmethod(lambda cls: tmp_path / 'nohome'))
    assert ot.find_bgutil_script_path() == script


def test_managed_paths_are_under_app_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(ot, 'app_data_dir', lambda: tmp_path)
    assert ot.managed_aria2_path().parent.name == 'aria2'
    assert ot.managed_turbo_path().parent.name == 'N_m3u8DL-RE'
    assert 'tools' in ot.managed_aria2_path().parts


def test_install_optional_tool_rejects_unknown():
    r = ot.install_optional_tool('evil-tool')
    assert r['ok'] is False
    assert r['code'] == 'unsupported'


def test_ambient_aria_binary_does_not_turn_manual_guidance_into_certification(
        monkeypatch):
    """A PATH hit has no independent digest and must not re-enable automation."""
    def probe_forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO('manual aria path probed an ambient binary')

    monkeypatch.setattr(ot.shutil, 'which', probe_forbidden)

    assert ot.install_aria2c() == MANUAL_ONLY_RESULTS['aria2c']


def _certified_host_version(tool: str) -> str | None:
    """Version the immutable catalog certifies for THIS host, else None.

    The catalog carries Node/Deno rows for win32/x86_64 and darwin/arm64 only;
    macOS x86_64 has no certified row, so the scan must offer nothing there.
    """
    import tool_manifest

    try:
        return tool_manifest.artifact_for(tool).version
    except tool_manifest.UnsupportedManagedInstall:
        return None


def test_scan_marks_only_certified_manifest_tools_installable():
    from yt_final import UpdateChecker
    UpdateChecker.invalidate_cache()
    report = UpdateChecker._scan_uncached(
        check_pypi=False, include_optional_tools=True, ffmpeg_path='ffmpeg')
    by = {r['name']: r for r in report['info']}
    # Derived from the manifest, not a hardcoded platform list, so certifying a
    # new host row (or retiring one) needs no edit here and an uncertified host
    # is still required to advertise no automatic action at all.
    for tool in ('node', 'deno'):
        certified = _certified_host_version(tool)
        assert by[tool].get('installable') is (certified is not None)
        # Offline / fast path reads certified runtime versions locally.
        assert by[tool].get('latest') == certified
    for name in (
        'aria2c',
        'N_m3u8DL-RE',
        'playwright',
        'bgutil-server',
        'bgutil-plugin',
    ):
        assert by[name].get('installable') is False
    for name in (
            'playwright', 'bgutil-plugin', 'bgutil-server',
            'aria2c', 'N_m3u8DL-RE'):
        assert by[name].get('latest') is None


def test_normalize_release_tag():
    from yt_final import UpdateChecker
    assert UpdateChecker._normalize_release_tag('v1.2.3') == '1.2.3'
    assert UpdateChecker._normalize_release_tag('release-1.37.0') == '1.37.0'
    assert UpdateChecker._normalize_release_tag('(not running)') is None
    assert UpdateChecker._normalize_release_tag(None) is None


def test_outdated_handles_sentinels_and_prerelease():
    from yt_final import UpdateChecker as U
    # Sentinels must never look outdated
    assert U._outdated('(not running)', '1.3.1') is False
    assert U._outdated('installed', '1.0.0') is False
    assert U._outdated(None, '1.0.0') is False
    # Stable install is not behind equal-base beta latest
    assert U._outdated('0.6.0', '0.6.0-beta') is False
    # Beta install is behind stable latest
    assert U._outdated('0.6.0-beta', '0.6.0') is True
    # Real lag
    assert U._outdated('v24.13.1', '24.18.0') is True
    assert U._outdated('v24.18.0', '24.18.0') is False
    # strip v / release-
    assert U._outdated('1.37.0', 'release-1.37.0') is False


def test_resolve_js_runtime_finds_certified_managed_deno(monkeypatch, tmp_path):
    from yt_final import UpdateChecker
    import optional_tools as ot_mod

    deno = tmp_path / 'deno' / 'versions' / ('2.1.0-' + 'a' * 64) / 'bin' / 'deno'
    deno.parent.mkdir(parents=True)
    deno.write_text('x')
    monkeypatch.setattr(
        ot_mod,
        'discover_managed_runtime',
        lambda tool: {
            'path': str(deno) if tool == 'deno' else None,
            'verified': tool == 'deno',
            'source': 'certified-pointer' if tool == 'deno' else 'missing',
        },
    )
    monkeypatch.setattr(UpdateChecker, '_cmd_first_line', classmethod(
        lambda cls, args, pattern=None, timeout=2: '2.1.0' if 'deno' in str(args[0]) else None
    ))
    monkeypatch.setattr('shutil.which', lambda n: None)
    info = UpdateChecker._resolve_js_runtime('deno')
    assert info['version'] == '2.1.0'
    assert info['path'] == str(deno)
    assert info['verified'] is True
    assert info['source'] == 'certified-pointer'


def test_scan_summary_lists_optional_outdated(monkeypatch):
    from yt_final import UpdateChecker

    monkeypatch.setattr(
        UpdateChecker, '_resolve_js_runtime',
        classmethod(lambda cls, name: {
            'node': {'path': '/n', 'version': 'v24.13.1'},
            'deno': {'path': None, 'version': None},
        }.get(name, {'path': None, 'version': None})),
    )
    monkeypatch.setattr(
        UpdateChecker,
        '_optional_latest_version',
        classmethod(lambda cls, name, **_kwargs: {
            'node': '24.18.0',
            'deno': '2.9.4',
        }.get(name)),
    )
    monkeypatch.setattr(
        UpdateChecker, '_pypi_latest',
        classmethod(lambda cls, pkg, timeout=3: '1.0.0'),
    )
    monkeypatch.setattr(
        UpdateChecker, '_package_probe',
        classmethod(lambda cls, pkg: {
            'version': '1.0.0', 'verified': True, 'source': 'process',
        }),
    )
    monkeypatch.setattr(
        UpdateChecker, '_ffmpeg_release_latest', classmethod(lambda cls: None))
    monkeypatch.setattr(
        UpdateChecker, '_cmd_first_line',
        classmethod(lambda cls, args, pattern=None, timeout=2: None))
    monkeypatch.setattr(
        UpdateChecker, '_bgutil_server_version',
        classmethod(lambda cls, base='http://127.0.0.1:4416': None))
    monkeypatch.setattr(
        UpdateChecker, '_installed',
        staticmethod(lambda pkg: '1.0.0'))

    UpdateChecker.invalidate_cache()
    report = UpdateChecker._scan_uncached(
        check_pypi=True, include_optional_tools=True, ffmpeg_path='ffmpeg')
    by = {r['name']: r for r in report['info']}
    assert by['node']['outdated'] is True
    assert by['node']['path'] == '/n'
    assert by['deno']['installed'] is None
    assert 'node' in report['summary'].get('optional_outdated', [])
    assert 'deno' in report['summary'].get('optional_missing', [])
    # pip ok when versions match mocks
    assert report['summary']['ok'] is True


def test_scan_fills_only_certified_optional_versions(monkeypatch):
    """Only manifest-backed runtimes and the lock-owned plugin get latest values."""
    from yt_final import UpdateChecker

    monkeypatch.setattr(
        UpdateChecker,
        '_optional_latest_version',
        classmethod(lambda cls, name, **_kwargs: {
            'node': '24.18.0',
            'deno': '2.9.4',
        }.get(name)),
    )
    monkeypatch.setattr(
        UpdateChecker,
        '_pypi_latest',
        classmethod(lambda cls, pkg, timeout=3: '1.3.1' if 'bgutil' in pkg else '1.0.0'),
    )
    monkeypatch.setattr(
        UpdateChecker, '_ffmpeg_release_latest', classmethod(lambda cls: None))

    UpdateChecker.invalidate_cache()
    report = UpdateChecker._scan_uncached(
        check_pypi=True, include_optional_tools=True, ffmpeg_path='ffmpeg')
    by = {r['name']: r for r in report['info']}
    assert by['node'].get('latest') == '24.18.0'
    assert by['deno'].get('latest') == '2.9.4'
    assert by['aria2c'].get('latest') is None
    assert by['N_m3u8DL-RE'].get('latest') is None
    assert by['bgutil-server'].get('latest') is None
    assert by['playwright'].get('latest') is None
    assert by['bgutil-plugin'].get('latest') == '1.3.1'


def test_optional_latest_version_uses_only_local_manifest(monkeypatch):
    """This replaces the retired GitHub/Node mutable-feed TTL contract."""
    from yt_final import UpdateChecker
    import urllib.request

    def network_forbidden(*_args, **_kwargs):
        raise ForbiddenInstallerIO('optional latest attempted network I/O')

    monkeypatch.setattr(urllib.request, 'urlopen', network_forbidden)

    assert UpdateChecker._optional_latest_version(
        'node', platform_name='win32', architecture='x86_64') == '24.18.0'
    assert UpdateChecker._optional_latest_version(
        'deno', platform_name='win32', architecture='x86_64') == '2.9.4'
    for name in (
            'aria2c', 'N_m3u8DL-RE', 'bgutil-server', 'playwright'):
        assert UpdateChecker._optional_latest_version(
            name, platform_name='win32', architecture='x86_64') is None
    assert not hasattr(UpdateChecker, '_github_latest_version')
    assert not hasattr(UpdateChecker, '_node_latest_version')


def test_ui_wires_optional_tool_install():
    from pathlib import Path
    js = (Path(__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')
    assert 'runOptionalToolInstall' in js
    assert "/api/updates/tools" in js
    assert 'data-tool-install' in js
    assert 'bgutil-plugin' in js
    assert 'bgutil-server' in js
    assert "'deno'" in js or '"deno"' in js
    assert "'node'" in js or '"node"' in js
    assert 'playwright' in js
    assert 'OPTIONAL_INSTALL_TOOLS' in js


def test_probe_playwright_shape():
    p = ot.probe_playwright()
    assert 'version' in p and 'importable' in p and 'chromium' in p


def test_safe_unpack_skips_traversal_and_symlinks(tmp_path):
    import io, tarfile, optional_tools
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        good = tarfile.TarInfo("aria2c"); data = b"#!/bin/sh\n"; good.size = len(data)
        tf.addfile(good, io.BytesIO(data))
        eviln = tarfile.TarInfo("../escape.txt"); ev = b"pwn"; eviln.size = len(ev)
        tf.addfile(eviln, io.BytesIO(ev))
        lnk = tarfile.TarInfo("link"); lnk.type = tarfile.SYMTYPE; lnk.linkname = "/etc/passwd"
        tf.addfile(lnk)
    dest = tmp_path / "out"; dest.mkdir()
    optional_tools._safe_unpack(
        buf.getvalue(),
        dest,
        archive_kind='tar.gz',
    )
    assert (dest / "aria2c").exists()                      # good regular file extracted
    assert not (tmp_path / "escape.txt").exists()          # traversal member skipped
    assert not (dest / "link").exists()                    # symlink skipped


def test_safe_unpack_zip_skips_symlink_entries(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('node.exe', b'certified-node')
        symlink = zipfile.ZipInfo('link')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(symlink, '../outside')
    destination = tmp_path / 'out'
    destination.mkdir()

    ot._safe_unpack(
        stream.getvalue(),
        destination,
        archive_kind='zip',
    )

    assert (destination / 'node.exe').read_bytes() == b'certified-node'
    assert not (destination / 'link').exists()
