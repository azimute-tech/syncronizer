"""Job noturno de indicadores de mercado (DATAGRO) para a API do AgroDB.

Fora de :mod:`syncronizer.endpoints` de propósito — o registry auto-descobre
endpoints ETL ali, e este job NÃO é um endpoint (não lê o Firebird, não estagia
em SQLite, não roda no ciclo de 10 min). É um job de cron próprio orquestrado
por :func:`run_indicadores` (:mod:`.job`), no mesmo molde do backup noturno;
a fonte vive em :mod:`.datagro` e as peças comuns em :mod:`.comum`.
"""
from .comum import IndicadoresError
from .job import indicadores_done_today, read_status, run_indicadores

__all__ = ["run_indicadores", "IndicadoresError", "indicadores_done_today", "read_status"]
