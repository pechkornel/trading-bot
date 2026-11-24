"""Account ids are masked in keeper events, output, alerts and CPGWError texts."""

from __future__ import annotations

import httpx
import pytest

from core.cpgw_client import CPGWClient, CPGWError, mask_account
from live import account_equity
from tests.test_cpgw_keeper import Env

ACCOUNT = "DU1234567"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def client_answering(handler) -> CPGWClient:
    client = CPGWClient("c=1", host="localhost")
    client._client = httpx.Client(
        base_url="https://localhost:5000", transport=httpx.MockTransport(handler)
    )
    return client


def test_public_alias_and_old_name_work():
    assert mask_account(f"x {ACCOUNT}", ACCOUNT) == "x ***67"
    assert account_equity.mask_account(f"x {ACCOUNT}", None) == "x ***67"
    assert account_equity._mask_account(f"x {ACCOUNT}", ACCOUNT) == "x ***67"


def test_http_error_text_masks_the_account_in_path_and_body():
    def handler(request):
        return httpx.Response(500, text=f"boom for {ACCOUNT}")

    client = client_answering(handler)
    client.account_id = ACCOUNT
    with pytest.raises(CPGWError) as info:
        client.account_summary()
    assert ACCOUNT not in str(info.value)
    assert "***67" in str(info.value)


def test_bootstrap_error_text_masks_the_account():
    def handler(request):
        if request.url.path.endswith("/iserver/accounts"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json=[{"desc": ACCOUNT}])

    client = client_answering(handler)
    with pytest.raises(CPGWError) as info:
        client.bootstrap()
    assert ACCOUNT not in str(info.value)


def test_keeper_events_stdout_and_alerts_never_carry_the_full_account(env, capsys):
    keeper = env.keeper()
    keeper.tick()  # logs in and prints the account
    env.client.authenticated = True
    keeper._session_verified = False
    keeper.tick()  # "existing session verified" path
    out = capsys.readouterr().out
    texts = [m for _, _, m in env.storage.events] + [n["text"] for n in env.notifier.sent]
    assert any("***67" in t for t in texts)
    assert ACCOUNT not in out
    assert all(ACCOUNT not in t for t in texts)


def test_session_file_keeps_the_full_account_for_the_runner(env):
    env.keeper().tick()
    assert ACCOUNT in env.session_file.read_text()
