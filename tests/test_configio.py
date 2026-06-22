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
