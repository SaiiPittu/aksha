from aksha.runtime.outbox.types import EffectSpec, SystemOfRecordSpec


def test_system_of_record_spec_names_a_receiver() -> None:
    assert SystemOfRecordSpec("git_remote", factory=object).name == "git_remote"


def test_effect_spec_names_system_key_fields_and_semantics() -> None:
    spec = EffectSpec(
        system="send", key_fields=("/document", "/recipient_id"), semantics="reconcile"
    )
    assert (spec.on_unknown, spec.record_fields, spec.receipt_kind) == ("tell_model", (), None)
    assert spec.key_fields == ("/document", "/recipient_id")
