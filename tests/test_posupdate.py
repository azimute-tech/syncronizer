"""Ciclo pós-atualização fora da janela (posupdate.py).

A propriedade que importa: código novo sincroniza já, mesmo fora da janela,
até completar UM ciclo — e nunca fica sincronizando a noite toda.
"""
from types import SimpleNamespace

from syncronizer import posupdate
from syncronizer.paths import build_paths


def _paths(tmp_path):
    return SimpleNamespace(last_cycle_commit=tmp_path / "state" / "last_cycle_commit")


def _stats(fb=True, errors=0, failed=0):
    totais = {"errors": errors, "failed": failed}
    return SimpleNamespace(firebird_available=fb, totals=lambda: totais)


def test_commit_sem_ciclo_completo_esta_pendente(tmp_path):
    # Instalação nova ou primeiro boot depois do update: sem marcador.
    assert posupdate.PosUpdate(_paths(tmp_path), "abc123").pendente is True


def test_marcador_de_outro_commit_continua_pendente(tmp_path):
    p = _paths(tmp_path)
    p.last_cycle_commit.parent.mkdir(parents=True)
    p.last_cycle_commit.write_text("antigo\n")
    assert posupdate.PosUpdate(p, "novo").pendente is True


def test_sem_git_nao_ha_pendencia(tmp_path):
    # Dev sem working tree: current_sha devolve None e a janela vale como sempre.
    pos = posupdate.PosUpdate(_paths(tmp_path), None)
    assert pos.pendente is False
    assert pos.forcar_fora_da_janela() is False


def test_ciclo_completo_grava_o_commit_e_encerra(tmp_path):
    p = _paths(tmp_path)
    pos = posupdate.PosUpdate(p, "abc123")
    assert pos.forcar_fora_da_janela() is True
    assert pos.registrar(_stats()) is True
    assert pos.pendente is False
    assert p.last_cycle_commit.read_text() == "abc123"
    # Encerrada, fora da janela volta a pular.
    assert pos.forcar_fora_da_janela() is False
    # E o próximo boot no mesmo commit já não força nada.
    assert posupdate.PosUpdate(p, "abc123").pendente is False


def test_ciclo_incompleto_mantem_a_pendencia(tmp_path):
    p = _paths(tmp_path)
    pos = posupdate.PosUpdate(p, "abc123")
    assert pos.registrar(_stats(fb=False)) is False       # Firebird fora
    assert pos.registrar(_stats(errors=1)) is False       # endpoint quebrou
    assert pos.registrar(_stats(failed=3)) is False       # linhas não entregues
    assert pos.pendente is True
    assert not p.last_cycle_commit.exists()


def test_teto_de_ciclos_fora_da_janela(tmp_path):
    pos = posupdate.PosUpdate(_paths(tmp_path), "abc123")
    rodou = [pos.forcar_fora_da_janela() for _ in range(posupdate.MAX_CICLOS_FORA_DA_JANELA + 3)]
    assert rodou.count(True) == posupdate.MAX_CICLOS_FORA_DA_JANELA
    assert rodou[-1] is False
    # Estourado o teto, a pendência continua: o ciclo DENTRO da janela a conclui.
    assert pos.pendente is True
    assert pos.registrar(_stats()) is True


def test_ciclo_dentro_da_janela_tambem_conclui(tmp_path):
    # Dentro da janela o scheduler nem pergunta; basta registrar o ciclo.
    pos = posupdate.PosUpdate(_paths(tmp_path), "abc123")
    assert pos.registrar(_stats()) is True
    assert pos.tentativas_fora == 0


def test_paths_tem_o_marcador_no_state(tmp_path):
    paths = build_paths(None, data_dir=tmp_path)
    assert paths.last_cycle_commit == tmp_path / "state" / "last_cycle_commit"
