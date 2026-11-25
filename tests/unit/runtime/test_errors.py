from aksha.runtime.errors import AkshaError


def test_error_carries_a_stable_code_and_a_message() -> None:
    e = AkshaError("owner_required", "every read names an owner")
    assert e.code == "owner_required"
    assert str(e) == "owner_required: every read names an owner"


def test_subclasses_set_a_default_code() -> None:
    class Refused(AkshaError):
        default_code = "refused"

    assert Refused(message="no").code == "refused"
    assert Refused("other", "no").code == "other"
