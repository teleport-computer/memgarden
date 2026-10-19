"""Release identity must fail closed without treating branch CI as a tag."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/check_version_consistency.py"

@pytest.fixture
def version_check(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("version_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    for key in ("GITHUB_REF_NAME", "GITHUB_REF_TYPE", "MEMGARDEN_RELEASE_TAG"):
        monkeypatch.delenv(key, raising=False)
    def run(version, ref_type, ref, explicit="", adapter=None, locked=None):
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "' + version + '"\n')
        (tmp_path / "uv.lock").write_text('name = "memgarden"\nversion = "' + (locked or version) + '"\n')
        target = tmp_path / "src/memgarden/adapters/dsh/package.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"version": adapter or version.replace(".post", "+post.")}))
        monkeypatch.setenv("GITHUB_REF_TYPE", ref_type)
        monkeypatch.setenv("GITHUB_REF_NAME", ref)
        monkeypatch.setenv("MEMGARDEN_RELEASE_TAG", explicit)
        return module.main()
    return run

@pytest.mark.parametrize("version", ["0.23.0", "0.23.0.post1"])
def test_matching_tag(version_check, version):
    assert version_check(version, "tag", "v" + version) == 0

@pytest.mark.parametrize("version,tag", [("0.23.0", "v0.23.1"), ("0.23.0.post1", "v0.23.0.post2"), ("0.23.0.post1", "v0.23.0")])
def test_mismatched_tag(version_check, version, tag):
    assert version_check(version, "tag", tag) == 1

@pytest.mark.parametrize("tag", ["", "vbad", "0.23.0.post1", "v0.23.0.post", "v0.23.0.post1junk", "v0.23.0\n"])
def test_malformed_tag(version_check, tag):
    assert version_check("0.23.0.post1", "tag", tag) == 1

@pytest.mark.parametrize("branch", ["main", "feature/retry", "v0.99.0", "malformed"])
def test_branch_is_not_tag(version_check, branch):
    assert version_check("0.23.0.post1", "branch", branch) == 0

@pytest.mark.parametrize("tag,result", [("v0.23.0.post1", 0), ("v0.23.0.post2", 1), ("main", 1)])
def test_dispatch_explicit_tag(version_check, tag, result):
    assert version_check("0.23.0.post1", "branch", "main", explicit=tag) == result

def test_adapter_semver_mapping(version_check):
    assert version_check("0.23.0.post1", "", "", adapter="0.23.0.post1") == 1

def test_lock_mismatch(version_check):
    assert version_check("0.23.0.post1", "", "", locked="0.23.0") == 1
