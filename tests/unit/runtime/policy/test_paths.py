import pytest

from aksha.runtime.policy.paths import is_under, normalised_path, root_problem


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/app.py", "src/app.py"),
        ("./src//app.py", "src/app.py"),
        ("src/../.git/hooks", ".git/hooks"),
        ("../outside", None),
        ("/etc/passwd", None),
        ("", None),
        ("a\\b", None),
    ],
)
def test_normalised_path_refuses_escapes(raw: str, expected: str | None) -> None:
    assert normalised_path(raw) == expected


@pytest.mark.parametrize(
    ("root", "problem"),
    [
        (".git", None),
        ("src/app", None),
        (".", None),
        (".git/", "not in normal form; write '.git'"),
        ("./.git", "not in normal form; write '.git'"),
        ("/etc", "absolute or climbs out"),
        ("../x", "absolute or climbs out"),
        (3, "a path root is a string"),
    ],
)
def test_root_problem_names_what_is_wrong(root: object, problem: str | None) -> None:
    found = root_problem(root)
    assert (found is None) if problem is None else (found is not None and problem in found)


@pytest.mark.parametrize(
    ("path", "root", "fold_case", "under"),
    [
        ("src/a.py", "src", False, True),
        ("src", "src/", False, True),
        ("srcx/a.py", "src", False, False),
        ("SRC/a.py", "src", False, False),
        (".GIT/config", ".git", True, True),
        ("anything", ".", False, True),
        ("x", "/abs", True, False),
    ],
)
def test_is_under(path: str, root: str, fold_case: bool, under: bool) -> None:
    assert is_under(path, root, fold_case=fold_case) is under
