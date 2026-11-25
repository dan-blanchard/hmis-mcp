"""Statistical disclosure control.

Every number that leaves the process goes through `DisclosureControl.release`.
A cell is a set of clients plus some values computed over them. Three rules
decide whether a cell's values are released:

1. Primary suppression. A cell covering 1..k-1 distinct clients is withheld.
2. Differencing guard. A cell is withheld if its client set differs from an
   already-released set by 1..k-1 clients and one contains the other, because
   subtracting the two answers would describe that small group.
3. Complementary suppression. Within one result, along every row/column
   ("line"), suppressed cells must number at least two and together cover at
   least k clients. Otherwise the smallest released cell on the line is
   withheld too, so a marginal total can't be used to back out the gap.

The guard is pairwise and in-memory. It stops the easy subtraction attacks,
not every combination of many overlapping queries. See README for the threat
model.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

MIN_CELL_FLOOR = 5
DEFAULT_MIN_CELL = 11
HISTORY_LIMIT = 5000


@dataclass(frozen=True)
class Policy:
    min_cell_size: int = DEFAULT_MIN_CELL

    def __post_init__(self) -> None:
        if self.min_cell_size < MIN_CELL_FLOOR:
            raise ValueError(
                f"min_cell_size must be at least {MIN_CELL_FLOOR}; got {self.min_cell_size}"
            )


@dataclass
class Cell:
    key: tuple[Any, ...]  # one value per group-by dimension
    members: int  # bitset over client indexes
    values: dict[str, Any]
    suppressed: str | None = None  # reason, or None if released

    @property
    def size(self) -> int:
        return self.members.bit_count()


def bitset(indexes: Iterable[int]) -> int:
    buf = bytearray()
    for i in indexes:
        byte = i >> 3
        if byte >= len(buf):
            buf.extend(b"\0" * (byte + 1 - len(buf)))
        buf[byte] |= 1 << (i & 7)
    return int.from_bytes(buf, "little")


@dataclass
class DisclosureControl:
    policy: Policy = field(default_factory=Policy)
    _history: dict[int, None] = field(default_factory=dict, repr=False)

    def release(self, cells: list[Cell]) -> list[Cell]:
        """Mark cells suppressed in place and remember what was released."""
        k = self.policy.min_cell_size
        cells = [c for c in cells if c.size > 0]

        for c in cells:
            if c.size < k:
                c.suppressed = "small_cell"

        seen = list(self._history)
        for c in sorted(cells, key=lambda c: -c.size):
            if c.suppressed:
                continue
            if any(_differs_by_small_nested_set(c.members, t, k) for t in seen):
                c.suppressed = "differencing"
            else:
                seen.append(c.members)

        self._complementary(cells, k)

        for c in cells:
            if not c.suppressed:
                self._history.pop(c.members, None)
                self._history[c.members] = None
        while len(self._history) > HISTORY_LIMIT:
            self._history.pop(next(iter(self._history)))
        return cells

    @staticmethod
    def _complementary(cells: list[Cell], k: int) -> None:
        if not cells:
            return
        ndim = len(cells[0].key)
        changed = True
        while changed:
            changed = False
            for axis in range(max(ndim, 1)):
                lines: dict[tuple, list[Cell]] = defaultdict(list)
                for c in cells:
                    lines[c.key[:axis] + c.key[axis + 1:]].append(c)
                for line in lines.values():
                    hidden = [c for c in line if c.suppressed]
                    if not hidden:
                        continue
                    union = 0
                    for c in hidden:
                        union |= c.members
                    if len(hidden) >= 2 and union.bit_count() >= k:
                        continue
                    shown = [c for c in line if not c.suppressed]
                    if shown:
                        min(shown, key=lambda c: c.size).suppressed = "complementary"
                        changed = True


def _differs_by_small_nested_set(a: int, b: int, k: int) -> bool:
    if a == b:
        return False
    only_a = (a & ~b).bit_count()
    only_b = (b & ~a).bit_count()
    if only_a and only_b:
        return False  # not nested; a difference of counts describes no one group
    return 0 < only_a + only_b < k
