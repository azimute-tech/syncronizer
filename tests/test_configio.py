from syncronizer import configio


def test_coerce_path_normalizes_backslashes():
    assert configio.coerce("firebird_path", r"C:\GA\Database\app.fdb") == "C:/GA/Database/app.fdb"


def test_coerce_types():
    assert configio.coerce("cycle_minutes", "5") == 5
    assert configio.coerce("auto_update", "on") is True
    assert configio.coerce("auto_update", "false") is False
    assert configio.coerce("api_base_url", "https://x") == "https://x"


def test_write_read_roundtrip_escapes_quotes(tmp_path):
    p = tmp_path / "config.toml"
    configio.write_flat(p, {
        "firebird_path": "C:/GA/app.fdb",
        "cycle_minutes": 10,
        "auto_update": True,
        "api_key": 'weird"value',
    })
    flat = configio.read_flat(p)
    assert flat["firebird_path"] == "C:/GA/app.fdb"
    assert flat["cycle_minutes"] == 10
    assert flat["auto_update"] is True
    assert flat["api_key"] == 'weird"value'  # quote escaped on write, read back intact


def test_save_config_merges_and_normalizes(tmp_path):
    p = tmp_path / "config.toml"
    configio.write_flat(p, {"firebird_host": "localhost"})
    configio.save_config(p, {"firebird_path": r"D:\dados\x.fdb", "cycle_minutes": "7"})
    flat = configio.read_flat(p)
    assert flat["firebird_host"] == "localhost"        # preserved
    assert flat["firebird_path"] == "D:/dados/x.fdb"   # normalized
    assert flat["cycle_minutes"] == 7


def test_flat_config_is_read_by_settings_loader(tmp_path, monkeypatch):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    configio.write_flat(cfgdir / "config.toml", {"firebird_path": "C:/x.fdb", "cycle_minutes": 3})
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    from syncronizer.config import load_settings
    s = load_settings()
    assert s.cycle_minutes == 3
    assert str(s.firebird_path).endswith("x.fdb")


def test_ui_config_wins_over_env(tmp_path, monkeypatch):
    # the UI's config.toml must win over a stray SYNC_* env var
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    configio.write_flat(cfgdir / "config.toml", {"cycle_minutes": 1})
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_CYCLE_MINUTES", "5")
    from syncronizer.config import load_settings
    assert load_settings().cycle_minutes == 1


def test_env_still_fills_fields_absent_from_config(tmp_path, monkeypatch):
    # a field NOT in config.toml still falls back to env
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    configio.write_flat(cfgdir / "config.toml", {"cycle_minutes": 2})
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SYNC_BATCH_SIZE", "999")
    from syncronizer.config import load_settings
    s = load_settings()
    assert s.cycle_minutes == 2   # from config
    assert s.batch_size == 999    # from env (absent in config)


# --------------------------------------------------------------------------- #
# migração de esquema (config_version) — CEPEA ligado por padrão
# --------------------------------------------------------------------------- #
def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCRONIZER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SYNC_INDICADORES_ENABLED", raising=False)
    from syncronizer.config import load_settings
    return load_settings()


def test_migrate_missing_file_is_noop(tmp_path):
    p = tmp_path / "config.toml"
    assert configio.migrate_config(p) == []
    assert not p.exists()


def test_migrate_flat_v1_flips_old_default(tmp_path):
    """Arquivo gravado pelo painel antigo (plano, sem versão) com o default `false`."""
    p = tmp_path / "config.toml"
    configio.write_flat(p, {"indicadores_enabled": False, "backup_enabled": False,
                            "cycle_minutes": 10})
    changes = configio.migrate_config(p)
    assert changes
    flat = configio.read_flat(p)
    assert flat["indicadores_enabled"] is True
    assert flat["backup_enabled"] is False          # outros booleanos intocados
    assert flat["config_version"] == configio.CONFIG_VERSION
    assert configio.migrate_config(p) == []          # idempotente


def test_migrate_grouped_v1_preserves_comments_and_other_sections(tmp_path):
    """config.toml.example antigo copiado à mão: [indicadores] enabled = false."""
    p = tmp_path / "config.toml"
    p.write_text(
        "# cabeçalho\n"
        "[backup]\n"
        "enabled = false  # backup segue desligado\n"
        "\n"
        "[indicadores]\n"
        "# comentário da seção\n"
        "enabled = false  # nota\n"
        "hour = 21\n",
        encoding="utf-8",
    )
    configio.migrate_config(p)
    text = p.read_text(encoding="utf-8")
    assert "# comentário da seção" in text and "# nota" in text
    assert text.splitlines()[1] == f"config_version = {configio.CONFIG_VERSION}"
    tomllib = configio.tomllib
    doc = tomllib.loads(text)
    assert doc["indicadores"]["enabled"] is True
    assert doc["indicadores"]["hour"] == 21
    assert doc["backup"]["enabled"] is False


def test_migrate_respects_explicit_choice_at_current_version(tmp_path):
    p = tmp_path / "config.toml"
    configio.write_flat(p, {"indicadores_enabled": False,
                            "config_version": configio.CONFIG_VERSION})
    assert configio.migrate_config(p) == []
    assert configio.read_flat(p)["indicadores_enabled"] is False


def test_migrate_v1_without_indicadores_only_stamps_version(tmp_path):
    p = tmp_path / "config.toml"
    configio.write_flat(p, {"cycle_minutes": 3})
    configio.migrate_config(p)
    flat = configio.read_flat(p)
    assert flat == {"cycle_minutes": 3, "config_version": configio.CONFIG_VERSION}


def test_save_config_stamps_version_and_keeps_ui_choice(tmp_path):
    p = tmp_path / "config.toml"
    # instalação nova: o painel cria o arquivo já na versão atual
    configio.save_config(p, {"indicadores_enabled": True, "cycle_minutes": "5"})
    assert configio.read_flat(p)["config_version"] == configio.CONFIG_VERSION
    # operador desliga pelo painel -> respeitado (inclusive após novo boot)
    configio.save_config(p, {"indicadores_enabled": False})
    assert configio.migrate_config(p) == []
    assert configio.read_flat(p)["indicadores_enabled"] is False


def test_save_config_on_v1_file_migrates_before_merging(tmp_path):
    p = tmp_path / "config.toml"
    configio.write_flat(p, {"indicadores_enabled": False, "cycle_minutes": 10})
    configio.save_config(p, {"cycle_minutes": "7"})     # form sem o campo
    flat = configio.read_flat(p)
    assert flat["indicadores_enabled"] is True
    assert flat["cycle_minutes"] == 7


def test_load_settings_migrates_legacy_file(tmp_path, monkeypatch):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    configio.write_flat(cfgdir / "config.toml", {"indicadores_enabled": False})
    assert _load(tmp_path, monkeypatch).indicadores_enabled is True


def test_load_settings_respects_disabled_at_current_version(tmp_path, monkeypatch):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    configio.write_flat(cfgdir / "config.toml", {
        "indicadores_enabled": False, "config_version": configio.CONFIG_VERSION})
    assert _load(tmp_path, monkeypatch).indicadores_enabled is False


def test_load_settings_default_enabled_without_file(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch).indicadores_enabled is True


def test_example_config_is_current_version_and_enabled():
    tomllib = configio.tomllib
    from pathlib import Path
    ex = Path(__file__).resolve().parents[1] / "config.toml.example"
    doc = tomllib.loads(ex.read_text(encoding="utf-8"))
    assert doc["config_version"] == configio.CONFIG_VERSION
    assert doc["indicadores"]["enabled"] is True


def test_indicadores_fields_exposed_in_panel():
    keys = {f[0] for f in configio.FIELDS}
    assert {"indicadores_enabled", "indicadores_hour", "indicadores_minute"} <= keys
