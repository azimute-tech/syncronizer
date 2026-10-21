"""Ciclo logo após código novo, mesmo fora da janela.

Pedido do Nelson (20/10/2026): quando as máquinas instaladas atualizarem, a
sincronização precisa rodar de novo para garantir o sincronismo, mesmo fora da
janela de execução. Sem isto, um self-update aplicado às 19:30 (janela 07–19)
só sincronizaria no dia seguinte — e um feed novo (ex.: `insumos_entradas`)
ficaria um dia inteiro sem chegar ao AgroDB.

A regra: o commit em execução que ainda NÃO completou um ciclo está
"pendente". Enquanto pendente, o ETL roda mesmo fora da janela. Um ciclo
completo grava o commit em ``state/last_cycle_commit`` e encerra a exceção.
Vale para self-update, update manual e instalação nova — tudo que troca o
código troca o commit. Sem working tree git (dev) não há commit e nada muda.

"Completo" = leu o Firebird, nenhum endpoint levantou erro e nenhuma linha
falhou no envio. Fora da janela a exceção tem TETO
(:data:`MAX_CICLOS_FORA_DA_JANELA`): uma linha rejeitada para sempre pela API
não pode deixar a máquina sincronizando a noite toda. Estourado o teto, o
backlog continua durável no control.db e escoa no próximo ciclo dentro da
janela, que também conclui a pendência.

Só stdlib + :mod:`syncronizer.updater` (também stdlib), como o bootgate.
"""
from __future__ import annotations

from . import updater

# Com o ciclo padrão de 10 min, ~1 h de tentativas fora da janela por commit.
MAX_CICLOS_FORA_DA_JANELA = 6


def ciclo_completo(stats) -> bool:
    """O ciclo leu o Firebird e entregou tudo, sem erro de endpoint nem envio falho."""
    if not getattr(stats, "firebird_available", False):
        return False
    totais = stats.totals()
    return totais["errors"] == 0 and totais["failed"] == 0


class PosUpdate:
    """Estado da pendência do commit em execução (um por processo)."""

    def __init__(self, paths, sha: str | None, log=None):
        self._paths = paths
        self._sha = sha
        self._log = log
        self.tentativas_fora = 0
        self.pendente = bool(sha) and updater.read_marker(paths.last_cycle_commit) != sha

    @classmethod
    def carregar(cls, paths, log=None) -> "PosUpdate":
        return cls(paths, updater.current_sha(paths, log=log), log=log)

    @property
    def sha(self) -> str | None:
        return self._sha

    def forcar_fora_da_janela(self) -> bool:
        """Fora da janela: roda este ciclo? Conta a tentativa quando sim."""
        if not self.pendente or self.tentativas_fora >= MAX_CICLOS_FORA_DA_JANELA:
            return False
        self.tentativas_fora += 1
        return True

    def registrar(self, stats) -> bool:
        """Depois de um ciclo: conclui a pendência se ele foi completo.

        Devolve True quando a pendência foi concluída neste ciclo.
        """
        if not self.pendente or not ciclo_completo(stats):
            return False
        updater.write_marker(self._paths.last_cycle_commit, self._sha or "")
        self.pendente = False
        return True
