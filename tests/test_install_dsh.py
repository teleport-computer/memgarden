from __future__ import annotations

from types import SimpleNamespace

from memgarden.cli import install_dsh


def test_install_dsh_writes_explicit_provider_and_model(tmp_path):
    home = tmp_path / "dsh-home"
    profile = home / "profiles" / "sdk-minimal"
    profile.mkdir(parents=True)
    (profile / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")

    result = install_dsh.run(SimpleNamespace(
        dsh_home=str(home), profile="sdk-minimal", tenant="tenant",
        owner="owner", bin="/tmp/memgarden", storage=str(tmp_path / "garden.db"),
        locale="zh-Hans", state_dir=str(tmp_path / "state"),
        provider="openai", model="gpt-5.4-mini",
    ))

    assert result == 0
    installed = (profile / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "provider: 'openai'" in installed
    assert "model: 'gpt-5.4-mini'" in installed


def test_install_dsh_keeps_existing_defaults_when_routing_is_omitted(tmp_path):
    home = tmp_path / "dsh-home"
    profile = home / "profiles" / "sdk-minimal"
    profile.mkdir(parents=True)

    result = install_dsh.run(SimpleNamespace(
        dsh_home=str(home), profile="sdk-minimal", tenant="tenant",
        owner="owner", bin="/tmp/memgarden", storage=str(tmp_path / "garden.db"),
        locale="zh-Hans", state_dir=str(tmp_path / "state"),
        provider="", model="",
    ))

    assert result == 0
    installed = (profile / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "provider:" not in installed
    assert "model:" not in installed
