"""app/verification.py — одноразовые коды подтверждения (общая механика
для регистрации по телефону и восстановления пароля)."""
import datetime as dt

from app import verification
from app.models import VerificationCode, VerificationCodePurpose


def test_issue_and_consume_correct_code(db):
    code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567")
    db.commit()

    ok, payload = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", code)
    assert ok is True
    assert payload is None


def test_consume_wrong_code_fails_and_increments_attempts(db):
    verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567")
    db.commit()

    ok, _ = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", "000000")
    assert ok is False

    row = db.query(VerificationCode).filter_by(target="9991234567").one()
    assert row.attempts == 1
    assert row.consumed_at is None


def test_consume_code_wrong_purpose_fails(db):
    code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567")
    db.commit()
    ok, _ = verification.consume_code(VerificationCodePurpose.PASSWORD_RESET, "9991234567", code)
    assert ok is False


def test_consume_code_wrong_target_fails(db):
    code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567")
    db.commit()
    ok, _ = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9997654321", code)
    assert ok is False


def test_code_cannot_be_consumed_twice(db):
    code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567")
    db.commit()
    ok1, _ = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", code)
    db.commit()
    ok2, _ = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", code)
    assert ok1 is True
    assert ok2 is False


def test_expired_code_fails(db):
    code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567")
    db.commit()
    row = db.query(VerificationCode).filter_by(target="9991234567").one()
    row.expires_at = dt.datetime.utcnow() - dt.timedelta(minutes=1)
    db.commit()

    ok, _ = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", code)
    assert ok is False


def test_reissuing_code_invalidates_previous_one(db):
    old_code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567")
    db.commit()
    new_code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567")
    db.commit()

    assert db.query(VerificationCode).filter_by(target="9991234567", consumed_at=None).count() == 1

    ok_old, _ = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", old_code)
    assert ok_old is False
    ok_new, _ = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", new_code)
    assert ok_new is True


def test_too_many_attempts_invalidates_code_even_with_correct_value(db):
    code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567")
    db.commit()
    for _ in range(verification.MAX_ATTEMPTS):
        ok, _ = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", "000000")
        assert ok is False
    db.commit()

    ok, _ = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", code)
    assert ok is False


def test_payload_is_returned_on_success(db):
    code = verification.issue_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", payload="hashed-secret")
    db.commit()
    ok, payload = verification.consume_code(VerificationCodePurpose.PHONE_REGISTER, "9991234567", code)
    assert ok is True
    assert payload == "hashed-secret"
