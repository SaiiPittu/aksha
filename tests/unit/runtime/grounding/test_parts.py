"""Record parts: a CSV row's part covers the whole logical row, quoted line breaks included."""

from __future__ import annotations

from aksha.runtime.grounding.parts import part_text, part_version

TABLE = 'id,note\nA,"first line\nsecond line"\nB,plain\n'


def test_a_row_with_a_quoted_line_break_is_one_row_of_the_part() -> None:
    assert part_text(TABLE, "where:id=A") == 'id,note\nA,"first line\nsecond line"\n'
    assert part_text(TABLE, "where:id=B") == "id,note\nB,plain\n"


def test_editing_only_a_continuation_line_moves_the_rows_part_and_no_other() -> None:
    edited = TABLE.replace("second line", "second line, edited")
    a, b = "where:id=A", "where:id=B"
    assert part_version(edited, a, "w") != part_version(TABLE, a, "w")
    assert part_version(edited, b, "w") == part_version(TABLE, b, "w")


def test_an_escaped_quote_does_not_end_or_extend_a_row() -> None:
    text = 'id,note\nA,"say ""hi""\nthere"\nB,x\n'
    assert part_text(text, "where:id=A") == 'id,note\nA,"say ""hi""\nthere"\n'
    assert part_text(text, "where:id=B") == "id,note\nB,x\n"
