"""host.toml — created with a token on first run, stable afterwards."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis_host.config import ConfigError, HostConfig, default_config_path, load_config, parse_config, render_toml


def test_first_run_creates_a_config_with_a_token(tmp_path: Path):
    path = tmp_path / "Jarvis2" / "host.toml"
    cfg, created = load_config(path)
    assert created and path.exists() and len(cfg.token) >= 40
    assert cfg.listen == "0.0.0.0:9030" and cfg.port == 9030 and cfg.host == "0.0.0.0"
    assert cfg.fs_roots == (Path.home(),) and cfg.shell_allow and cfg.screen_enabled and cfg.outlook_accounts == ()
    again, created_again = load_config(path)
    assert not created_again and again.token == cfg.token and again.name == cfg.name


def test_parse_and_render_round_trip():
    cfg = HostConfig(
        name="laptop",
        listen="127.0.0.1:9031",
        token="t0k3n",
        outlook_accounts=("aapostolov@postbank.bg",),
        fs_roots=(Path(r"C:\Users\Arsen"), Path(r"D:\Shared")),
        shell_allow=False,
        screen_enabled=True,
    )
    parsed = parse_config(render_toml(cfg))
    assert parsed == cfg
    assert parsed.port == 9031 and parsed.host == "127.0.0.1"


def test_missing_token_is_generated_and_written_back(tmp_path: Path):
    path = tmp_path / "host.toml"
    path.write_text('name = "x"\nlisten = "0.0.0.0:9030"\n', encoding="utf-8")
    cfg, created = load_config(path)
    assert created and cfg.token and cfg.name == "x"
    assert f'token = "{cfg.token}"' in path.read_text(encoding="utf-8")


def test_invalid_config_is_a_clear_error():
    with pytest.raises(ConfigError, match=r"host\.toml"):
        parse_config("name = [unclosed")
    with pytest.raises(ConfigError, match="host:port"):
        parse_config('name = "x"\nlisten = "nonsense"\n')
    with pytest.raises(ConfigError, match=r"fs\.roots must be a list"):
        parse_config('name = "x"\n[fs]\nroots = "C:/"\n')


def test_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("JARVIS_HOST_CONFIG", str(tmp_path / "custom.toml"))
    assert default_config_path() == tmp_path / "custom.toml"
