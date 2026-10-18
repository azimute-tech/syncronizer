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
- Aviso de fim de ciclo: `POST /api/integracoes/tgc/ciclo-concluido` ao fim de todo ciclo
  que leu o Firebird, com `enviados`/`falhas` por recurso. O AgroDB passa a recalcular os
  derivados da fazenda uma vez por ciclo.
- Aviso pendente persistido no `control.db` e reenviado até um 2xx (3 tentativas no ciclo,
  depois no início/fim dos próximos ciclos); pendentes se fundem num só. 404 (API sem a
  rota) desliga o aviso até reiniciar; outro 4xx descarta com WARNING.

### Documentado
- Mudar o intervalo do ciclo (ex.: 15 min) é só `cycle_minutes` + reiniciar pelo painel.
