# netbox-ops — Histórico de alterações

## NBH-xxx — Correções da gestão de backups e do restauro (netbox-ops v0.3.6)

**Situação inicial:** na v0.3.5 a lista de backups seguia a ordem alfabética dos nomes, misturando diferentes tipos sem respeitar a data de criação. O restauro parcial apenas da BD a partir de um pacote completo falhava porque o dump não era extraído antes da tentativa de importação. A gestão incluía uma acção autónoma «Apagar todos» e a opção «Validar» era pouco evidente, sobretudo para arquivos legados.

**Trabalho realizado:**
- Ordenação cronológica dos backups, do mais recente para o mais antigo, independentemente do tipo ou da etiqueta pré-restauro. Nomes sem data válida usam a hora de modificação.
- Remoção de «Apagar todos» do menu interactivo, CLI e gestor de backups; mantida a eliminação selectiva, inclusive em lote, com confirmação.
- Destaque e reposicionamento de «Validar» na barra de operações; mantido o atalho V e adicionada validação apenas de integridade para arquivos `legacy`, sem permitir o seu restauro.
- Corrigida a extração do dump PostgreSQL em restauros `full --only db`, e a navegação de restauro quando existe um único backup marcado sem linha focada.
- Acrescentadas mensagens explicativas para acções de restauro temporariamente indisponíveis ou incompatíveis.
- Actualizada a documentação e executados testes de regressão e novos testes isolados.

**Motivo:** melhorar a legibilidade, reduzir o risco de eliminação acidental e corrigir falhas no fluxo de reposição de dados.

**Validação:** testes automatizados com PostgreSQL/systemd simulados; validação real de restauro, rollback e coerência do Redis na VM RHEL ainda pendente.

---

## NBH-xxx — Restauro identificado e TUI com Live Log (netbox-ops v0.3.5)

**Situação inicial:** a versão 0.3.4 não distinguia os backups de salvaguarda criados automaticamente antes de um restauro. Os cabeçalhos da tabela de backups estavam alinhados à esquerda. Na gestão de serviços, a área inferior era desperdiçada e as operações estavam acima do rodapé, sem pré-visualização contínua do journal nem opção de live log. O gráfico circular tinha legenda com cores inconsistentes face às fatias.

**Trabalho realizado:**
- Identificados os backups de segurança feitos durante restauro com o segmento `pre-restore` no nome e `purpose: pre_restore` no manifesto e no sidecar JSON, mantendo `purpose: manual` para backups normais.
- A listagem rápida identifica as salvaguardas apenas pelo nome, sem voltar a introduzir leitura pesada dos arquivos. O painel de detalhe e a tabela apresentam a proveniência; arquivos antigos não recebem etiquetas retroactivamente.
- Centrados os cabeçalhos de tipo, data/hora, tamanho, etiqueta e nome no respectivo campo da tabela de backups.
- Reorganizada a gestão de serviços: acções na barra inferior horizontal, painel de métricas no topo, journal no espaço inferior e Enter para expandir o painel de logs.
- Implementada a opção `Live log: ON/OFF` e tecla L, através de subprocesso journalctl --follow com leitura não bloqueante e buffer limitado. As consultas estáticas decorrem em worker para não bloquear navegação.
- Corrigidas as cores da legenda do donut para coincidirem com os segmentos: backups (teal), outros dados (azul) e espaço livre (cinzento).
- Mantidas as restrições de acções de serviços e as confirmações de restauro/apagamento. Actualizados PT-PT/EN, testes isolados e documentação.

**Motivo:** tornar inequívoca a origem dos backups de segurança, melhorar a leitura e utilização dos menus e permitir observar logs sem abandonar a gestão de serviços nem afectar a resposta do teclado.

**Validação:** testes automatizados isolados, verificação sintáctica e integridade do ZIP; validação visual e operacional em VM RHEL/Python 3.12 e ensaios reais de restauro pendentes. Não foi realizada qualquer alteração na VM durante a preparação.

---

## NBH-xxx — Gestão de backups com painel de detalhe permanente e operações horizontais (netbox-ops v0.3.4)

**Situação inicial:** na v0.3.3 a gestão de backups apresentava operações em posições fixas distribuídas por duas linhas e o Enter na lista abria um ecrã separado de detalhe. O painel da direita não resumia a selecção múltipla, aumentando a navegação necessária e a possibilidade de ambiguidade sobre os ficheiros afectados.

**Trabalho realizado:**
- Reorganizada a barra de operações de backup para ficar horizontal por baixo da listagem e do painel de detalhe, ajustando-se automaticamente à largura do terminal.
- Eliminado o ecrã de detalhe aberto por Enter; a pré-visualização está permanentemente disponível no painel direito para o ficheiro focado ou único ficheiro marcado.
- Adicionado resumo de selecção múltipla com quantidade, tipos e tamanho agregado, indicando que não é possível restaurar vários backups simultaneamente.
- Operações indisponíveis são apresentadas desactivadas, incluindo restauro de ficheiros legados/incompatíveis ou sem metadados prontos.
- Mantidas marcação com Espaço, selecção total, atalhos, barra de progresso, confirmações e verificações de integridade existentes.
- Corrigida a repetição de leitura da lista na acção F5, passando a ocorrer uma única actualização.
- Actualizados os ficheiros JSON de tradução PT-PT/EN, o README e os testes automatizados.
- Mantida a lógica de backup/restauro e o isolamento dos ambientes Python; nenhuma alteração efectuada na VM RHEL.

**Motivo:** proporcionar uma vista operacional de backups consistente com a gestão de serviços, reduzir ecrãs redundantes e evitar confusões entre o ficheiro focado e os ficheiros marcados para operações em lote.

**Validação:** 132 testes automatizados isolados com Python 3.13, incluindo navegação e operações TUI simuladas; validação visual e operações reais em RHEL/Python 3.12 ainda pendentes.

---

## NBH-xxx — Paleta NetBox Dark e conclusão integrada nas barras de progresso (netbox-ops v0.3.3)

**Situação inicial:** a v0.3.2 apresentava uma paleta de terminal pouco uniforme e um gráfico de armazenamento formado por pontos `●`, de contorno pouco circular. Após operações de serviços ou de backups, a TUI substituía o ecrã de progresso por outro ecrã com os resultados, duplicando informação.

**Trabalho realizado:**
- Adoptada a paleta A (NetBox Dark): fundo escuro, painéis diferenciados, destaques teal e cores reservadas para sucesso, falha e avisos.
- Implementada aproximação xterm-256 da paleta e fallback para terminais de oito cores; mantido suporte para `NO_COLOR`.
- Substituído o gráfico circular pontilhado por um donut de caracteres Unicode Braille com segmentos para backups, outros dados e espaço livre, e percentagem de backups ao centro.
- As operações de serviços mostram o resultado final no mesmo ecrã da barra e lista de tarefas, mantendo-o visível até Enter/Esc.
- Backups, restauros, validações e eliminações mantêm também o ecrã de progresso no final, incluindo caminho/resultado ou erro; deixou de existir a página redundante de resultados.
- Actualizadas traduções PT-PT/EN, testes isolados e documentação. Mantida a lógica de negócios existente, sem intervenções no NetBox nem nas configurações da VM.

**Motivo:** melhorar a consistência visual, legibilidade e experiência em terminais SSH e evitar navegação redundante após operações demoradas.

**Validação:** 118 testes automatizados isolados e teste de arranque em terminal emulado de 256 cores; validação do tema no cliente SSH e ensaios reais de backup/restauro na VM RHEL pendentes.

---

## NBH-xxx — TUI operacional e versões fiáveis (netbox-ops v0.3.2)

**Situação inicial:** a v0.3.1 apresentava `5.2.9` como se fosse a versão do NetBox, embora fosse a versão do Django. O detalhe de serviços não permitia consultar os logs, o menu de backups não suportava marcação múltipla e as barras de progresso não discriminavam suficientemente as tarefas.

**Trabalho realizado:**
- Corrigida a detecção da versão NetBox a partir de `release.yaml`, mantendo cache por execução e mostrando as versões também no dashboard.
- Introduzida marcação de proveniência da versão nos novos manifestos de backup; os anteriores passam a ser tratados como versão desconhecida, com confirmação extra de restauro.
- Adicionada consulta dos últimos logs ao ecrã completo de detalhe de serviços, com visualizador e escolha de quantidade, sem voltar a mostrar logs na pré-visualização operacional.
- Adicionado gráfico circular textual do armazenamento de backups, distinguindo backups, restantes dados do filesystem e espaço livre.
- Implementada selecção múltipla de backups, seleccionar todos, validar e apagar selecção, com barra de progresso; mantém-se o restauro individual e selectivo de `full`.
- Introduzida lista de tarefas com estado durante a barra de progresso de serviços, backups, restauros, validação e eliminação.
- Mantidos menu TUI PT/EN, CLI e política `db`/`media`/`full`.
- Actualizados testes e documentação, sem alterações ao NetBox da VM.

**Motivo:** corrigir informação de versão enganadora, melhorar a legibilidade operacional e reduzir risco de operações involuntárias em lote.

**Validação:** testes automatizados isolados e verificações sintácticas; validação em VM RHEL e ensaios de restauro reais pendentes.

---


## NBH-xxx — Optimização da TUI e cache de metadados (netbox-ops v0.3.1)

**Situação inicial:** a v0.3.0 apresentava os logs dos serviços na gestão operacional, operações nem sempre adequadas ao estado actual e a gestão de backups reabria os manifestos de arquivos comprimidos em cada movimento do cursor, com atraso significativo na navegação.

**Trabalho realizado:**
- Reorganizados os painéis do menu principal e da gestão de serviços, mantendo as operações próximas da lista.
- Substituídos os logs no painel de detalhe por CPU, RAM, uptime, PID, arranque e versão do componente.
- Introduzida consulta de versões uma vez por execução, de forma assíncrona e com cache; NetBox e NetBox RQ partilham a versão.
- As acções incompatíveis com o estado do serviço ficam desactivadas; nos lotes mistos apenas os serviços elegíveis avançam para confirmação.
- A listagem de backups tornou-se leve e independente de abertura de arquivos.
- Implementado cache de metadados por ficheiro e invalidação por alteração do ficheiro ou sidecar.
- Adicionados sidecars JSON aos novos backups de media e completos, com metadados de apresentação; a integridade continua a exigir verificação independente.
- Mantidos os três tipos de backups (`db`, `media` e `full`) e o restauro selectivo de pacotes completos.
- Adicionados testes automatizados de desempenho, versões, estado de serviços e metadados.

**Motivo:** melhorar a experiência de utilização via SSH, reduzir o trabalho de leitura sobre arquivos grandes e mostrar informação operacional relevante sem modificar o modelo de backup/restauro aprovado.

**Validação:** testes automatizados isolados concluídos; validação em RHEL e ensaio de restauro real pendentes.

---

# netbox-ops — v0.3.0

NBH-xxx — Reorganização da TUI e gestão unificada de backups

**Situação inicial:**
- A versão 0.2.0 apresentava os serviços e backups em opções separadas no menu principal.
- A visualização principal não utilizava o espaço livre para métricas.
- O backup de artefactos incluía por defeito configurações do NetBox e LDAP.
- O restauro de pacotes completos não permitia escolher BD ou media de forma independente.

**Trabalho realizado:**
- Implementado o painel de estado no menu principal com CPU/RAM por serviço e espaço ocupado/livre dos backups.
- Reorganizado o ecrã de gestão de serviços com multi-selecção, detalhes e operações permanentes no rodapé.
- Criado menu único de gestão de backups com listagem, metadados, criação, validação, restauro, eliminação individual e eliminação total.
- Limitado o backup de artefactos a imagens e ficheiros carregados em `media`, excluindo ficheiros de configuração e outros artefactos.
- Alterado o backup integrado para `BD + media`.
- Implementado restauro selectivo de backup completo: apenas BD, apenas media ou ambos.
- Adicionada barra de progresso por etapas nas operações de backup/restauro e confirmações destrutivas.
- Acrescentada compatibilidade de leitura da configuração anterior sem importar artefactos fora do novo âmbito.
- Mantidos os pacotes antigos com configurações como legados, sem suporte de restauro na nova versão.
- Conservada a paragem/arranque coordenados dos serviços NetBox durante o restauro e a preservação de Redis/RQ; limpeza selectiva de cache pendente de validação do ambiente.
- Actualizadas as traduções PT-PT e EN, documentação e testes automatizados.

**Motivo:**
- Melhorar a navegação e observabilidade operacional em terminal, reduzir risco de operações involuntárias e limitar o âmbito dos backups aos dados da aplicação definidos para a sprint.

**Validação operacional na VM RHEL:** pendente, especialmente os restauros PostgreSQL e a coerência da cache Redis.
