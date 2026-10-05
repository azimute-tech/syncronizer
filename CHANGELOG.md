# Changelog

## [Não lançado]

### Adicionado
- Aviso de fim de ciclo: `POST /api/integracoes/tgc/ciclo-concluido` ao fim de todo ciclo
  que leu o Firebird, com `enviados`/`falhas` por recurso. O AgroDB passa a recalcular os
  derivados da fazenda uma vez por ciclo.
- Aviso pendente persistido no `control.db` e reenviado até um 2xx (3 tentativas no ciclo,
  depois no início/fim dos próximos ciclos); pendentes se fundem num só. 404 (API sem a
  rota) desliga o aviso até reiniciar; outro 4xx descarta com WARNING.

### Documentado
- Mudar o intervalo do ciclo (ex.: 15 min) é só `cycle_minutes` + reiniciar pelo painel.
