<!-- Cópia do documento claude/STATUS_ATUAL.md do projeto "Repo_Backup" no claude.ai. A versão principal é a do projeto; esta é atualizada por PR. Ver a seção 4 de INSTRUCOES_DO_PROJETO.md. -->

# Repo_Backup — Status atual (documento vivo)

**Última atualização:** 13/10/2026, 23h30 (horário de Brasília)
**Estado do `main` nesta data:** commit `bee0816` (04/10/2026). Nenhum PR aberto e nenhuma branch pendente.
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

## 3. Capabilities e o que está provado

| Capability | Mnemonic | Workflow | Estado |
|---|---|---|---|
| `backup_repository` | `BKP_REPO` | `backup-repository.yml` | **Provado em run real.** Git (bundle, LFS, submódulo só gitlink, arquivo ~25 MiB), labels, milestones, issues com sub-issues e dependências, Projects (parcial, com regra E). |
| `backup_projects` | `BKP_PROJ` | `backup-projects.yml` | **Provado** nos escopos `PROJECT`, `OWNER_PROJECT_SET` e `SOURCE_REPOSITORY`. Atenção: `OWNER_PROJECT_SET` grava **todos** os Projects do dono; avisar no ECR. |
| `backup_issues` | `BKP_ISSUES` | `backup-issues.yml` | **Provado**, inclusive relações. Um repositório por linha. |
| `restore_repository` | `RST_REPO` | `restore-repository.yml` | Etapas 1 (git), 2 (labels e milestones), 3 (issues e comentários) e 3b (sub-issues e dependências) **provadas em run real**. Etapa 4 (Projects) **implementada, só teste local**. Sempre cria repositório novo e privado. |
| `list_capabilities`, `show_status`, `validate_evidence`, `help` | — | — | Informativas, respondidas na conversa. |

**Classes:** 5 de 18 implementadas no `BKP_REPO` (`git`, `labels`, `milestones`, `issues`, `projects`). As 13 restantes entram em cada execução como restrições aceitas (`NOT-VERIFIED`): `pull_requests`, `actions`, `checks`, `actions_caches`, `environments`, `variables`, `secret_metadata`, `deployments`, `rules_and_branch_protection`, `collaborators`, `webhooks`, `codespaces_repository_state_delta`, `other_discovered_state`.

**Ainda não provado em run real (lista principal):** repositórios grandes e limite de taxa, conteúdo de submódulos, nomes com acento no OneDrive, reações, pull requests, Projects de organização, recusas do `RST_REPO` (alvo existente, token inválido), saída 48 por falha de escrita. A lista completa por capability está na seção I.14 do `BACKUP_REPOSITORY.md`.

## 4. Repositórios, pastas e execuções de referência

| Item | Valor |
|---|---|
| Repositório executor | `Moriblo/repo_backup` (**público**) |
| Environment dos segredos | `onedrive-backup` |
| Destino no OneDrive | `Apps/GitHub repo_backup/<destination>/<request_id>/{package,evidence}/` |
| Repositórios de teste | `Moriblo/backup_teste` (LFS, submódulo, tags), `Moriblo/backup_teste_issues` (privado; issues, relações, Project nº 13 e nº 14 "Teste restore") |
| Repositórios criados pelas restaurações de teste | `Moriblo/backup_teste_issues-restaurado`, `-restaurado-2` a `-restaurado-5` (todos privados; candidatos a limpeza pelo HITL) |
| Último backup completo | `req-20261004-006`, `backups/backup_teste_issues/req-20261004-006`, run `37184520042`, todos os passos com sucesso |
| Última restauração provada | `req-20261003-019` (etapa 3b), run `37167245637`, alvo `backup_teste_issues-restaurado-5` |
| Próximo `request_id` livre | `req-20261013-001` em diante, conforme a data do GO (confirme no `commands.log` do `main`) |
| Repositório-alvo original das Fases 0 e 1 | `Moriblo/MIT`, **ainda sem backup real** |

## 5. Próximos passos (em ordem)

1. **Provar a etapa 4 (Projects) do `RST_REPO` em run real.** Fonte: `backups/backup_teste_issues/req-20261004-006`. Alvo sugerido: `backup_teste_issues-restaurado-6`. Antes do GO, confirmar que o `RESTORE_WRITE_TOKEN` tem o escopo `project`. Depois, conferir o quadro restaurado na interface e registrar a prova na seção I.14 por PR.
2. **Classes temporais**, prioridade do protocolo (II.12): `actions` (runs, jobs, logs, artifacts), `checks`, `actions_caches` e entregas de `webhooks`. A retenção do GitHub mudou em 01/10/2026.
3. **Classe `pull_requests`** (PRs, reviews, threads, comentários), a última do nível 3.
4. **Primeiro backup real do `Moriblo/MIT`** com `BKP_REPO` e as restrições aceitas.
5. **Higiene e riscos:**
   - A partir de **19/10/2026**, o `ubuntu-latest` migra para Ubuntu 26. Rodar um backup curto depois dessa data ou fixar `ubuntu-24.04`.
   - Aviso de Node 20 descontinuado em `actions/checkout@v4`, `actions/create-github-app-token@v2` e `actions/upload-artifact@v4`.
   - Conferir a validade do `SOURCE_READ_TOKEN`, do `PROJECTS_READ_TOKEN` e do `RESTORE_WRITE_TOKEN` (clássico, com `repo` e `workflow`, amplo). Avaliar revogar este último entre os testes.
   - Decidir se o `repo_backup` deve virar privado.
   - Apagar as variáveis `AZURE_CLIENT_ID` e `AZURE_TENANT_ID` e o app Entra antigo "Repository Preservation OneDrive Writer".
   - Atualizar ou fechar as issues #5, #7 e #11, paradas desde 03/10 e com partes já superadas.
   - Limpar os repositórios `-restaurado*` de teste, quando o HITL decidir.
   - Corrigir, por PR, o comentário de cabeçalho do `restore-repository.yml`. Ele ainda diz que Projects **não** são restaurados, mas a etapa 4 foi mergeada no PR #55. O engine pode apresentar um ECR com a descrição errada.

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

## 7. Sessões do Claude que operaram o projeto

| Sessão | Período | Trabalho |
|---|---|---|
| claude.ai/code/session_01R9yS4DP5NjxYb5wy4Wt31D | 29/09–02/10 | Fluxo ECR/GO/`commands.log`/Dispatcher, primeiros backups reais, pacote restaurável, LFS |
| claude.ai/code/session_01YHH3f36KATMnp9YNqn5g9E | 02/10–04/10 | Etapa 2A, Projects, `BKP_PROJ`, `BKP_ISSUES`, `RST_REPO` etapas 1 a 4 |
| Sessão de 13/10 (ligada ao projeto Repo_Backup) | 13/10– | Consolidação do contexto no projeto, status e instruções |

**Limitação da sessão ligada ao projeto (verificada em 14/10):** lê o repositório, a API do GitHub (runs, passos, issues, PRs), grava o `commands.log` e abre PRs. **Não** baixa logs de job nem artefatos do Actions: ficam num armazenamento da Microsoft que o proxy da sessão bloqueia (403). Para ler o `BKP_RESULT` ou a evidência, usar o navegador no computador do HITL (o engine pode abrir a página do run) ou o próprio HITL. A situação dos passos de cada job é visível pela API.

**Regra: um só engine por vez.** Duas sessões operando juntas arriscam `request_id` repetido e runs simultâneos, que invalidam o refresh token do OneDrive.

## 8. Pendências de contexto

- A sincronização do GitHub configurada no projeto **não alimentava a busca** na verificação de 13/10/2026. Por isso os arquivos principais do repositório são espelhados no projeto. Se a sincronização voltar a funcionar, os espelhos podem ser dispensados.
- Os chats do ChatGPT das Fases 0 e 1 e as sessões do Claude de 29/09 a 04/10 não estão no projeto. O que importava deles está no repositório, nas issues e nos documentos de fase já salvos.
