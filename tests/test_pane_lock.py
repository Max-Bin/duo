"""Tests for pane_lock advisory locking."""

from __future__ import annotations

import os
import threading

import pytest

from duo.transport import (
    _FLOCK_OWNERS,
    _THREAD_LOCKS,
    _get_thread_lock,
    pane_lock,
)


@pytest.fixture(autouse=True)
def _clean_thread_locks():
    """Clean per-label thread locks between tests."""
    _THREAD_LOCKS.clear()
    _FLOCK_OWNERS.clear()
    yield
    _THREAD_LOCKS.clear()
    _FLOCK_OWNERS.clear()


@pytest.fixture()
def tmp_locks_dir(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Redirect _LOCKS_DIR to a temp directory."""
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr("duo.transport._LOCKS_DIR", lock_dir)
    return lock_dir


class TestGetThreadLock:
    """Tests for _get_thread_lock helper."""

    def test_returns_rlock(self):
        lock = _get_thread_lock("pane-a")
        assert isinstance(lock, type(threading.RLock()))

    def test_same_label_returns_same_lock(self):
        a1 = _get_thread_lock("pane-a")
        a2 = _get_thread_lock("pane-a")
        assert a1 is a2

    def test_different_labels_return_different_locks(self):
        a = _get_thread_lock("pane-a")
        b = _get_thread_lock("pane-b")
        assert a is not b


class TestPaneLock:
    """Tests for pane_lock context manager."""

    def test_basic_acquire_release(self, tmp_locks_dir):
        """Lock can be acquired and released."""
        with pane_lock("test-pane"):
            assert (tmp_locks_dir / "test-pane.lock").exists()
        # After exit, lock file still exists but flock is released.
        assert (tmp_locks_dir / "test-pane.lock").exists()

    def test_creates_lock_directory(self, tmp_locks_dir):
        """Lock directory is created if it doesn't exist."""
        assert not tmp_locks_dir.exists()
        with pane_lock("pane1"):
            assert tmp_locks_dir.is_dir()

    def test_reentrant_same_thread(self, tmp_locks_dir):
        """Same thread can acquire the lock twice (RLock)."""
        with pane_lock("pane1"):
            with pane_lock("pane1"):
                pass  # No deadlock

    def test_unsafe_label_rejected(self, tmp_locks_dir):
        """Labels with unsafe characters are rejected."""
        with pytest.raises(ValueError, match="Unsafe pane label"):
            with pane_lock("pane; rm -rf /"):
                pass

    def test_timeout_cross_thread(self, tmp_locks_dir):
        """Cross-thread lock contention raises TimeoutError."""
        barrier = threading.Barrier(2, timeout=5)
        holder_ready = threading.Event()
        errors: list[Exception] = []

        def hold_lock():
            try:
                with pane_lock("shared"):
                    holder_ready.set()
                    barrier.wait()  # Hold lock until main thread signals
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=hold_lock)
        t.start()
        holder_ready.wait(timeout=5)

        try:
            with pytest.raises(TimeoutError, match="in-process pane lock"):
                with pane_lock("shared", timeout=0.2):
                    pass
        finally:
            barrier.wait()  # Release the holder
            t.join(timeout=5)

        assert not errors

    def test_different_labels_independent(self, tmp_locks_dir):
        """Locks on different labels don't block each other."""
        with pane_lock("pane-a"):
            with pane_lock("pane-b"):
                pass  # No deadlock, different labels

    def test_lock_file_path(self, tmp_locks_dir):
        """Lock file is created at the expected path."""
        with pane_lock("my-pane"):
            expected = tmp_locks_dir / "my-pane.lock"
            assert expected.exists()

    def test_exception_releases_lock(self, tmp_locks_dir):
        """Lock is released even if an exception occurs inside."""
        with pytest.raises(RuntimeError, match="boom"):
            with pane_lock("pane1"):
                raise RuntimeError("boom")

        # Lock should be released — can re-acquire immediately
        with pane_lock("pane1", timeout=0.5):
            pass

    def test_cross_process_lock_file(self, tmp_locks_dir):
        """Lock file is usable for fcntl.flock."""
        import fcntl

        with pane_lock("proc-test"):
            lock_path = tmp_locks_dir / "proc-test.lock"
            fd = os.open(str(lock_path), os.O_RDONLY)
            try:
                # Should fail with EAGAIN since we hold the lock
                with pytest.raises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_cross_process_timeout(self, tmp_locks_dir):
        """Cross-process flock contention triggers TimeoutError."""
        import fcntl

        lock_path = tmp_locks_dir
        lock_path.mkdir(parents=True, exist_ok=True)
        file_path = lock_path / "xproc.lock"

        # Hold flock from "another process" (same process, different fd)
        fd = os.open(str(file_path), os.O_CREAT | os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with pytest.raises(TimeoutError, match="cross-process pane lock"):
                with pane_lock("xproc", timeout=0.3):
                    pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class TestDialogFunctionsUseLock:
    """Verify dialog functions acquire pane_lock."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        """Eliminate real sleeps for test speed."""
        monkeypatch.setattr("duo.transport._time.sleep", lambda _: None)

    def test_approve_permission_acquires_lock(self, monkeypatch, tmp_locks_dir):
        """approve_permission should acquire pane_lock."""
        lock_acquired = []

        original_pane_lock = pane_lock

        import contextlib
        from collections.abc import Generator

        @contextlib.contextmanager
        def tracking_lock(label: str, **kw) -> Generator[None, None, None]:
            lock_acquired.append(label)
            with original_pane_lock(label, **kw):
                yield

        monkeypatch.setattr("duo.transport.pane_lock", tracking_lock)
        monkeypatch.setattr("duo.transport.is_in_dialog_stable", lambda _label: True)
        monkeypatch.setattr(
            "duo.transport.read_pane",
            lambda _label, _lines: "╭─\n  1. Yes\n╰─\n",
        )
        monkeypatch.setattr("duo.transport.select_dialog_option", lambda _l, _o: None)

        from duo.transport import approve_permission

        approve_permission("test-pane")
        assert "test-pane" in lock_acquired

    def test_select_dialog_option_acquires_lock(self, monkeypatch, tmp_locks_dir):
        """select_dialog_option should acquire pane_lock."""
        lock_acquired = []

        original_pane_lock = pane_lock

        import contextlib
        from collections.abc import Generator

        @contextlib.contextmanager
        def tracking_lock(label: str, **kw) -> Generator[None, None, None]:
            lock_acquired.append(label)
            with original_pane_lock(label, **kw):
                yield

        monkeypatch.setattr("duo.transport.pane_lock", tracking_lock)
        monkeypatch.setattr("duo.transport.is_in_dialog_stable", lambda _label: True)
        monkeypatch.setattr("duo.transport.type_text", lambda _l, _t: None)
        monkeypatch.setattr("duo.transport.is_in_dialog", lambda _label: False)
        monkeypatch.setattr("duo.transport._record_pr", lambda _l, _k, _v: None)

        from duo.transport import select_dialog_option

        select_dialog_option("test-pane", "1")
        assert "test-pane" in lock_acquired
