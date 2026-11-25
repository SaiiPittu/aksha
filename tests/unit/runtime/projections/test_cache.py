from aksha.runtime.projections.cache import ProjectionCache


def test_cache_hits_only_the_exact_sequence() -> None:
    cache = ProjectionCache()
    cache.put("o1", "trail", (), 4, ("s4",))
    assert cache.get("o1", "trail", (), 4) == (True, ("s4",))
    assert cache.get("o1", "trail", (), 5) == (False, None)
    assert cache.get("o2", "trail", (), 4) == (False, None)
    assert cache.get("o1", "trail", ("x",), 4) == (False, None)


def test_a_none_state_is_still_a_hit() -> None:
    cache = ProjectionCache()
    cache.put("o1", "nothing", (), 4, None)
    assert cache.get("o1", "nothing", (), 4) == (True, None)


def test_a_newer_entry_makes_the_older_unreachable() -> None:
    cache = ProjectionCache()
    cache.put("o1", "trail", (), 4, "s4")
    cache.put("o1", "trail", (), 6, "s6")
    assert cache.get("o1", "trail", (), 4) == (False, None)
    assert cache.latest_before("o1", "trail", (), 9) == (6, "s6")
    assert cache.latest_before("o1", "trail", (), 6) is None
    cache.put("o1", "trail", (), 5, "s5")  # older than what is kept: ignored
    assert cache.get("o1", "trail", (), 6) == (True, "s6")


def test_another_owner_touches_nothing_and_clear_empties() -> None:
    cache = ProjectionCache()
    cache.put("o1", "trail", (), 4, "a")
    cache.put("o2", "trail", (), 9, "b")
    assert cache.get("o1", "trail", (), 4) == (True, "a")
    assert len(cache) == 2
    cache.clear()
    assert len(cache) == 0
