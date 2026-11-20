# netbox-ops v0.4.0

Utilitário local de administração do NetBox em **RHEL e Debian (e distribuições derivadas)**, com PostgreSQL e Redis na VM. Usa **Python 3.12 num venv exclusivo** (`/opt/netbox-ops/venv`), independente do ambiente Python do NetBox. O programa executa-se em PT-PT ou inglês, por TUI (curses) e por CLI.

> **Estado de validação:** testes automatizados e terminal emulado; **restauros reais ainda não ensaiados em RHEL nem Debian**. Não executar um restauro na instalação de produção antes de testar a reversão numa VM de ensaio com dados não produtivos ou snapshot apropriado.

## Instalação / actualização

Copiar o ZIP para a VM e executar:

```bash
sudo unzip -o netbox-ops-v0.4.0.zip -d /opt
sudo /opt/netbox-ops/install.sh
sudo netbox-ops --version
sudo netbox-ops
```

O instalador reconstrói **apenas** o venv do `netbox-ops`, sem instalar bibliotecas no sistema, alterar o venv do NetBox ou reiniciar os respectivos serviços. Mantém o launcher em `/usr/local/sbin/netbox-ops` e o link `/usr/sbin/netbox-ops` necessário ao `PATH` restrito do `sudo`. Configurações e backups persistem em `/etc/netbox-ops` e `/var/lib/netbox-ops` após reinstalação por Git.

Dependências externas: **Python 3.12** com `venv`/`ensurepip` (independentemente da versão de Python predefinida pelo sistema), `systemd`, `journalctl`, `runuser`, ferramentas clientes PostgreSQL (`psql`, `pg_dump`, `pg_restore`, `createdb`, `dropdb`) e `curses`. O programa usa apenas a biblioteca standard do Python. O instalador **não executa apt/dnf**, não instala nem altera PostgreSQL/Redis/NetBox e não altera o Python do sistema.

### Compatibilidade RHEL / Debian (0.4.0)

A detecção baseia-se em `/etc/os-release`. São abrangidas as famílias RHEL (incluindo Rocky/Alma) e Debian (incluindo Ubuntu). Em ambos os casos é necessária uma instalação NetBox local com `systemd`, PostgreSQL local, o utilizador de serviço `postgres` e os caminhos configurados de acordo com a VM; por omissão NetBox usa `/opt/netbox`.

| Componente | RHEL | Debian |
| --- | --- | --- |
| Serviço PostgreSQL | `postgresql.service` ou `postgresql-N.service` (PGDG) | `postgresql@N-main.service`, resolvido através de `pg_lsclusters` |
| Serviço Redis | `redis.service` (fallback `redis-server.service`) | `redis-server.service` (fallback `redis.service`) |
| PostgreSQL CLI | binários do `PATH` ou `/usr/pgsql-N/bin/` (PGDG) | binários de `/usr/bin/` e ferramentas do `postgresql-common` |
| Python 3.12 | `/usr/bin/python3.12` ou `/usr/local/bin/python3.12` | `/usr/bin/python3.12` ou `/usr/local/bin/python3.12` |
| Serviços NetBox / Nginx | unidades habituais `netbox.service`, `netbox-rq.service`, `nginx.service` | idem; alterar o JSON se necessário |

**Atenção ao Debian:** `postgresql.service` pode ser uma unidade agregadora e não controlar o cluster de BD. Por omissão o programa tenta identificar o cluster real com `pg_lsclusters`. Se houver vários clusters ou várias unidades PostgreSQL RHEL, **não escolhe um arbitrariamente**. Especificar a unidade em `services.postgresql` e a porta em `backup.postgres_port`; validar a configuração antes de usar um restauro. O utilizador pode também fixar o directório dos clientes PostgreSQL em `backup.pg_bin_directory` quando utiliza uma versão PGDG fora do `PATH`.

**Configuração:** em novas instalações, `config.example.json` contém `"services": {}` para activar a descoberta; as unidades especificadas em `/etc/netbox-ops/config.json` mantêm sempre prioridade. O instalador **não substitui** um JSON existente, para preservar personalizações anteriores. Em migrações RHEL → Debian, rever explicitamente as unidades persistentes, especialmente PostgreSQL e Redis.

```json
{
  "services": {
    "postgresql": "postgresql@16-main.service",
    "redis": "redis-server.service"
  },
  "backup": {
    "directory": "/var/lib/netbox-ops/backups",
    "database": "netbox",
    "owner": "netbox",
    "media_directory": "/opt/netbox/netbox/media",
    "postgres_port": 5432
  }
}
```

Este JSON é um **exemplo para um Debian com PostgreSQL 16/main na porta 5432**; alterar a versão, a porta e as unidades à instalação efectiva. Para configurações sem overrides, o exemplo incluído no ZIP activa a detecção automática.

**Diagnóstico não destrutivo:**

```bash
sudo netbox-ops platform
sudo netbox-ops services status
```

O comando `platform` mostra a família Linux, o Python utilizado, as unidades detectadas e os binários PostgreSQL. **Não executa operações nem cria dados.** Num Debian cujo Python do sistema não é 3.12, instalar previamente o Python 3.12 com suporte `venv` e apontar `PYTHON_BIN` se necessário; o instalador recusa outra versão em vez de usar silenciosamente 3.11/3.13.


## Identidade visual da v0.4.0

Adoptada a **paleta A — NetBox Dark**, com realce teal, superfícies azul-petróleo/escuras e cores semânticas para resultados. A interface `curses` utiliza aproximações xterm-256 (não exige terminais true-color), fallback de oito cores e respeita a variável `NO_COLOR`. O indicador de armazenamento é um **donut Braille**: backups / outros dados / espaço livre, com proporções reais do filesystem que contém os backups. Num terminal que não suporte estes glifos Unicode, os valores numéricos e a legenda continuam a ser a referência.

As operações com barra de progresso terminam **no mesmo ecrã**, onde ficam a lista de tarefas e o resultado/erro. Após o fim, **Enter ou Esc** regressa ao menu anterior. Durante a execução não se deve interromper a operação. A barra apresenta fracções de etapas/ficheiros/serviços concluídos, não uma estimativa do tempo nem dos bytes transferidos.

## Interface TUI

O **menu principal** tem as entradas «Gerir serviços», «Gestão de backups», «Verificar actualizações», upgrades do NetBox/plugins e self-upgrade. As opções de actualização continuam **não implementadas**. À direita aparecem **estado, versão, CPU e memória actuais** de cada serviço, com refrescamento aproximado de 3 segundos. A secção de armazenamento apresenta um **gráfico circular Braille** com legenda de cores sincronizadas com as respectivas fatias (teal=backups, azul=outros dados, cinzento=livre), com três parcelas: bytes ocupados por backups, restantes dados existentes no filesystem e espaço livre. A percentagem ao centro indica o peso dos backups **na capacidade total do filesystem onde ficam guardados**, não a percentagem de todos os ficheiros do disco.

O CPU é calculado por diferença entre leituras de `CPUUsageNSec` (ou contador de cgroup v2); pode ultrapassar 100% quando um serviço usa vários cores. A memória representa a ocupação do cgroup, não o RSS de um processo isolado. Quando o kernel/systemd não fornece a métrica, aparece `—`. Não há recolha histórica, base SQLite, gráficos de desempenho histórico, serviço colector ou timer.

### Navegação

- `↑/↓` navegam, `Enter` abre, `Esc` apaga primeiro o realce e depois volta ao ecrã anterior; no principal `Esc` **não fecha**. `Q` sai apenas do menu principal.
- Em **Gerir serviços**, `Espaço` marca/desmarca, `Tab` alterna entre a lista e as operações horizontais **no fundo** do ecrã (`Iniciar`, `Parar`, `Reiniciar`, `Activar`, `Desactivar`); `Enter` expande os logs do serviço focado ou executa a operação seleccionada (as alterações exigem confirmação). O painel direito mostra estado, arranque, PID, CPU/RAM actuais, versão e uptime, **com logs no painel inferior, sem ocupar o painel de métricas**. A vista normal apresenta os **últimos 50 logs** num painel inferior e a opção **Live log: ON/OFF** no rodapé; `L` também alterna o modo Live. `Enter` sobre um serviço expande o painel de logs, permitindo scroll com `PgUp/PgDn`, `L` Live ON/OFF, `N` escolher de 1 a 500 linhas e `F5` actualizar. A leitura inicial é assíncrona e o live log usa `journalctl --follow` não bloqueante. A CLI continua a permitir `services logs`.
- As acções sem efeito no estado actual aparecem desactivadas. Num lote misto, são executados apenas os serviços elegíveis e os restantes são indicados como ignorados, antes da confirmação. `enable/disable` continuam a não iniciar nem parar serviços.
- As versões de NetBox/NetBox RQ (mesma versão da aplicação), Nginx, servidor PostgreSQL e Redis são obtidas **uma vez por execução do netbox-ops** e guardadas em memória. A consulta decorre em segundo plano; enquanto não estiver concluída, aparece `…`. **A versão do NetBox passa a ser lida de `/opt/netbox/netbox/release.yaml`**. `manage.py --version` apresenta a versão do **Django**, pelo que deixou de ser utilizado para identificar o NetBox. Se não for possível ler a versão com segurança, aparece `—`. A cache pode ser invalidada quando os upgrades forem implementados.
- Em **Gestão de backups**, `↑/↓` movem o foco pela lista, `Espaço` marca/desmarca para operações em lote, `*` ou `S` selecciona/desselecciona todos e `Tab` alterna entre a lista e **a barra horizontal de operações por baixo dos painéis**. Na barra, `←/→` escolhem a operação e **`Enter` executa apenas a operação focada**; na lista, Enter não abre qualquer página adicional. A tabela apresenta cabeçalhos centrados e a coluna **Etiqueta** com `MANUAL` ou `PRÉ-REST.`. O painel direito **acompanha sempre a linha focada**; marcar um ou vários backups não bloqueia a pré-visualização dos outros. As operações em lote continuam a usar **apenas** os itens marcados, com resumo de quantidade e tamanho no painel direito, quando existe espaço. Operações indisponíveis aparecem esbatidas (ex.: restauro com vários ficheiros marcados). A barra ocupa uma linha em terminais largos e quebra linhas em terminais estreitos, sendo desenhada **junto ao rodapé**. No espaço abaixo da lista existe o mesmo gráfico circular Braille do menu principal, com espaço ocupado por backups/outros dados/livre, quando o terminal dispõe de altura suficiente. Mantêm-se os atalhos `C` criar, `R` restaurar **um ficheiro**, `V` validar todos os marcados (ou o ficheiro focado), `D` apagar os marcados (ou o ficheiro focado) e `F5` actualizar a lista. `*` selecciona/deselecciona todos, mas deixou de existir a acção independente «Apagar todos». Todas as operações destrutivas exigem confirmação; nenhuma é iniciada simplesmente por entrar no menu. Uma confirmação incorrecta mostra um **alerta** e, após Enter/Esc, regressa à vista anterior **sem executar a operação**. Num backup `full`, a escolha do restauro (apenas BD / apenas media / tudo) tem um ecrã próprio com descrições, setas e Enter; não existe opção executada por defeito.
- A lista inicial lê **apenas nomes, tamanhos e datas** do filesystem. Os metadados do item seleccionado são obtidos uma vez e **mantidos em cache durante o menu**, com invalidação perante alteração do ficheiro/manifesto. Os novos backups `media` e `full` incluem um **sidecar JSON** pequeno (`.tar.gz.json`) para leitura quase imediata. Arquivos mais antigos sem sidecar podem demorar a disponibilizar o detalhe, mas a leitura é feita em segundo plano e **não bloqueia as setas**. O índice de integridade nunca é inferido a partir da existência de um manifesto: só aparece como validado depois da operação `V` bem-sucedida.
- `F5` actualiza estado onde suportado; `L` altera idioma no ecrã principal. `--no-tui` disponibiliza menu de texto simplificado para terminais incompatíveis.

### Barra de progresso

Criação, validação, restauro e eliminação de backups usam uma barra **por etapas ou ficheiros concluídos**, não uma percentagem estimada de bytes processados pelo `pg_dump`, TAR ou `pg_restore`. **Por baixo da barra aparece a lista de tarefas, cada uma com o estado em curso, concluído ou falhado**. Para validar/apagar vários backups surgem os nomes dos arquivos processados. A gestão de serviços apresenta igualmente a lista dos serviços já alterados e do serviço em execução. O ecrã de progresso permanece visível no fim da operação, com o estado final e eventuais erros; Enter/Esc regressa à gestão, sem abrir um segundo ecrã redundante. **Não fechar o terminal durante um restauro.**

## Novidades da v0.4.0

- **RHEL e Debian:** descoberta de família Linux, unidades systemd PostgreSQL/Redis e executáveis PostgreSQL, respeitando overrides explícitos; em Debian o restauro usa a porta do cluster seleccionado.
- **Python 3.12 obrigatório:** procura automática em `/usr/bin` e `/usr/local/bin`, com `PYTHON_BIN` opcional; o NetBox e o Python do sistema não são alterados.
- **TUI:** confirmação errada tem alerta, a coluna de backups distingue `MANUAL`/`PRÉ-REST.`, o painel de detalhe acompanha o cursor e o donut aparece no espaço livre da esquerda.
- **Restauro `full`:** escolha mais clara entre BD, media ou ambos, sempre exigindo navegação explícita, Enter e confirmação adicional.
- **Compatibilidade:** mantém o motor de backup/restauro e a política de serviços/Redis da v0.3.6. Os testes são isolados e **não substituem ensaios reais** nos dois SO.

## Correcções específicas da v0.3.6

- **Ordem da lista:** todas as entradas `db`, `media`, `full` e `legacy` são ordenadas pela data UTC do nome (mais recente primeiro); para nomes sem data reconhecível usa-se a hora de modificação. A listagem não precisa de abrir arquivos.
- **Validar:** botão destacado na barra de operações, depois de «Criar». Atalho `V` para validar o backup focado ou todos os marcados. A validação de arquivos legados é **apenas de integridade**, sem permitir o seu restauro.
- **Apagar todos:** removido da TUI e da CLI. Continua possível marcar explicitamente vários backups e eliminá-los, sempre com confirmação.
- **Restauro de um backup `full` apenas da BD:** corrigida a falta de extração do dump `database/netbox.dump` antes da importação temporária. Corrigida também a acção sobre um único backup marcado sem linha focada. Se o restauro estiver indisponível, o TUI explica se faltam metadados, se há mais de um backup marcado ou se o arquivo é incompatível.
- **Segurança:** os restauros continuam a validar checksums e compatibilidade e a exigir confirmação; não foram introduzidas alterações na política de Redis nem validação operacional real de PostgreSQL na VM.

## Política de backup da v0.4.0

| Tipo | Ficheiros gerados | Inclui |
| --- | --- | --- |
| `db` | `netbox-db-....dump` e `.dump.json` | Base de dados PostgreSQL e manifesto de integridade |
| `media` | `netbox-media-....media.tar.gz` e `.media.tar.gz.json` | **Apenas** `/opt/netbox/netbox/media` (imagens/anexos/uploads) |
| `full` | `netbox-full-....full.tar.gz` e `.full.tar.gz.json` | **BD + media**, manifesto e hashes |

Nesta sprint **não** são copiados `configuration.py`, `ldap_config.py`, `local_requirements.txt`, Gunicorn, Nginx, certificados, scripts, reports, SELinux, configs de Redis/PostgreSQL nem outras configurações do SO. O código só aceita `media` como artefacto para backup/restauro. Um `/etc/netbox-ops/config.json` anterior que contenha `backup.artifacts` é lido para aproveitar exclusivamente a entrada `media`; as restantes são ignoradas. A configuração recomendada passa a utilizar `backup.media_directory` (ver `config.example.json`).

**Salvaguardas automáticas de restauro:** sempre que um restauro for substituir a BD e/ou media, o netbox-ops cria previamente o(s) respectivo(s) backup(s) de segurança, identificado(s) por `-pre-restore-` no nome e por `purpose: pre_restore` no manifesto e sidecar JSON. O TUI apresenta a etiqueta **PRÉ-REST.** e o detalhe indica «Pré-restauro (salvaguarda automática)». Os backups criados manualmente têm `purpose: manual`. Os arquivos automáticos criados em versões anteriores não recebem etiquetas retroactivamente, por falta de metadados suficientes para provar a origem. Backups preventivos são ficheiros de backup normais e podem ser verificados, restaurados ou apagados conforme as regras usuais.

Os pequenos sidecars dos novos arquivos servem exclusivamente para pré-visualização e **não substituem** a validação de integridade. A sua eliminação acompanha a eliminação do backup correspondente.

Os backups são gravados em `/var/lib/netbox-ops/backups` (`root`, directório `0700`, arquivos `0600`). O ficheiro `.dump` utiliza o formato custom de PostgreSQL (`pg_dump -Fc`); não suporta importação directa de dumps SQL simples. **A BD pode, por si só, conter informação interna e pessoal**, apesar de os ficheiros de configuração e as credenciais LDAP já não serem incluídos. Não há aviso repetitivo no menu; aplicam-se os controlos de acesso da política interna.

O backup `db` não pára serviços e usa um snapshot do PostgreSQL. O backup `media` é uma cópia em utilização e pode ser incoerente perante uploads simultâneos. O `full` pára temporariamente **NetBox e NetBox RQ**, preserva o estado original e só volta a iniciar os serviços que já estavam activos, para coordenar a BD e os uploads.

## CLI

```bash
# Consultas e acções de serviços
sudo netbox-ops services status
sudo netbox-ops services logs --service netbox --lines 50
sudo netbox-ops services manage
sudo netbox-ops services restart --service netbox
sudo netbox-ops services enable --service netbox

# Gestão de backups
sudo netbox-ops backup manage
sudo netbox-ops backup db
sudo netbox-ops backup media
sudo netbox-ops backup full
sudo netbox-ops backup list
sudo netbox-ops backup list --kind full
sudo netbox-ops backup verify --kind media --file /var/lib/netbox-ops/backups/NOME.media.tar.gz

# Restauros: confirmação interactiva obrigatória
sudo netbox-ops restore db --file /caminho/backup.dump
sudo netbox-ops restore media --file /caminho/backup.media.tar.gz
sudo netbox-ops restore full --file /caminho/backup.full.tar.gz --only db
sudo netbox-ops restore full --file /caminho/backup.full.tar.gz --only media
sudo netbox-ops restore full --file /caminho/backup.full.tar.gz --only all

# Eliminação (confirmação explícita)
sudo netbox-ops backup delete --file /var/lib/netbox-ops/backups/NOME.dump

```

A CLI permite automatizar restauros exclusivamente com **ambos** os parâmetros `--yes --acknowledge-data-loss` (nunca apenas um deles). Em operações envolvendo PostgreSQL/Redis, a CLI de serviços exige `--allow-critical` quando é usado `--yes`.

### Restauro de um pacote completo

No TUI, seleccionar um arquivo `full`, premir `R` e escolher:

- **Apenas BD:** não substitui media.
- **Apenas media:** não altera a BD.
- **Tudo:** repõe BD e media como conjunto.

O programa verifica o backup antes de substituir dados, prepara BD temporária quando relevante, cria backups de segurança dos componentes a substituir, pára `netbox` e `netbox-rq`, executa o restauro, verifica as migrações com `manage.py migrate --check` e reinicia **apenas os serviços originalmente activos**. Quando a BD é substituída, mantém a anterior como `nbo_prev_*`. Em caso de falha tenta reverter as alterações; **não existe uma transacção única garantida entre PostgreSQL e filesystem**. Os restauros parciais podem deixar referências a media sem correspondência na BD, ou vice-versa.

**Redis:** reiniciar `netbox` e `netbox-rq` não limpa necessariamente a cache Redis. O netbox-ops **preserva intencionalmente o Redis e as filas RQ**, pois a configuração das bases Redis de cache/tarefas ainda não foi validada na VM. Depois de restaurar a BD, rever a coerência da cache antes de reabrir a aplicação. Não existe `FLUSHALL` ou `FLUSHDB` automático que pudesse eliminar tarefas ou outros dados. Esta validação deve integrar o ensaio de restauro; o processo ainda não pode ser considerado completamente automatizado para recuperação em produção.

### Versões de NetBox registadas em backups anteriores

Até à v0.3.1, o campo `netbox_version` dos manifestos era recolhido com `manage.py --version`, isto é, correspondia à **versão do Django** e não podia comprovar a compatibilidade da BD com o NetBox. Na v0.3.3 os novos manifestos incluem `netbox_version_source: release.yaml`. Os backups anteriores continuam listados e podem ser validados, mas a versão da aplicação aparece como **desconhecida**. No TUI, um restauro desses backups exige primeiro escrever `ACCEPT UNKNOWN VERSION` e depois a confirmação específica de restauro. A CLI exige `--accept-unknown-version` após validação externa. Isto **não prova compatibilidade**: verificar previamente a versão de origem e usar uma VM de ensaio. Não há alteração automática dos backups antigos.

### Compatibilidade com v0.2.0

- Backups `db` anteriores continuam acessíveis e sujeitos a verificações de versão/manifesto.
- Archives `.files.tar.gz` e pacotes `full` antigos que incluam configurações são identificados como **legados** na listagem, mas **não podem ser restaurados** por esta versão, para impedir a reposição inadvertida de ficheiros de configuração/credenciais. Podem ser **validados em modo apenas de leitura** (estrutura e SHA-256 dos ficheiros) e eliminados de forma explícita, mas não restaurados. Conserve cópias antigas antes de decidir eliminá-los.
- O programa não efectua upgrades do esquema NetBox durante o restauro. Uma versão divergente é recusada por defeito; a CLI disponibiliza `--accept-unknown-version` e `--allow-version-mismatch` apenas para ensaios cuidadosamente controlados.

## Limites da v0.4.0

- A recolha de CPU e RAM é **instantânea**, sem histórico ou gráficos. A primeira amostra de CPU pode apresentar `—` até existir uma segunda leitura (aprox. 3 segundos).
- A consulta de versão do NetBox usa o campo `version` do ficheiro `/opt/netbox/netbox/release.yaml` (formato tradicional de instalação por Git/release), e **não** `manage.py --version`, que pertence ao Django. Nginx usa `nginx -v`, Redis usa `redis-server --version` e PostgreSQL consulta `SHOW server_version` na instância local. Se a ferramenta não conseguir consultar uma versão, apresenta `—`, sem impedir a gestão dos serviços.
- As opções de upgrade, incluindo self-upgrade, continuam a ser **apenas menus/CLI preparadas**. A invalidação automática de versões após upgrade será ligada quando essa funcionalidade for implementada.
- O restauro da BD **ainda não executa limpeza selectiva do Redis**, porque as bases de cache/RQ da VM não foram confirmadas. Não executar um restauro real em produção sem um ensaio e validação de coerência.

## Testes

```bash
cd /opt/netbox-ops
/opt/netbox-ops/venv/bin/python -m unittest discover -s tests -v
bash -n install.sh upgrade.sh
```

Os testes usam temporários, simulação de systemd/PostgreSQL e terminal emulado; **não provam o funcionamento de um restauro real em RHEL/NetBox**. Recomenda-se testar `services status`, `backup media`, `backup db`, `backup full` numa janela aprovada e, separadamente, um restauro completo e parcial num clone/snapshot isolado da VM.
