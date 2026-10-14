<!-- Cópia do documento claude/STATUS_ATUAL.md do projeto "Repo_Backup" no claude.ai. A versão principal é a do projeto; esta é atualizada por PR. Ver a seção 4 de INSTRUCOES_DO_PROJETO.md. -->

# Repo_Backup — Status atual (documento vivo)

**Última atualização:** 14/10/2026, 03h30 (horário de Brasília)
**Estado do `main` nesta data:** commit `7822242` (merge do PR #59, 14/10/2026), mais o merge deste PR de contexto. Nenhum outro PR aberto.
**Engine ativo:** a sessão claude.ai/code/session_01YHH3f36KATMnp9YNqn5g9E (Claude Code), retomada em 14/10. A sessão ligada ao projeto não opera: mantém os documentos do projeto e revisa PRs.
**Quem atualiza:** o engine (Claude), ao fim de cada sessão em que algo mudou. O ritual está em `claude/INSTRUCOES_DO_PROJETO.md`.

> Leia este documento primeiro em qualquer sessão nova. Ele diz onde o projeto está e o que vem depois. Ele **não autoriza nada**: toda execução exige ECR e GO do HITL na própria sessão, conforme o protocolo `BACKUP_REPOSITORY.md`.

---

## 1. O que é o projeto, em três linhas

Backup de repositórios do GitHub (conta `Moriblo`) para o OneDrive pessoal. Os executores são workflows do repositório `Moriblo/repo_backup`. O Claude atua como engine: apresenta o menu e o ECR, grava a linha no `commands.log` depois do GO e reporta o resultado. A origem é sempre somente leitura. O objetivo final é poder **restaurar**: um backup só está provado quando a restauração funciona.

## 2. Linha do tempo resumida

| Período | O que aconteceu |
|---|---|
| 26–28/09 | Fases 0 e 1 (matriz de capacidades e inventário do `Moriblo/MIT`) feitas no ChatGPT. Processo v1.11, matriz v1.10 e inventário v1.0. |
| 29/09 | Rota OneDrive decidida (pessoal, OAuth delegado, sem OIDC). Fluxo ECR → GO → `commands.log` → Dispatcher implementado. Primeiros runs reais com `Moriblo/Minha_Caixinha_de_Saude`. |
| 30/09–01/10 | Pacote restaurável provado. Restauração manual pelo `RESTAURAR.txt` provada, inclusive com LFS (`Moriblo/backup_teste`). |
| 02/10 | Etapa 2A: `labels`, `milestones` e `issues` lidas pela API e provadas em run real. |
| 03/10 | `projects` no `BKP_REPO` (com a regra E). `BKP_PROJ` nos três escopos. `BKP_ISSUES` com sub-issues e dependências. `RST_REPO` etapas 1, 2 e 3 provadas. |
| 04/10 | `RST_REPO` etapa 3b (relações) provada. Etapa 4 (Projects) implementada (PR #55), **sem run real**. Backup `req-20261004-006` gerado para essa prova. |
| 05–13/10 | Projeto parado. |
| 14/10 | `RST_REPO` etapa 4 (Projects): o `req-20261014-001` **falhou** (Auto-add sub-issues do Project novo pôs a filha antes da adição explícita: "Content already exists"); causa confirmada pelo HITL no Project nº 15. PRs #57 (log da falha sem títulos), #58 (tolera os workflows padrão do Project novo) e #59 (resumo de sucesso, tempos e texto do checklist) mergeados. O `req-20261014-002` **passou** e foi conferido contra os originais: etapa 4 provada em run real. |

## 3. Capabilities e o que está provado

| Capability | Mnemonic | Workflow | Estado |
|---|---|---|---|
| `backup_repository` | `BKP_REPO` | `backup-repository.yml` | **Provado em run real.** Git (bundle, LFS, submódulo só gitlink, arquivo ~25 MiB), labels, milestones, issues com sub-issues e dependências, Projects (parcial, com regra E). |
| `backup_projects` | `BKP_PROJ` | `backup-projects.yml` | **Provado** nos escopos `PROJECT`, `OWNER_PROJECT_SET` e `SOURCE_REPOSITORY`. Atenção: `OWNER_PROJECT_SET` grava **todos** os Projects do dono; avisar no ECR. |
| `backup_issues` | `BKP_ISSUES` | `backup-issues.yml` | **Provado**, inclusive relações. Um repositório por linha. |
| `restore_repository` | `RST_REPO` | `restore-repository.yml` | Etapas 1 (git), 2 (labels e milestones), 3 (issues e comentários), 3b (sub-issues e dependências) e **4 (Projects)** **provadas em run real** (a 4 no `req-20261014-002`). **Ressalva:** o tratamento dos workflows padrão do Project novo (4b: item "já existe" por Auto-add sub-issues, reposicionamento, Done retido em issue aberta, extras aceitos) tem prova **só em teste local**; no `-002` o `workflow_effects` veio vazio. Sempre cria repositório novo e privado. |
| `list_capabilities`, `show_status`, `validate_evidence`, `help` | — | — | Informativas, respondidas na conversa. |

**Classes:** 5 de 18 implementadas no `BKP_REPO` (`git`, `labels`, `milestones`, `issues`, `projects`). As 13 restantes entram em cada execução como restrições aceitas (`NOT-VERIFIED`): `pull_requests`, `actions`, `checks`, `actions_caches`, `environments`, `variables`, `secret_metadata`, `deployments`, `rules_and_branch_protection`, `collaborators`, `webhooks`, `codespaces_repository_state_delta`, `other_discovered_state`.

**Ainda não provado em run real (lista principal):** repositórios grandes e limite de taxa, conteúdo de submódulos, nomes com acento no OneDrive, reações, pull requests, Projects de organização, recusas do `RST_REPO` (alvo existente, token inválido), saída 48 por falha de escrita. A lista completa por capability está na seção I.14 do `BACKUP_REPOSITORY.md`.

## 4. Repositórios, pastas e execuções de referência

| Item | Valor |
|---|---|
| Repositório executor | `Moriblo/repo_backup` (**público** hoje; decisão de 14/10: tornar **privado**, mas só depois de resolver a pré-condição dos segredos de environment, ver §5 e I.15) |
| Environment dos segredos | `onedrive-backup` |
| Destino no OneDrive | `Apps/GitHub repo_backup/<destination>/<request_id>/{package,evidence}/` |
| Repositórios de teste | `Moriblo/backup_teste` (LFS, submódulo, tags), `Moriblo/backup_teste_issues` (privado; issues, relações, Project nº 13 e nº 14 "Teste restore") |
| Repositórios criados pelas restaurações de teste | `Moriblo/backup_teste_issues-restaurado-2` a `-restaurado-7` (todos privados; **a apagar pelo HITL**, decisão de 14/10; a evidência fica no OneDrive). O `-restaurado-6` é um alvo **parcial** (run `-001` falhou na etapa 4) e o `-restaurado-7` é o da prova da etapa 4 |
| Projects criados pelas restaurações de teste | nº 15 (**incompleto**, do `-001`, ligado ao `-restaurado-6`), nº 16 e nº 17 (do `-002`, ligados ao `-restaurado-7`; cópias de #13 e #14). A apagar pelo HITL (decisão de 14/10) |
| Último backup completo | `req-20261004-006`, `backups/backup_teste_issues/req-20261004-006`, run `37184520042`, todos os passos com sucesso |
| Última restauração provada | `req-20261014-002` (etapa 4), run `38515538532`, alvo `backup_teste_issues-restaurado-7`; as cinco classes `OK`, 55 escritas de 72 planejadas, Projects nº 16 e nº 17 conferidos contra os originais pelo HITL. Antes: `req-20261003-019` (etapa 3b) |
| Restauração que falhou (causa conhecida) | `req-20261014-001`, run `38507996550`, alvo `-restaurado-6`, Project nº 15: `RstAddIssueItem` "Content already exists in this project" (corrigido no PR #58) |
| Próximo `request_id` livre | `req-20261014-003` em diante, conforme a data do GO (confirme no `commands.log` do `main`) |
| Repositório-alvo original das Fases 0 e 1 | `Moriblo/MIT`, **ainda sem backup real** |

## 5. Próximos passos (em ordem)

1. **(feito em 14/10)** Provar a etapa 4 (Projects) do `RST_REPO` em run real: `req-20261014-002`. Opcional, sem pressa: um run que **force a corrida** (pai antes da filha, com o Auto-add sub-issues ganhando) para provar em run real o caminho "já existe", o reposicionamento e os extras aceitos do 4b. O `-002` não os exercitou.
2. **Classes temporais**, prioridade do protocolo (II.12): `actions` (runs, jobs, logs, artifacts), `checks`, `actions_caches` e entregas de `webhooks`. A retenção do GitHub mudou em 01/10/2026.
3. **Classe `pull_requests`** (PRs, reviews, threads, comentários), a última do nível 3.
4. **Primeiro backup real do `Moriblo/MIT`** com `BKP_REPO` e as restrições aceitas.
5. **Higiene e riscos:**
   - (feito, PR de higiene de 14/10) `runs-on: ubuntu-24.04` fixo em todos os workflows (o `ubuntu-latest` migra para o Ubuntu 26 em 19/10/2026) e actions em Node 24: `checkout@v5`, `upload-artifact@v6`, `create-github-app-token@v3`. Rodar um backup curto depois do merge confirma. Pendente, sem pressa: trocar `app-id` por `client-id` (deprecado) e avaliar `checkout`/`upload-artifact` v7.
   - Validade real do **refresh token do OneDrive** (cerca de 90 dias sem uso, a confirmar; o App Writer o regrava a cada run) e plano de renovação se expirar (`onedrive_authorize.py`). (Veio da issue #7, fechada em 14/10.)
   - Apagar do OneDrive, se o HITL não quiser a cópia, os dados de Projects alheios gravados pelo run `req-20261003-002` (`api-project-items.json` e `api-projects.json` em `backups/backup_teste_issues/req-20261003-002/package/`). (Veio da issue #7.)
   - Repositório grande (limite de 120 min do job e disco do runner) e nomes com acento no OneDrive: medir ou aceitar como limite. (Veio da issue #7.)
   - Conferir a validade do `SOURCE_READ_TOKEN`, do `PROJECTS_READ_TOKEN` e do `RESTORE_WRITE_TOKEN` (clássico, com `repo` e `workflow`, amplo). Avaliar revogar este último entre os testes.
   - **`repo_backup` privado (decisão do HITL de 14/10), com uma pré-condição.** No plano gratuito, os segredos de **environment** são ignorados em repositório privado, e todos os workflows usam `environment: onedrive-backup`: tornar privado agora **interrompe o sistema**. Resolver antes (decisão de projeto + PR próprio): mover os segredos para segredos de repositório (e mudar onde o App Writer regrava o refresh token), ou assinar o Pro. Consequências aceitas: sem proteção de branch (o check "Registry traceability" vira só detectivo) e os minutos de Actions entram na cota do plano. Ver I.15.
   - Apagar as variáveis `AZURE_CLIENT_ID` e `AZURE_TENANT_ID` e o app Entra antigo "Repository Preservation OneDrive Writer".
   - (feito em 14/10, sem PR) As issues #5, #7 e #11 foram atualizadas ou fechadas conforme o estado atual (ver o comentário de cada uma).
   - O HITL apaga os repositórios `-restaurado-2` a `-restaurado-7` e os Projects nº 15, 16 e 17 (decisão de 14/10).
   - (feito no PR #58) O comentário de cabeçalho do `restore-repository.yml` foi corrigido.
   - Ajustes manuais do checklist da prova: desligar o *Pull request merged* do Project nº 17; configurar o filtro e ligar o *Auto-add to project* (a API não o lista, mas ele existe na aba Workflows).

## 6. Registro de decisões do HITL

| Data | Decisão |
|---|---|
| 29/09 | OneDrive **pessoal** com OAuth **delegado** (`Files.ReadWrite.AppFolder`, app público, device code). Sem OIDC nem app-only: o Graph não aceita token app-only em OneDrive pessoal. |
| 29/09 | Código só entra por **pull request** revisado pelo HITL. Escrita direta no `main` só no `commands.log`, e só depois do GO. |
| 29/09 | O ECR é definido nos `inputs` do `workflow_call` de cada workflow. O engine o apresenta exatamente como está. |
| 29/09 | Cada GO gera uma linha JSON no `commands.log`. O `request_id` percorre ECR, linha, run e pasta no OneDrive. |
| 29/09 | Restituição ao GitHub só por capability própria (`restore_repository`), com Mnemonic, ECR e GO próprios. O backup nunca escreve na origem. |
| 29/09 | Check `Registry traceability` exigido em `main`, sem exigir PR e sem "Do not allow bypassing" (o admin precisa gravar o `commands.log`). |
| 02/10 | Etapa 2 começou por `labels`, `milestones` e `issues`, depois `projects`, porque o repositório de teste já estava pronto. As temporais vêm em seguida. |
| 02–03/10 | `projects` com token **clássico** `read:project`. O escopo `repo` **não** é dado ao token de leitura. Regra E aprovada. Mnemonic `BKP_PROJ` aprovado. |
| 03/10 | `BKP_ISSUES`: um repositório por linha, de propósito (lição do `OWNER_PROJECT_SET`). |
| 03/10 | `RST_REPO` cria sempre repositório **novo e privado**, nunca escreve em repositório existente e nunca apaga. `RESTORE_WRITE_TOKEN` clássico com `repo` e `workflow`. |
| 03/10 | `data_backup_path` (restaurar dados de outro backup) foi **adiado** até haver necessidade. |
| 04/10 | Restauração de Projects cria sempre um Project **novo**. `copyProjectV2` não é usado (hipótese não provada). |
| 13/10 | Contexto do projeto consolidado no projeto do Claude, com instruções de continuidade (`claude/INSTRUCOES_DO_PROJETO.md`). |
| 14/10 | Engine volta a ser a sessão 01YHH... do Claude Code; a sessão ligada ao projeto só mantém os documentos e revisa PRs. O ECR req-20261013-001 montado na sessão do projeto foi cancelado sem gravação. Contexto copiado para docs/contexto/ (PR #56). |
| 14/10 | **Higiene:** o HITL apaga os repositórios `-restaurado-2` a `-restaurado-7` e os Projects nº 15, 16 e 17 (evidência fica no OneDrive). Todos os workflows com `runs-on: ubuntu-24.04` e actions em Node 24 (PR de higiene). |
| 14/10 | **`repo_backup` privado** (plano gratuito): aceitas a perda da proteção de branch (o check "Registry traceability" vira só detectivo) e a entrada dos minutos de Actions na cota. A regra de log sem títulos e nomes continua como **boa prática**. **Pré-condição levantada pelo engine:** environments e seus segredos são ignorados em repositório privado no plano Free; resolver antes de tornar privado (I.15). |
| 14/10 | Etapa 4: um **item a mais** no Project restaurado só é aceito se for **sub-issue de uma issue restaurada que está no Project** (adicionado pelo Auto-add sub-issues); fica, é listado com o motivo e a classe vira `PARTIAL`. Qualquer outro item inesperado é `FAILED`. Vale também para o extra que só aparece na conferência final. |
| 14/10 | Etapa 4: **Status Done não é gravado** numa issue aberta no backup (o Auto-close a fecharia), só no campo `Status`; `PARTIAL`. O restore nunca muda o estado de uma issue por causa de um workflow do Project. |
| 14/10 | Etapa 4: o **reposicionamento** dos itens é tentado (`updateProjectV2ItemPosition`); se falhar, `PARTIAL` com nota. O restore não apaga os workflows padrão do Project novo (irreversível e contradiz restaurar o estado deles). |
| 14/10 | **Log do Actions:** o repositório executor era **público**, então nenhum texto que vá para o log traz títulos, nomes de labels, campos, views ou workflows, nem valores. Divergências usam posição ("campo 2"), o erro da API é cortado na primeira aspa, e o detalhe completo fica no `restore-result.json`. Em sucesso, o log traz `RESTORE_SUMMARY` por classe e `RESTORE_TIMINGS`. |
| 14/10 | Um workflow do backup que a API não lista no Project restaurado **não** é "inexistente" (o *Auto-add to project* existe na aba Workflows e a API não o devolve): o checklist diz "não listado pela API; configure o filtro e ligue à mão". |
| 14/10 | PRs em sequência, mergeados pelo HITL: 4a (#57) depois 4b (#58) depois 4c (#59). O HITL aceitou, antes de cada run, o `PARTIALLY-PRESERVED` dos Projects, os extras dos workflows, o Done retido e o alvo parcial mantido em caso de falha. |

## 7. Sessões do Claude que operaram o projeto

| Sessão | Período | Trabalho |
|---|---|---|
| claude.ai/code/session_01R9yS4DP5NjxYb5wy4Wt31D | 29/09–02/10 | Fluxo ECR/GO/`commands.log`/Dispatcher, primeiros backups reais, pacote restaurável, LFS |
| claude.ai/code/session_01YHH3f36KATMnp9YNqn5g9E | 02/10–14/10 | Etapa 2A, Projects, `BKP_PROJ`, `BKP_ISSUES`, `RST_REPO` etapas 1 a 4; em 14/10: `BKP_REPO` `-005`/`-006` e `RST_REPO` `-001`/`-002`, PRs #55 a #59 e este contexto |
| Sessão de 13/10 (ligada ao projeto Repo_Backup) | 13/10– | Consolidação do contexto no projeto, status e instruções |

**O que cada sessão consegue ler do Actions (verificado em 14/10 numa sessão do Claude Code):** lê o repositório, a API do GitHub (runs, passos, issues, PRs), grava o `commands.log` e abre PRs, e **baixa o log de um job** (`get_job_logs`, inclusive o `BKP_RESULT` e as linhas `RESTORE_*`). **Não** baixa os **artefatos** do Actions: o link temporário aponta para o armazenamento da Microsoft e o proxy da sessão o bloqueia (403). Para ler o `restore-result.json` ou a evidência completa, o HITL abre o artefato (ou a pasta no OneDrive) e cola o trecho. Não tente contornar o bloqueio. (A versão anterior deste parágrafo dizia que nem os logs de job eram baixáveis; isso vale só para a sessão ligada ao projeto, que usa outro conjunto de ferramentas.) Desde o PR #57 o log traz o motivo das falhas e, desde o #59, o resumo de cada classe, então o artefato raramente é necessário.

**Regra: um só engine por vez.** Duas sessões operando juntas arriscam `request_id` repetido e runs simultâneos, que invalidam o refresh token do OneDrive.

## 8. Pendências de contexto

- A sincronização do GitHub configurada no projeto **não alimentava a busca** na verificação de 13/10/2026. Por isso os arquivos principais do repositório são espelhados no projeto. Se a sincronização voltar a funcionar, os espelhos podem ser dispensados.
- Os chats do ChatGPT das Fases 0 e 1 e as sessões do Claude de 29/09 a 04/10 não estão no projeto. O que importava deles está no repositório, nas issues e nos documentos de fase já salvos.
