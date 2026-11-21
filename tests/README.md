# Testes do netbox-ops

Os testes utilizam `unittest` (biblioteca standard) e não devem requerer serviços systemd ou PostgreSQL reais.

## Executar toda a suite

A partir da raiz do repositório, com Python 3.12 ou superior:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

## Executar testes de um domínio

```bash
python -m unittest tests.backups.test_backups -v
python -m unittest tests.services.test_services -v
python -m unittest tests.tui.test_tui -v
```

## Organização

- `backups/`: criação, validação e restauro de backups.
- `cli/`: comandos, parsing e confirmações da CLI.
- `config/`: validação e carregamento da configuração.
- `i18n/`: traduções e chaves de idioma.
- `install/`: instalação e ligações simbólicas.
- `services/`: operações e salvaguardas de serviços systemd.
- `tui/`: navegação, apresentação e operações na TUI.
- `helpers/`: objectos simulados partilhados pelos testes.
- `regression/`: testes históricos por versão, mantidos temporariamente com os seus nomes para preservar o contexto.

Os testes de regressão por versão poderão ser movidos gradualmente para o respectivo domínio em PRs futuras. Não se deve mudar as asserções no mesmo commit que reorganiza ficheiros.
