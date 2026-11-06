import pytest

from barnraise.globs import outside_repo, overlap


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("src/api/**", "src/api/routes.ts"),
        ("src/**", "src/api/**"),
        ("src/api", "src/api/x/y.ts"),  # a directory owns its contents
        ("**", "anything/at/all.txt"),
        ("**/*.json", "package.json"),
        ("src/*.ts", "src/app.ts"),
        ("src/*.ts", "src/a*.ts"),
        ("tests/test_*.py", "tests/*_api.py"),
    ],
)
def test_overlapping(a: str, b: str) -> None:
    assert overlap(a, b) and overlap(b, a)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("src/api/**", "src/ui/**"),
        ("src/*.ts", "src/app.py"),
        ("src/api/**", "src/apiclient.ts"),
        ("package.json", "src/package.json"),
        ("tests/test_api.py", "tests/test_ui.py"),
        ("src/*.ts", "src/lib/a.ts"),
    ],
)
def test_disjoint(a: str, b: str) -> None:
    assert not overlap(a, b) and not overlap(b, a)


@pytest.mark.parametrize("glob", ["/etc/passwd", "~/x", "../sibling/**", "src/../../x", "C:/x", ""])
def test_outside_repo(glob: str) -> None:
    assert outside_repo(glob) is not None


def test_inside_repo() -> None:
    assert outside_repo("src/**") is None and outside_repo("./src/a.ts") is None
