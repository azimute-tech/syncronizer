# Changelog

## [Não lançado]

### Adicionado
- Indicador do Boi DATAGRO praça SP (`DATAGRO_BOI_GORDO_SP`) no job noturno de
  indicadores, ao lado do CEPEA: lê os 5 últimos dias úteis de SP do
  `pec.datagro.com/pec/mapas/boletim_cinco.svg` (fonte oficial, gravada em `FONTE`),
  infere o ano (corrente; mês à frente do mês de hoje = ano anterior) e confere com a
  data do `boi.svg`; o JSON `quadros` da DATAGRO só confirma o último dia (divergência
  vira warning). Envia até 5 itens num POST; formato inesperado falha fechado.
  `[indicadores] datagro_enabled` (padrão `true`) desliga só essa fonte.
- `last_indicadores.json` passa a ter um bloco por fonte (`cepea`, `datagro`) e `ok`
  global só com as duas OK; uma fonte fora do ar não derruba a outra, e o catch-up
  refaz apenas a pendente (`indicadores-once` refaz as duas).
- O "envio de hoje concluído" que trava o catch-up olha o bloco de CADA fonte, não
  o `ok` do topo: um status gravado pela versão anterior (só CEPEA) não conta, e o
  catch-up coleta o DATAGRO no mesmo dia do self-update. O catch-up também roda uma
  vez 90 s após o boot — serviço relançado depois das 20:30 não espera o interval.
- Aviso de fim de ciclo: `POST /api/integracoes/tgc/ciclo-concluido` ao fim de todo ciclo
  que leu o Firebird, com `enviados`/`falhas` por recurso. O AgroDB passa a recalcular os
  derivados da fazenda uma vez por ciclo.
- Aviso pendente persistido no `control.db` e reenviado até um 2xx (3 tentativas no ciclo,
  depois no início/fim dos próximos ciclos); pendentes se fundem num só. 404 (API sem a
  rota) desliga o aviso até reiniciar; outro 4xx descarta com WARNING.

### Removido
- Indicador do Boi Gordo CEPEA/B3 (`CEPEA_BOI_GORDO`): o job noturno de indicadores
  deixa de coletar e enviar o CEPEA (decisão de 18/10/2026: só DATAGRO e as travas). O
  scraper do widget sai; os helpers que o DATAGRO usa (faixa anti-lixo, parse de R$,
  conferência da resposta da API) passam para `indicadores/comum.py`, sem mudança de
  comportamento. A API do AgroDB passa a rejeitar `CEPEA_BOI_GORDO` como item
  descontinuado: instalação ainda na versão antiga do serviço vai logar falha do CEPEA
  (e o catch-up dela retenta a cada 30 min) até o self-update; o DATAGRO segue entrando.
- `[indicadores] datagro_enabled`: com uma fonte só, duplicava `[indicadores] enabled`,
  que passa a ligar/desligar o job todo; o campo sai do painel. config.toml antigo que
  ainda tenha a chave carrega normalmente (ela é ignorada).
- Bloco `cepea` do `last_indicadores.json`: o status passa a ter só o bloco `datagro`, e
  `ok` global = `datagro.ok`. Status antigo com bloco `cepea` é ignorado (só o `datagro`
  decide se o dia está feito, então um `cepea` com falha não deixa o catch-up rodando) e
  some na próxima gravação. Bloco `datagro` com `skipped` (desligado pela chave removida)
  não conta como feito: a coleta volta no mesmo dia do self-update.

### Documentado
- Mudar o intervalo do ciclo (ex.: 15 min) é só `cycle_minutes` + reiniciar pelo painel.
