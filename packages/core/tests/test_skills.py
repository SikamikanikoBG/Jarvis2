"""Which skills ride along with a request."""


def test_a_trigger_matches_as_a_phrase_of_its_own_not_inside_another_word():
    """2026-11-17: the trigger "x.com" fired inside "forex.com" in an FX brief, and an X-posting
    playbook rode along on every step of an hour-long run."""
    from jarvis_core.features.skills import _says

    assert not _says("brokers: OANDA v20 practice, forex.com, IBKR", "x.com")
    assert _says("draft it and post on x.com tonight", "x.com")
    assert _says("Compose post for LinkedIn", "compose post")
    assert not _says("between us", "tweet")
    assert _says("Напиши tweet за това", "tweet")
