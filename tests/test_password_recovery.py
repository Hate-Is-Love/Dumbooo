"""Forgot password: recovery codes and the on-PC reset tool."""

import pytest

from services.auth_service import (AccountLocked, InvalidCredentials, NotAuthenticated, generate_recovery_code,
                                   normalize_recovery_code)
from tests.test_application import OWNER, make_core


@pytest.fixture
def core(paths):
    c = make_core(paths)
    yield c
    c.shutdown()


def test_recovery_code_format():
    code = generate_recovery_code()
    assert len(code) == 19 and code.count("-") == 3
    assert not set(code.replace("-", "")) & set("01OI")
    assert normalize_recovery_code(code.lower().replace("-", " ")) == code


def test_a_recovery_code_resets_the_password_once(core):
    session = core.accounts.session
    assert not core.auth.has_recovery_code(session)
    with pytest.raises(InvalidCredentials):
        core.auth.create_recovery_code(session, "wrong password")
    code = core.auth.create_recovery_code(session, OWNER[2])
    assert core.auth.has_recovery_code(session)

    new_code = core.auth.recover_with_code(OWNER[0], code.lower(), "Brand-new-pass-42")
    assert new_code != code
    with pytest.raises(NotAuthenticated):
        core.auth.require(session)                                   # every session was signed out
    core.auth.login(OWNER[0], "Brand-new-pass-42")
    with pytest.raises(InvalidCredentials):
        core.auth.login(OWNER[0], OWNER[2])                          # the old password is gone
    with pytest.raises(InvalidCredentials):
        core.auth.recover_with_code(OWNER[0], code, "Another-pass-43")   # the old code is used up
    core.auth.recover_with_code(OWNER[0], new_code, "Another-pass-43")
    actions = [r["action"] for r in core.db.query("SELECT action FROM admin_audit")]
    assert "recovery_code_created" in actions and actions.count("password_recovered") == 2


def test_guessing_recovery_codes_locks_the_account(core):
    session = core.accounts.session
    code = core.auth.create_recovery_code(session, OWNER[2])
    for _ in range(5):
        with pytest.raises(InvalidCredentials):
            core.auth.recover_with_code(OWNER[0], "AAAA-BBBB-CCCC-DDDD", "Brand-new-pass-42")
    with pytest.raises(AccountLocked):
        core.auth.recover_with_code(OWNER[0], code, "Brand-new-pass-42")


def test_no_code_no_recovery_and_unknown_users_look_the_same(core):
    with pytest.raises(InvalidCredentials):
        core.auth.recover_with_code(OWNER[0], "AAAA-BBBB-CCCC-DDDD", "Brand-new-pass-42")
    with pytest.raises(InvalidCredentials):
        core.auth.recover_with_code("nobody", "AAAA-BBBB-CCCC-DDDD", "Brand-new-pass-42")


def test_the_local_reset_tool_path(core):
    code = core.auth.reset_password_locally(OWNER[0].upper(), "Reset-on-this-pc-7")
    assert len(code) == 19
    core.auth.login(OWNER[0], "Reset-on-this-pc-7")
    assert core.db.scalar("SELECT COUNT(*) FROM admin_audit WHERE action = 'password_reset_local'") == 1
    with pytest.raises(InvalidCredentials):
        core.auth.reset_password_locally("nobody", "Reset-on-this-pc-7")
