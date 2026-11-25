import pytest

from hmis_mcp.privacy import Cell, DisclosureControl, Policy, bitset


def cell(key, members):
    return Cell(key=key, members=bitset(members), values={"clients": len(set(members))})


def by_key(cells):
    return {c.key: c.suppressed for c in cells}


def test_bitset_roundtrip():
    assert bitset([0, 3, 9]) == 0b1000001001
    assert bitset([]) == 0
    assert bitset([5, 5]).bit_count() == 1


def test_policy_floor():
    with pytest.raises(ValueError):
        Policy(min_cell_size=1)


def test_small_cells_suppressed_and_empty_dropped():
    dc = DisclosureControl(Policy(11))
    out = dc.release([cell(("a",), range(0, 50)), cell(("b",), range(50, 60)),
                      cell(("c",), range(60, 63)), cell(("d",), [])])
    got = by_key(out)
    assert ("d",) not in got
    assert got[("a",)] is None
    assert got[("b",)] == "small_cell" and got[("c",)] == "small_cell"


def test_single_small_cell_gets_a_complementary_partner():
    dc = DisclosureControl(Policy(11))
    out = dc.release([cell(("a",), range(0, 50)), cell(("b",), range(50, 70)),
                      cell(("c",), range(70, 73))])
    got = by_key(out)
    assert got[("c",)] == "small_cell"
    assert got[("b",)] == "complementary"  # smallest released cell on the line
    assert got[("a",)] is None


def test_complementary_runs_along_both_axes():
    dc = DisclosureControl(Policy(11))
    cells = [cell((r, c), range(base, base + n)) for (r, c, base, n) in [
        ("r1", "c1", 0, 3), ("r1", "c2", 100, 40), ("r1", "c3", 200, 60),
        ("r2", "c1", 300, 30), ("r2", "c2", 400, 50), ("r2", "c3", 500, 70),
    ]]
    got = by_key(dc.release(cells))
    for axis in (0, 1):
        lines = {}
        for key, reason in got.items():
            lines.setdefault(key[:axis] + key[axis + 1:], []).append(reason)
        for reasons in lines.values():
            hidden = [r for r in reasons if r]
            assert len(hidden) != 1, got


def test_differencing_against_earlier_release():
    dc = DisclosureControl(Policy(11))
    assert by_key(dc.release([cell((), range(0, 100))]))[()] is None
    # Same population minus three people: answering would describe those three.
    assert by_key(dc.release([cell((), range(3, 100))]))[()] == "differencing"
    # Minus twenty is fine, and so is an identical repeat.
    assert by_key(dc.release([cell((), range(20, 100))]))[()] is None
    assert by_key(dc.release([cell((), range(0, 100))]))[()] is None


def test_suppressed_cells_do_not_enter_history():
    dc = DisclosureControl(Policy(11))
    dc.release([cell((), range(0, 100))])
    dc.release([cell((), range(3, 100))])  # suppressed
    # Had the suppressed set been remembered, this one (2 away from it) would be blocked.
    assert by_key(dc.release([cell((), range(5, 120))]))[()] is None


def test_overlapping_non_nested_sets_are_released():
    dc = DisclosureControl(Policy(11))
    dc.release([cell((), range(0, 100))])
    assert by_key(dc.release([cell((), range(2, 102))]))[()] is None
