"""The connect-time account guard.

Only the guard is covered here: the rest of IBClient needs a live Gateway.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from warrior_bot.broker.ib_client import IBClient




class _AccountIB:
    def __init__(self, accounts):
        self._accounts = accounts

    def managedAccounts(self):
        return list(self._accounts)


def _client_with(accounts, account=""):
    """An IBClient with no connection -- only the guard is under test."""
    client = IBClient.__new__(IBClient)
    client.config = SimpleNamespace(trading=SimpleNamespace(account=account))
    client.ib = _AccountIB(accounts)
    return client


def test_one_account_and_none_configured_is_fine():
    """Today's setup: IBKR assumes the only account."""
    _client_with(["DU111"])._verify_account()


def test_a_configured_account_the_login_manages_is_fine():
    _client_with(["DU111", "DU222"], account="DU111")._verify_account()


def test_several_accounts_with_none_configured_is_refused():
    """IBKR rejects every order in this state, and an unscoped position read
    feeds another bot's holdings into the reconciliation watchdog."""
    with pytest.raises(RuntimeError, match="manages 2 accounts"):
        _client_with(["DU111", "DU222"])._verify_account()


def test_a_configured_account_the_login_does_not_manage_is_refused():
    with pytest.raises(RuntimeError, match="is not managed by this login"):
        _client_with(["DU111"], account="DU999")._verify_account()


def test_no_managed_accounts_only_warns(caplog):
    """IB sometimes reports nothing here before it settles."""
    with caplog.at_level("WARNING"):
        _client_with([], account="DU111")._verify_account()
    assert "no managed accounts" in caplog.text
