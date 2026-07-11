import os
import signal
import subprocess
import sys
import time

import pytest

import src.lifecycle as lifecycle


@pytest.fixture
def pidfile(tmp_path, monkeypatch):
    path = str(tmp_path / "loupe.pid")
    monkeypatch.setattr(lifecycle, "PIDFILE", path)
    return path


@pytest.fixture
def spawned():
    """Track subprocess.Popen objects and guarantee cleanup even on failure."""
    procs = []

    def spawn(*args, **kwargs):
        p = subprocess.Popen(*args, **kwargs)
        procs.append(p)
        return p

    yield spawn

    for p in procs:
        if p.poll() is None:
            p.kill()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def test_acquire_fresh_no_pidfile(pidfile):
    assert not os.path.exists(pidfile)

    result = lifecycle.acquire_pidfile_or_toggle()

    assert result is True
    with open(pidfile) as f:
        assert f.read().strip() == str(os.getpid())


def test_acquire_stale_dead_pid(pidfile):
    dead_pid = 2**22 - 1
    # verify it really is unused before relying on it
    try:
        os.kill(dead_pid, 0)
        pytest.skip(f"pid {dead_pid} unexpectedly alive on this system")
    except ProcessLookupError:
        pass
    except PermissionError:
        pytest.skip(f"pid {dead_pid} unexpectedly alive (EPERM) on this system")

    with open(pidfile, "w") as f:
        f.write(f"{dead_pid}\n")

    result = lifecycle.acquire_pidfile_or_toggle()

    assert result is True
    with open(pidfile) as f:
        assert f.read().strip() == str(os.getpid())


def test_acquire_stale_recycled_pid(pidfile, spawned):
    sleeper = spawned(["sleep", "5"])
    time.sleep(0.2)  # let it actually start

    with open(pidfile, "w") as f:
        f.write(f"{sleeper.pid}\n")

    result = lifecycle.acquire_pidfile_or_toggle()

    assert result is True
    with open(pidfile) as f:
        assert f.read().strip() == str(os.getpid())
    assert sleeper.poll() is None  # sleeper untouched


def test_acquire_toggle_off_sends_sigterm(pidfile, spawned):
    dummy = spawned(
        [sys.executable, "-c", "import time; time.sleep(30)", "loupe-dummy"]
    )
    time.sleep(0.3)  # let it actually start and cmdline show up

    with open(pidfile, "w") as f:
        f.write(f"{dummy.pid}\n")

    result = lifecycle.acquire_pidfile_or_toggle()

    assert result is False
    returncode = dummy.wait(timeout=5)
    assert returncode == -15


def test_release_pidfile_own(pidfile):
    with open(pidfile, "w") as f:
        f.write(f"{os.getpid()}\n")

    lifecycle.release_pidfile()

    assert not os.path.exists(pidfile)


def test_release_pidfile_foreign(pidfile):
    with open(pidfile, "w") as f:
        f.write("1\n")

    lifecycle.release_pidfile()

    assert os.path.exists(pidfile)
    with open(pidfile) as f:
        assert f.read().strip() == "1"


def test_release_pidfile_idempotent_when_missing(pidfile):
    assert not os.path.exists(pidfile)

    lifecycle.release_pidfile()  # must not raise

    assert not os.path.exists(pidfile)


def test_fail_prints_message_and_hint(capsys):
    lifecycle.fail("something broke", hint="try again")

    captured = capsys.readouterr()
    assert captured.err == "loupe: something broke\ntry again\n"


def test_fail_no_hint(capsys):
    lifecycle.fail("something broke")

    captured = capsys.readouterr()
    assert captured.err == "loupe: something broke\n"


def test_fail_swallows_notify_send_errors(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise FileNotFoundError("no notify-send")

    monkeypatch.setattr(subprocess, "run", boom)

    lifecycle.fail("boom message", hint="a hint")  # must not raise

    captured = capsys.readouterr()
    assert "boom message" in captured.err


def test_install_signal_handlers_dispatches_sigterm():
    from gi.repository import GLib

    received = []
    loop = GLib.MainLoop()

    def cleanup():
        received.append(True)
        loop.quit()

    lifecycle.install_signal_handlers(cleanup)

    def send_signal():
        os.kill(os.getpid(), signal.SIGTERM)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(50, send_signal)
    GLib.timeout_add_seconds(5, loop.quit)  # safety net so test can't hang

    loop.run()

    assert received == [True]
