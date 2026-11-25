import pytest
from jsonschema import Draft202012Validator

from aksha.runtime.tools.arguments import is_json_pointer, pointer_of, schema_refusal
from aksha.runtime.tools.types import Err

SCHEMA = {
    "type": "object",
    "properties": {
        "count": {"type": "integer", "minimum": 1},
        "tags": {"type": "array", "items": {"type": "string"}},
        "url": {"type": "string"},
    },
    "required": ["count", "url"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("text", "ok"),
    [("", True), ("/a", True), ("/a/0", True), ("/a~0b~1c", True), ("a", False), ("/a~2", False)],
)
def test_is_json_pointer(text: str, ok: bool) -> None:
    assert is_json_pointer(text) is ok


def test_pointer_of_escapes_each_part() -> None:
    assert pointer_of(["a", 0, "b/c", "d~e"]) == "/a/0/b~1c/d~0e"
    assert pointer_of([]) == ""


def test_schema_error_lists_paths() -> None:
    args = {"count": 0, "tags": ["ok", 3], "extra": True}
    err = schema_refusal(Draft202012Validator(SCHEMA), args)
    assert isinstance(err, Err) and err.code == "schema"
    assert err.paths == ("/count", "/extra", "/tags/1", "/url")
    for path in err.paths:
        assert path in err.message


def test_valid_arguments_give_no_refusal() -> None:
    assert schema_refusal(Draft202012Validator(SCHEMA), {"count": 2, "url": "u"}) is None
