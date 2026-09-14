"""Public onboarding must execute against this checkout, not imaginary APIs."""
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def python_blocks(path):
    return re.findall(r"```python\n(.*?)```", path.read_text(), re.S)


def test_readme_quickstarts_match_and_replay_without_duplicate_cards(tmp_path):
    primary = python_blocks(ROOT / "README.md")
    assert primary == python_blocks(ROOT / "README.en.md")
    assert len(primary) == 1
    for _ in range(2):
        result = subprocess.run([sys.executable, "-c", primary[0]], cwd=tmp_path,
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert "Avoids spicy food" in result.stdout
    with sqlite3.connect(tmp_path / "garden.db") as connection:
        assert connection.execute("select count(*) from cards").fetchone()[0] == 1


@pytest.mark.parametrize("name", ["wire_capture.py", "retrieval_runtime.py"])
def test_public_integration_examples_execute(name, tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "examples" / name)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


def test_public_document_links_resolve_and_have_no_machine_paths():
    documents = [ROOT / name for name in (
        "README.md", "README.en.md", "CONTRIBUTING.md", "SECURITY.md",
        "adapters/dsh-memgarden/README.md", "evals/README.md",
    )] + list((ROOT / "docs").glob("*.md"))
    for path in documents:
        content = path.read_text()
        assert not re.search(r"/Users/|/var/folders/|/private/tmp/claude-", content), path
        for target in re.findall(r"\]\(([^)]+)\)", content):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            local = target.split("#", 1)[0]
            assert (path.parent / local).exists(), f"{path}: {target}"
