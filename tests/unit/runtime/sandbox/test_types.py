from aksha.runtime.sandbox.types import EnvironmentSpec, SandboxBackendSpec


def test_sandbox_declarations_are_plain_values() -> None:
    env = EnvironmentSpec(
        "py",
        base="python@sha256:x",
        lock_inputs=("uv.lock",),
        build_argv=("uv", "sync"),
        egress="pypi",
    )
    assert env.lock_inputs == ("uv.lock",)
    assert SandboxBackendSpec("refusing", factory=object).name == "refusing"


def test_sandbox_needs_default_to_an_os_jail_with_the_network_off() -> None:
    from aksha.runtime.sandbox.types import SandboxNeeds, SandboxPolicy

    needs = SandboxNeeds()
    assert (needs.min_strength, needs.policy.network) == ("os-jail", "none")
    assert needs.policy == SandboxPolicy() and needs.policy.protected == ()
    assert (needs.tree_input, needs.tree_writable, needs.environment) == (None, False, None)


def test_availability_and_strength_order() -> None:
    from aksha.runtime.sandbox.types import STRENGTHS, Availability, strong_enough

    assert STRENGTHS == ("none", "os-jail", "container", "vm")
    probe = Availability("bwrap", "os-jail", available=True)
    assert probe.reason == ""
    assert strong_enough("container", "os-jail") and strong_enough("os-jail", "os-jail")
    assert not strong_enough("none", "os-jail")
