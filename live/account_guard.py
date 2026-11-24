"""Paper account check for the runners.

The keeper checks the account prefix at login, but a runner takes its account id from the
session file. With `--live` the runner checks the same prefix itself before it can send
an order. A dry run sends no orders, so it is not checked.
"""

from __future__ import annotations

from core.config import load_keeper_config


def require_paper_account_for_live(
    account_id: object, *, dry_run: bool, prefix: str | None = None
) -> None:
    """Raise RuntimeError when `--live` is set and the account is not a paper account.

    `prefix` defaults to `keeper.paper_account_prefix` (DU). A missing account id is
    refused too. The error text masks the account id."""
    if dry_run:
        return
    wanted = prefix if prefix is not None else load_keeper_config().paper_account_prefix
    if isinstance(account_id, str) and account_id.startswith(wanted):
        return
    if isinstance(account_id, str) and account_id:
        shown = "***" + account_id[-2:]
    else:
        shown = "none"
    raise RuntimeError(
        f"--live refused: the session account ({shown}) does not start with the paper "
        f"prefix {wanted!r}. Orders are sent only to a paper account. Run without --live, "
        "or log in to a paper account."
    )
