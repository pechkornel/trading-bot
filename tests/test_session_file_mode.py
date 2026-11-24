"""The session file (it holds the cookie) is never world readable, even briefly."""

from __future__ import annotations

import json
import os
import stat

from live import cpgw_keeper


def test_temp_file_is_created_with_mode_600(tmp_path, monkeypatch):
    opened = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        opened.append((str(path), flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    old_umask = os.umask(0)  # a permissive umask must not widen the file
    try:
        monkeypatch.setattr(cpgw_keeper.os, "open", spy)
        target = tmp_path / "cpgw_session.json"
        cpgw_keeper._write_session("c=1", "DU1234567", target)
    finally:
        os.umask(old_umask)

    created = [o for o in opened if o[0].startswith(str(tmp_path))]
    assert created, "the temp file must be created with os.open"
    for _, flags, mode in created:
        assert flags & os.O_CREAT
        assert mode == 0o600
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert json.loads(target.read_text())["cookie"] == "c=1"


def test_stale_wide_temp_file_does_not_leak_its_mode(tmp_path):
    target = tmp_path / "cpgw_session.json"
    stale = tmp_path / "cpgw_session.json.tmp"
    stale.write_text("old")
    os.chmod(stale, 0o666)
    cpgw_keeper._write_session("c=2", None, target)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert not stale.exists()
