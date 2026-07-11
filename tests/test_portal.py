import os
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import portal


# --- sender-token mangling ---

def test_mangle_sender_strips_colon_and_replaces_dots():
    assert portal.mangle_sender(":1.42") == "1_42"


def test_mangle_sender_multiple_dots():
    assert portal.mangle_sender(":1.2.3") == "1_2_3"


# --- request-path construction ---

def test_request_path_format():
    path = portal.request_path("1_42", "loupe7")
    assert path == "/org/freedesktop/portal/desktop/request/1_42/loupe7"


# --- token persistence ---

def test_save_then_load_round_trip(tmp_path):
    token_path = str(tmp_path / "state" / "restore_token")
    portal.save_token(token_path, "abc123")
    assert portal.load_token(token_path) == "abc123"


def test_save_creates_file_mode_0600(tmp_path):
    token_path = str(tmp_path / "state" / "restore_token")
    portal.save_token(token_path, "abc123")
    mode = stat.S_IMODE(os.stat(token_path).st_mode)
    assert mode == 0o600


def test_load_returns_none_when_missing(tmp_path):
    token_path = str(tmp_path / "state" / "restore_token")
    assert portal.load_token(token_path) is None


def test_delete_removes_file(tmp_path):
    token_path = str(tmp_path / "state" / "restore_token")
    portal.save_token(token_path, "abc123")
    portal.delete_token(token_path)
    assert portal.load_token(token_path) is None


def test_delete_is_idempotent_when_missing(tmp_path):
    token_path = str(tmp_path / "state" / "restore_token")
    portal.delete_token(token_path)  # should not raise


# --- SelectSources options dict ---

def test_select_sources_options_token_omitted_when_none():
    opts = portal.build_select_sources_options(None)
    assert "restore_token" not in opts
    assert opts["types"].unpack() == 1
    assert opts["multiple"].unpack() is False
    assert opts["cursor_mode"].unpack() == 1
    assert opts["persist_mode"].unpack() == 2


def test_select_sources_options_token_present_when_saved():
    opts = portal.build_select_sources_options("saved-token")
    assert "restore_token" in opts
    assert opts["restore_token"].unpack() == "saved-token"


# --- stale-token retry decision ---

def test_should_retry_select_sources_code2_token_first_time():
    assert portal.should_retry_stale_token("SelectSources", 2, True, False) is True


def test_should_not_retry_select_sources_already_retried():
    assert portal.should_retry_stale_token("SelectSources", 2, True, True) is False


def test_should_not_retry_create_session_code2_token():
    assert portal.should_retry_stale_token("CreateSession", 2, True, False) is False


def test_should_not_retry_start_code2_token():
    assert portal.should_retry_stale_token("Start", 2, True, False) is False


def test_should_not_retry_select_sources_no_token():
    assert portal.should_retry_stale_token("SelectSources", 2, False, False) is False


def test_should_not_retry_dbus_exception_marker():
    # D-Bus transport exceptions use code -1 as their sentinel and must
    # never trigger a retry, regardless of step or token presence.
    assert portal.should_retry_stale_token("SelectSources", -1, True, False) is False


def test_should_not_retry_cancel_code1():
    assert portal.should_retry_stale_token("SelectSources", 1, True, False) is False
