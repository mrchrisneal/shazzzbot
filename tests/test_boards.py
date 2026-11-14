import pytest

from shazzzbot import boards
from shazzzbot.boards import Board, child, message_state, pack, restore_ids


def sized(*sizes):
    import discord

    return [discord.Embed(title=str(i), description="x" * (size - len(str(i)))) for i, size in enumerate(sizes)]


def test_pack_balances_whole_sections_instead_of_stranding_a_tiny_tail():
    embeds = sized(3500, 2200, 2000, 100)
    groups = pack(embeds)
    assert [[embed.title for embed in group] for group in groups] == [["0"], ["1", "2", "3"]]
    assert [sum(map(len, group)) for group in groups] == [3500, 4300]
    assert [embed for group in groups for embed in group] == embeds


def test_pack_obeys_count_and_size_and_prefers_the_larger_first_group_on_a_tie():
    embeds = sized(*([100] * 11))
    groups = pack(embeds)
    assert [len(group) for group in groups] == [6, 5]
    assert all(len(group) <= boards.EMBED_COUNT and sum(map(len, group)) <= 6000 for group in groups)
    assert [len(group) for group in pack(sized(*([2400] * 6)))] == [2, 2, 2]
    assert [len(group) for group in pack(sized(3000, 3000))] == [2]
    assert pack([]) == []
    with pytest.raises(ValueError, match="section exceeds"):
        pack(sized(6001))


def test_an_oversized_trimmed_section_gets_its_own_message():
    embeds = sized(100, 100, 100)
    assert pack(embeds, alone={1}) == [[embeds[0]], [embeds[1]], [embeds[2]]]


@pytest.mark.parametrize("value,expected", [(42, [42]), ([42, 43], [42, 43]), ([42, 42], [42]),
                                          (None, []), ([], []), (True, []), (0, []), ([-1], []),
                                          (["42"], []), ({"20": 42}, [])])
def test_message_ids_restore_old_and_new_state_without_accepting_invalid_ids(value, expected):
    assert restore_ids(value) == expected


def test_state_keeps_every_active_and_pending_surplus_id_in_order():
    a, b = Board(20), Board(21)
    assert a.messages == [a]
    assert message_state([a, b]) == {}
    a.message_id = 1
    assert message_state([a]) == {"20": 1}
    second = child(a, 1)
    second.message_id = 2
    a.messages.append(second)
    extra = child(a, 2)
    extra.message_id = 3
    a.surplus = [extra]
    assert message_state([a, b]) == {"20": [1, 2, 3]}
    assert second.owner is a and second.position == 1 and second.channel_id == a.channel_id
