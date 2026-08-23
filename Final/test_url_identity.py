import importlib
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

try:
    import Final.url_identity as url_identity_module
    from Final.url_identity import (
        dedupe_download_urls,
        download_identity,
        output_identity_lock,
        youtube_video_id,
    )
except ImportError:
    import url_identity as url_identity_module
    from url_identity import (
        dedupe_download_urls,
        download_identity,
        output_identity_lock,
        youtube_video_id,
    )


VIDEO_ID = "dQw4w9WgXcQ"
OTHER_ID = "aB3dE5gH7_k"


@pytest.mark.parametrize(
    "url",
    [
        f"https://youtu.be/{VIDEO_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://m.youtube.com/shorts/{VIDEO_ID}",
        f"https://music.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
        f"https://youtube.com/live/{VIDEO_ID}",
        f"youtu.be/{VIDEO_ID}",
    ],
)
def test_single_video_aliases_share_identity(url):
    assert youtube_video_id(url) == VIDEO_ID
    assert download_identity(url) == ("youtube", VIDEO_ID)


@pytest.mark.parametrize(
    "url",
    [
        f"https://youtube.com.evil.test/watch?v={VIDEO_ID}",
        f"https://evil-youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com@evil.test/watch?v={VIDEO_ID}",
        f"https://youtu.be.evil.test/{VIDEO_ID}",
        f"https://www.youtube-nocookie.com/watch?v={VIDEO_ID}",
        f"https://example.test/watch?v={VIDEO_ID}",
    ],
)
def test_spoofed_or_unknown_hosts_never_yield_a_video_id(url):
    assert youtube_video_id(url) is None
    assert download_identity(url)[0] == "url"


@pytest.mark.parametrize(
    "spoof",
    [
        f"https://evil.test\\@youtube.com/watch?v={VIDEO_ID}",
        f"https://user:secret@youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com:444/watch?v={VIDEO_ID}",
        f"http://youtube.com:443/watch?v={VIDEO_ID}",
    ],
)
def test_ambiguous_authority_cannot_suppress_a_later_genuine_alias(spoof):
    genuine = f"https://www.youtube.com/watch?v={VIDEO_ID}"

    assert youtube_video_id(spoof) is None
    assert download_identity(spoof)[0] == "url"
    assert dedupe_download_urls([spoof, genuine]) == [spoof, genuine]


@pytest.mark.parametrize(
    "url",
    [
        f"https://youtube.com:443/watch?v={VIDEO_ID}",
        f"http://youtube.com:80/watch?v={VIDEO_ID}",
        f"youtube.com:443/watch?v={VIDEO_ID}",
    ],
)
def test_default_and_schemeless_https_ports_share_video_identity(url):
    assert youtube_video_id(url) == VIDEO_ID
    assert download_identity(url) == ("youtube", VIDEO_ID)


def test_schemeless_default_https_port_aliases_dedupe_before_scheduling():
    schemeless = f"youtube.com:443/watch?v={VIDEO_ID}"
    explicit = f"https://www.youtube.com/watch?v={VIDEO_ID}"

    assert dedupe_download_urls([schemeless, explicit]) == [schemeless]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/",
        "https://www.youtube.com/feed/subscriptions",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/watch?v=too-short",
        f"https://www.youtube.com/watch?v={VIDEO_ID}x",
        "https://www.youtube.com/watch?v=dQw4w9WgXc!",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&v={OTHER_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}/extra",
        f"https://www.youtube.com/shorts/{VIDEO_ID}/extra",
        f"https://www.youtube.com/shorts/{VIDEO_ID}?v={OTHER_ID}",
        f"https://www.youtube.com/watch/{VIDEO_ID}",
        f"https://www.youtube.com/embed/",
    ],
)
def test_malformed_ambiguous_and_non_video_pages_do_not_collapse(url):
    assert youtube_video_id(url) is None
    assert download_identity(url)[0] == "url"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list=",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list",
        f"https://www.youtube.com/watch?list=PL123&v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?list=PL123&list=PL456",
    ],
)
def test_any_list_query_key_preserves_full_url_identity(url):
    assert youtube_video_id(url) is None
    kind, value = download_identity(url)
    assert kind == "url"
    assert value.startswith("https://")
    assert "list" in value


def test_full_url_identity_normalizes_transport_without_reordering_query():
    first = " HTTPS://Example.COM:443/a/../Video?b=2&a=1&a=0#ignored "
    second = "https://example.com/a/../Video?b=2&a=1&a=0"
    reordered = "https://example.com/a/../Video?a=1&b=2&a=0"

    assert download_identity(first) == (
        "url",
        "https://example.com/a/../Video?b=2&a=1&a=0",
    )
    assert download_identity(first) == download_identity(second)
    assert download_identity(first) != download_identity(reordered)


def test_ipv6_address_is_canonicalized_without_case_folding_scope_identifier():
    upper_scope = "HTTPS://[FE80::A%25ETH0]:443/video"
    lower_scope = "https://[fe80::a%25eth0]/video"

    assert download_identity(upper_scope) == (
        "url",
        "https://[fe80::a%25ETH0]/video",
    )
    assert download_identity(upper_scope) != download_identity(lower_scope)


def test_bracketed_ipvfuture_cannot_suppress_the_distinct_dns_authority():
    bracketed = "HTTPS://[v1.ABC]/video"
    dns = "https://v1.abc/video"

    assert download_identity(bracketed) == (
        "url",
        "https://[v1.abc]/video",
    )
    assert download_identity(bracketed) != download_identity(dns)
    assert dedupe_download_urls([bracketed, dns]) == [bracketed, dns]


def test_dedupe_preserves_first_occurrence_order_and_original_spelling():
    first_alias = f"https://youtu.be/{VIDEO_ID}"
    later_alias = f"https://www.youtube.com/watch?v={VIDEO_ID}&utm_source=test"
    playlist = f"https://www.youtube.com/watch?v={VIDEO_ID}&list="
    urls = [
        first_alias,
        "HTTPS://Example.COM:443/video#one",
        later_alias,
        "https://example.com/video",
        playlist,
        playlist,
        f"https://youtu.be/{OTHER_ID}",
    ]

    assert dedupe_download_urls(urls) == [
        first_alias,
        "HTTPS://Example.COM:443/video#one",
        playlist,
        f"https://youtu.be/{OTHER_ID}",
    ]


def test_dedupe_accepts_one_shot_generators():
    values = (
        url
        for url in [
            f"https://youtu.be/{VIDEO_ID}",
            f"https://www.youtube.com/watch?v={VIDEO_ID}",
            f"https://youtu.be/{OTHER_ID}",
        ]
    )
    assert dedupe_download_urls(values) == [
        f"https://youtu.be/{VIDEO_ID}",
        f"https://youtu.be/{OTHER_ID}",
    ]


def _wait_for_reference_count(expected: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with url_identity_module._OUTPUT_LOCKS_GUARD:
            refs = [entry.references for entry in url_identity_module._OUTPUT_LOCKS.values()]
        if refs == [expected]:
            return
        threading.Event().wait(0.005)
    pytest.fail(f"output lock reference counts never reached {[expected]} (last={refs})")


def test_same_output_identity_tracks_active_and_waiter_then_cleans_registry(tmp_path):
    active_entered = threading.Event()
    release_active = threading.Event()
    waiter_started = threading.Event()
    waiter_entered = threading.Event()
    errors = []

    def active():
        try:
            with output_identity_lock(VIDEO_ID, tmp_path):
                active_entered.set()
                assert release_active.wait(3)
        except BaseException as exc:
            errors.append(exc)

    def waiter():
        try:
            assert active_entered.wait(3)
            waiter_started.set()
            with output_identity_lock(VIDEO_ID, tmp_path / "."):
                waiter_entered.set()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=active)
    second = threading.Thread(target=waiter)
    first.start()
    assert active_entered.wait(3)
    second.start()
    assert waiter_started.wait(3)
    _wait_for_reference_count(2)
    assert not waiter_entered.is_set(), "same ID/output waiter was not serialized"

    release_active.set()
    assert waiter_entered.wait(3)
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    with url_identity_module._OUTPUT_LOCKS_GUARD:
        assert url_identity_module._OUTPUT_LOCKS == {}


def test_distinct_ids_and_directories_do_not_share_a_global_lock(tmp_path):
    release_first = threading.Event()
    first_entered = threading.Event()
    other_id_entered = threading.Event()
    other_dir_entered = threading.Event()
    errors = []

    def hold_first():
        try:
            with output_identity_lock(VIDEO_ID, tmp_path / "one"):
                first_entered.set()
                assert release_first.wait(3)
        except BaseException as exc:
            errors.append(exc)

    def enter_other_id():
        try:
            assert first_entered.wait(3)
            with output_identity_lock(OTHER_ID, tmp_path / "one"):
                other_id_entered.set()
        except BaseException as exc:
            errors.append(exc)

    def enter_other_directory():
        try:
            assert first_entered.wait(3)
            with output_identity_lock(VIDEO_ID, tmp_path / "two"):
                other_dir_entered.set()
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=hold_first),
        threading.Thread(target=enter_other_id),
        threading.Thread(target=enter_other_directory),
    ]
    for thread in threads:
        thread.start()
    assert first_entered.wait(3)
    assert other_id_entered.wait(3), "distinct video ID was globally serialized"
    assert other_dir_entered.wait(3), "distinct output directory was globally serialized"
    release_first.set()
    for thread in threads:
        thread.join(3)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    with url_identity_module._OUTPUT_LOCKS_GUARD:
        assert url_identity_module._OUTPUT_LOCKS == {}


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_output_lock_releases_and_cleans_after_exception_or_cancel(tmp_path, error_type):
    with pytest.raises(error_type):
        with output_identity_lock(VIDEO_ID, tmp_path):
            raise error_type("stop")

    entered = threading.Event()

    def reacquire():
        with output_identity_lock(VIDEO_ID, tmp_path):
            entered.set()

    thread = threading.Thread(target=reacquire)
    thread.start()
    assert entered.wait(3), "lock remained held after exceptional exit"
    thread.join(3)
    assert not thread.is_alive()
    with url_identity_module._OUTPUT_LOCKS_GUARD:
        assert url_identity_module._OUTPUT_LOCKS == {}


def test_missing_video_id_is_a_noop_without_registry_entry(tmp_path):
    with output_identity_lock(None, tmp_path):
        with output_identity_lock("", tmp_path):
            pass
    with url_identity_module._OUTPUT_LOCKS_GUARD:
        assert url_identity_module._OUTPUT_LOCKS == {}


def test_package_and_direct_import_modes_share_one_process_registry():
    package_module = importlib.import_module("Final.url_identity")
    direct_module = importlib.import_module("url_identity")
    assert package_module is direct_module
    assert package_module._OUTPUT_LOCKS is direct_module._OUTPUT_LOCKS


@pytest.mark.parametrize(
    "order",
    [
        ("Final.url_identity", "url_identity"),
        ("url_identity", "Final.url_identity"),
    ],
)
def test_simultaneous_flat_and_package_imports_share_registry_and_lock(order):
    """Per-name import locks must not permit two process-wide output registries."""
    project_root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        r"""
        import builtins
        import importlib
        import sys
        import tempfile
        import threading
        from pathlib import Path

        root = Path(sys.argv[1])
        sys.path.insert(0, str(root))
        sys.path.insert(0, str(root / "Final"))
        names = tuple(sys.argv[2:4])
        gate = threading.Barrier(2)
        original_import = builtins.__import__

        def gated_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "re" and (globals or {}).get("__name__") in {
                "url_identity", "Final.url_identity"
            }:
                gate.wait(5)
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = gated_import
        modules = {}
        errors = []

        def load(name):
            try:
                modules[name] = importlib.import_module(name)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=load, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        builtins.__import__ = original_import

        assert all(not thread.is_alive() for thread in threads), "simultaneous import hung"
        assert not errors, errors
        package = modules["Final.url_identity"]
        direct = modules["url_identity"]
        assert package._OUTPUT_LOCK_STATE is direct._OUTPUT_LOCK_STATE
        assert package._OUTPUT_LOCKS is direct._OUTPUT_LOCKS

        with tempfile.TemporaryDirectory() as directory:
            entered = threading.Event()
            def wait_on_direct_registry():
                with direct.output_identity_lock("dQw4w9WgXcQ", Path(directory)):
                    entered.set()

            with package.output_identity_lock("dQw4w9WgXcQ", Path(directory)):
                waiter = threading.Thread(target=wait_on_direct_registry)
                waiter.start()
                assert not entered.wait(0.25), "same key entered through the second registry"
            assert entered.wait(2), "waiter did not enter after release"
            waiter.join(2)
            assert not waiter.is_alive()
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script, str(project_root), *order],
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork (runs on Linux/WSL)")
def test_fork_child_resets_inherited_output_lock_registry(tmp_path):
    """A child must not inherit a permanently locked copy of a parent-owned key."""
    import select
    import signal

    read_fd, write_fd = os.pipe()
    child_pid = None
    try:
        with output_identity_lock(VIDEO_ID, tmp_path):
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    with output_identity_lock(VIDEO_ID, tmp_path):
                        os.write(write_fd, b"acquired")
                    os._exit(0)
                except BaseException as exc:
                    os.write(write_fd, f"error:{type(exc).__name__}:{exc}".encode())
                    os._exit(1)

            os.close(write_fd)
            write_fd = -1
            ready, _, _ = select.select([read_fd], [], [], 3)
            if not ready:
                os.kill(child_pid, signal.SIGKILL)
                os.waitpid(child_pid, 0)
                child_pid = None
                pytest.fail("forked child blocked on the parent's inherited output lock")
            message = os.read(read_fd, 256)

        waited_pid, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert waited_pid > 0
        assert os.waitstatus_to_exitcode(status) == 0
        assert message == b"acquired"
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
