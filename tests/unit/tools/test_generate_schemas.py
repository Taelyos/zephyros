"""The check that the published schemas follow the code."""

from __future__ import annotations

from pathlib import Path

from tools.generate_schemas import check, main, write

from zephyros.core.publication import published_schemas

REPOSITORY = Path(__file__).resolve().parents[3]


def test_the_committed_schemas_are_up_to_date() -> None:
    assert check(REPOSITORY) == []


def test_a_missing_file_is_found(tmp_path: Path) -> None:
    assert "missing" in check(tmp_path)[0]


def test_a_changed_document_is_found(tmp_path: Path) -> None:
    write(tmp_path)
    (tmp_path / "docs" / "tools.md").write_text("edited by hand", encoding="utf-8")
    assert any("docs/tools.md: differs" in finding for finding in check(tmp_path))
    (tmp_path / "docs" / "tools.md").unlink()
    assert any("docs/tools.md: missing" in finding for finding in check(tmp_path))


def test_a_changed_file_and_a_stray_file_are_found(tmp_path: Path) -> None:
    write(tmp_path)
    schema = tmp_path / "schemas" / "configuration.schema.json"
    schema.write_text(schema.read_text(encoding="utf-8") + " ", encoding="utf-8")
    (tmp_path / "schemas" / "stray.json").write_text("{}", encoding="utf-8")
    findings = check(tmp_path)
    assert any("differs" in finding for finding in findings)
    assert any("stray.json" in finding for finding in findings)


def test_write_then_check_is_clean(tmp_path: Path) -> None:
    written = write(tmp_path)
    assert [path.name for path in written] == [*published_schemas(), "tools.md"]
    assert check(tmp_path) == []


def test_main_writes_and_checks(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]  # reason: pytest fixtures
    monkeypatch.chdir(tmp_path)
    assert main(["--check"]) == 1
    assert main([]) == 0
    assert main(["--check"]) == 0
    assert main(["--bad"]) == 2
    assert "up to date" in capsys.readouterr().out
