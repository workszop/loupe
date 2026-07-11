import os
import signal
import subprocess

import src.lifecycle as lifecycle


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
