"""Admin UI server state assembly."""
from types import SimpleNamespace

from syncronizer import configio
from syncronizer.config import Settings
from syncronizer.web import server


def _fake_app(settings, config_file):
    return SimpleNamespace(
        settings=settings,
        paths=SimpleNamespace(config_file=config_file),
        endpoints=[],
        store=None,           # not touched when there are no endpoints
        last_cycle_at=None,
        last_fb_ok=False,
    )


def test_form_reflects_saved_config_not_stale_in_memory(tmp_path):
    """Regression: saving a value without restarting must not appear to 'revert'.

    The service started with cycle_minutes=1 (in app.settings); the user then saves 60
    to config.toml. The form must show 60 (the persisted value), not the stale 1.
    """
    cfg = tmp_path / "config.toml"
    settings = Settings(cycle_minutes=1)          # value loaded at service start
    configio.save_config(cfg, {"cycle_minutes": 60})  # saved later via the UI

    state = server.build_state(_fake_app(settings, cfg))
    field = next(f for f in state["config"] if f["key"] == "cycle_minutes")
    assert field["value"] == 60


def test_form_falls_back_to_defaults_for_unsaved_fields(tmp_path):
    """A field absent from config.toml shows the effective/default value."""
    cfg = tmp_path / "config.toml"
    configio.save_config(cfg, {"cycle_minutes": 60})  # only cycle saved
    settings = Settings()

    state = server.build_state(_fake_app(settings, cfg))
    batch = next(f for f in state["config"] if f["key"] == "batch_size")
    assert batch["value"] == settings.batch_size      # default, not blank
