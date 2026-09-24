"""The identifiers the server mints, and the one thing they must never do."""

from tindarr.storage.ids import new_id


def test_an_identifier_never_starts_like_a_command_line_option() -> None:
    """A leading ``-`` made ``--user <id>`` unparseable for one id in sixty-four.

    CI caught it as a flake; a real administrator would have caught it as "this account
    cannot be imported". Ten thousand draws put the odds of missing a regression here
    below one in a very large number.
    """
    assert all(not new_id().startswith(("-", "_")) for _ in range(10_000))


def test_identifiers_keep_their_length_and_do_not_repeat() -> None:
    """Rejecting a draw must not shorten the value or narrow the alphabet."""
    minted = {new_id() for _ in range(1_000)}
    assert len(minted) == 1_000
    assert {len(value) for value in minted} == {22}
