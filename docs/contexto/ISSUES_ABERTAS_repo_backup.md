<!-- Cópia do documento claude/ISSUES_ABERTAS_repo_backup.md do projeto "Repo_Backup" no claude.ai. A versão principal é a do projeto; esta é atualizada por PR. Ver a seção 4 de INSTRUCOES_DO_PROJETO.md. -->

# Issues abertas do repo_backup (cópia de 13/10/2026)

> Cópia fiel do corpo das issues abertas de `Moriblo/repo_backup`, que registram decisões do HITL fora dos arquivos do repositório. Nenhuma tem comentários. A fonte é sempre o GitHub: se a issue mudar, esta cópia fica velha. Atualize-a com o ritual descrito em `claude/INSTRUCOES_DO_PROJETO.md`.

---

## Issue #5: Método de autorização de engine para realização de backup

*Aberta em 2026-09-29, atualizada em 2026-09-29, estado open.*

## Contexto
O `dispatcher.yml` (PR 4) valida o conteúdo do `commands.log`, mas não restringe **quem** grava o log. Qualquer conta com permissão de escrita no repositório pode acrescentar uma linha e disparar um backup. Este item define o método para autorizar engines a gravar comandos.

**Decisão de escopo:** fica para depois de validarmos o backup em si, em PR separado, para não somar uma variável de teste e de troubleshooting agora.

## Desenho proposto (rascunho)
- **Base da checagem:** `github.actor` do push, que o GitHub autentica. Autor/committer dos commits e qualquer campo dentro da linha do log são forjáveis e só servem como informação.
- **Lista de escritores autorizados**, versionada (ex.: `backup/authorized_writers.yaml`), com por engine: `engine_id`, `actors` (logins), `mnemonics` permitidos, `status` (`ACTIVE`/`REVOKED`) e `note`, além de `enforcement: report | enforce`.
- **A lista é lida do commit anterior ao push (`before`)**, nunca do novo, para um push não se autorizar editando a própria lista. Ela só muda por pull request, com merge do dono.
- **Novo campo `engine_id` obrigatório** na linha do `commands.log` (`commands-log-line.schema.yaml`).
- **`dispatch_check.py`** passa a verificar, por linha nova: actor na lista; `engine_id` da linha coerente com o actor; Mnemonic permitido para aquele engine. Divergência = violação (saída 30) e nada é despachado.
- **Rollout:** começar com `enforcement: report` e `Moriblo` como login provisório; o primeiro push só registra o actor no log do job; depois trocar para `enforce` por PR.
- **Testes a cobrir:** actor não autorizado, engine `REVOKED`, Mnemonic fora da lista, `engine_id` que não bate com o actor.

## Questão em aberto: como autorizar um engine novo
Duas camadas: (1) permissão de escrita no GitHub e (2) entrada na lista, por PR.
- **Opção A: GitHub App por engine** (Contents: write só neste repositório; actor `nome[bot]`). Recomendada em segurança, mas exige reconfigurar as restrições a cada engine novo.
- **Opção B: um único App de escrita**, restrito a este repositório, reaproveitado por vários engines. Menos configuração, isolamento menor entre engines.
- **Opção C: conta/colaborador** por engine. Mais simples, com permissões mais amplas.
A escolha ficou em aberto e deve ser decidida ao retomar este item.

## Limites conhecidos
- O controle é **detectivo**: o push chega ao `main`, o dispatcher recusa e nada executa. A linha recusada permanece no log (append-only) e o `request_id` fica queimado.
- Bloqueio **preventivo** (regras de push restrito em Settings > Rules) precisa ser verificado: não sabemos se está disponível para repositório pessoal.

## Critérios de aceite
- [ ] Decisão sobre a opção A, B ou C registrada aqui.
- [ ] Arquivo da lista de escritores, comentado no padrão do repositório, mais o registro no `capabilities.yaml`.
- [ ] Schema, script e testes atualizados; PR aprovado antes de qualquer criação.
- [ ] Primeiro ciclo em `report`, depois `enforce`.

---

## Issue #7: Solicitação de ajustes SA-01 a SA-08: documentação, comentários, registro de artefatos e PR do OneDrive

*Aberta em 2026-09-29, atualizada em 2026-10-03, estado open.*

> **Revisão 6 (03/10/2026).** SA-01 a SA-08 foram implementados e mesclados; o fluxo foi provado em runs reais e a restauração do nível 1 foi testada. A **Etapa 2A** (`labels`, `milestones`, `issues`) e a **Etapa 2B, parte A** (`projects`, de forma parcial) foram entregues e **provadas em run real** (PRs #28 a #33). O que resta (`BKP_PROJ`, restauração como capability e as demais classes) é acompanhado na issue **#11**; a autorização de engine, na **#5**. Esta issue fica aberta só para as pendências da seção 8 e pode ser fechada quando elas forem resolvidas ou movidas.
> Revisões anteriores: 5 (Etapa 2A provada), 4 (estado após SA-08), 3 (objetivo de restaurar), 2 (índice no topo).

## Índice

| ID | Ajuste | PR | Prova de conclusão | Estado |
|---|---|---|---|---|
| SA-01 | Atualizar `BACKUP_REPOSITORY.md` (guia operacional em pt-BR) | PR-DOC (#10) | Revisão do HITL | ☑ |
| SA-02 | Comentar schemas antigos e blocos do `capabilities.yaml` | PR-COMENTÁRIOS (#9) | YAML carregado idêntico | ☑ |
| SA-03 | Comentários internos em `bkp_repo.py` e `dispatch_check.py` | PR-COMENTÁRIOS (#9) | AST idêntica | ☑ |
| SA-04 | Fonte única para o workflow do Mnemonic | PR-REGISTRO | `dispatch_check.py` falha se houver divergência | ☑ |
| SA-05 | `current_sha` e script de rastreabilidade | PR-REGISTRO | `validate_traceability.py --check` limpo; `traceability.yml` roda em todo PR | ☑ |
| SA-06 | Critério de `VALIDATED` com evidência de run | PR-REGISTRO | Estados reclassificados com URL do run e commit | ☑ |
| SA-07 | Remover valores obsoletos e redundâncias | PR-REGISTRO | YAML sem obsoletos; script limpo | ☑ |
| SA-08 | PR do OneDrive (pacote restaurável e teste de restauração) | PR-ONEDRIVE | Backup real com evidência e manifest válidos e restauração de teste | ☑ |

Sequência dos PRs (D5): PR-DOC → PR-COMENTÁRIOS → PR-REGISTRO → PR-ONEDRIVE. Cumprida.

## 1. Regras de trabalho acordadas
- Tudo que for criado passa pela aprovação do HITL antes de ser escrito; código só por pull request.
- Escrita direta no `main` **somente** no `commands.log` e **somente** após GO.
- A origem do backup é sempre **somente leitura**.
- OneDrive por OAuth **delegado** (`Files.ReadWrite.AppFolder`), **sem OIDC**.
- Todos os arquivos (`yaml`, `yml`, `py`) muito bem comentados, em português. Alterações só de comentário são provadas comparando o YAML carregado / a AST.
- Método de autorização de engine: issue #5 (fora deste escopo).
- **O objetivo do backup é restaurar.** Um backup só está provado quando uma restauração funciona (#11).

## 2. Estado validado até aqui
**Fluxo real, de ponta a ponta** (`commands.log` → Dispatcher → `BKP_REPO` → preflight → decisão do HITL → destino → mirror → pacote → evidência no OneDrive):
- 29/09: `req-20260929-001` (preflight `BLOCKED`, 17 restrições) e `req-20260929-002` (destino falha fechado, saída 20), os dois primeiros runs reais.
- 29/09 a 30/09: `req-20260929-003` a `-007` e `req-20260930-001`/`-002`, com `Moriblo/Minha_Caixinha_de_Saude`, ajustando a rotação do token pelo App Writer, o checkout sem credenciais persistidas e o instantâneo legível (zip).
- 30/09: o **pacote restaurou para um repositório novo** (bundle verificado, refs iguais ao `refs.tsv`, arquivos iguais ao zip). O `RESTAURAR.txt` foi seguido literalmente, sem problemas (relato do HITL).
- 01/10: `req-20261001-001` com `Moriblo/backup_teste` (arquivo de ~25 MiB, Git LFS, submódulo, 2 branches, 2 tags): hash por `quickXorHash` e restauração com LFS. `git` fica `PARTIALLY-PRESERVED` (do submódulo só o gitlink).
- 02/10: `req-20261002-001` com `Moriblo/backup_teste_issues` (privado), **Etapa 2A**: `labels` 4, `milestones` 1, `issues` 6, 5 comentários; `api-issues.json` conferido pelo HITL.
- 03/10, **Etapa 2B, parte A (`projects`)**, três runs reais com `backup_teste_issues`:
  - `req-20261003-001`: **falhou**. O token clássico `read:project` não enxerga o repositório privado e o GraphQL devolve `null` no lugar dos repositórios ligados; o leitor estourou e derrubou o job antes do pacote (corrigido no PR #31: exceção vira `FAILED` sem derrubar o job).
  - `req-20261003-002`: funcionou, mas incluiu às cegas 3 Projects (2 alheios, 108 itens).
  - `req-20261003-003` ([37089900544](https://github.com/Moriblo/repo_backup/actions/runs/37089900544)): **regra E** (PR #32). Quadro nº 13 confirmado por 6/6 títulos (7 itens, 14 campos, Status e Prioridade conferidos pelo HITL); nº 12 e nº 5 excluídos e fora do pacote; `PARTIALLY-PRESERVED` (referência das issues privadas oculta; o estado do quadro sobrevive). `COMPLETE_WITH_EXCEPTIONS`, 13 restrições.

**Entregue no SA-08:** `onedrive.py`, `onedrive_authorize.py`, destino validado de verdade, comparação de refs, pacote restaurável (bundle, LFS, lista de refs, zip), `evidence.json` e `manifest.json` nos schemas 2.0, bloco `onedrive`. Órfão OIDC e `diagnostic-git-read.yml` removidos.

**Entregue na Etapa 2A (PR #28/#29):** `github_api_read.py` (só GET, paginação, repetição, conferências), `SOURCE_READ_TOKEN` com *Issues: Read*, 10 testes locais.

**Entregue na Etapa 2B-A (PR #30/#31/#32/#33):** `github_projects_read.py` (GraphQL, só consultas, três escopos, conferências, itens ocultos = `PARTIAL`, regra E), credencial própria por classe (`class_credentials`, secret `PROJECTS_READ_TOKEN`: token **clássico** com `read:project`, porque token fine-grained não alcança Projects v2 de conta pessoal), 28 testes locais. O registro da prova está no PR #33.

**Adaptações ao setup real:** environment `onedrive-backup`; leitura da origem por `SOURCE_READ_TOKEN` (fine-grained, somente leitura); App Writer **opcional**; token clássico `read:project` só para `projects` (sem `repo`, por decisão de segurança).

**Não provado:** repositórios muito grandes (limite de 120 min do job e disco do runner), conteúdo de submódulos, nomes com acento no OneDrive, reações e relações entre issues, pull requests, restauração pela API; e, em `projects`: falso positivo por título repetido, origem sem issues, Project só com rascunhos, os escopos `PROJECT` e `OWNER_PROJECT_SET` (`BKP_PROJ`), Projects de organização, campos de iteração, data e número, itens arquivados e status updates.

**Outros:** actor do push do engine atual: `Moriblo`. Repositório de teste da Etapa 2: `Moriblo/backup_teste_issues` (privado) e quadro nº 13 (https://github.com/users/Moriblo/projects/13).

## 3. Decisões do HITL

| ID | Decisão |
|---|---|
| D1 | O documento `BACKUP_REPOSITORY.md` inteiro é em **português** (Operação, Protocolo normativo, Glossário). |
| D2 | `current_sha` conferido por **script**, não à mão. |
| D3 | `VALIDATED` só com **evidência registrada** (URL do run e commit). |
| D4 | Solicitação registrada como issue, com todos os detalhes. |
| D5 | Ordem dos PRs: PR-DOC → PR-COMENTÁRIOS → PR-REGISTRO → PR-ONEDRIVE. |
| D11 | A Etapa 2 começa por `labels`, `milestones` e `issues`, em JSON bruto da API. |
| D12 | `projects` em dois PRs: A (leitor e `BKP_REPO`) e B (`BKP_PROJ`). Mnemonic `BKP_PROJ`. |
| D13 | Token **clássico** com `read:project` para `projects`; o escopo `repo` **não** é concedido (daria escrita em todos os repositórios). |
| D14 | Regra E: vínculo oculto confirmado pelos títulos das issues da origem; candidato não confirmado é excluído do pacote. |

D6 a D10 (pendentes do PR-REGISTRO) foram tratadas no PR-REGISTRO; a regra final está no `capabilities.yaml` e no guia.

## 4. Achados da auditoria (`main` em `53568e6`): todos resolvidos
Comentários nos arquivos sem cobertura (SA-02, SA-03); workflow do Mnemonic em 5 lugares (SA-04); `current_sha` faltando e órfão OIDC (SA-05, SA-08); critério de `VALIDATED` (SA-06); `historical_existence` obsoleto (SA-07); bloco `onedrive` ausente (SA-08). `implemented_classes` é a fonte única do estado "classe implementada".

## 5. Ajustes
Todos concluídos, com as provas descritas no índice. Detalhes no `BACKUP_REPOSITORY.md` e no histórico dos PRs #9 a #33.

## 6. Fluxo operacional
Documentado no `BACKUP_REPOSITORY.md` (Parte I). Não é repetido aqui.

## 7. Desenho do OneDrive (como ficou)
- **Autenticação:** app público no Entra, sem client secret e sem tenant ID, autoridade `https://login.microsoftonline.com/consumers`; destino em `/me/drive/special/approot`. Login delegado feito uma vez por `onedrive_authorize.py`.
- **Rotação do refresh token:** com o App Writer, o novo valor é gravado no secret do environment **antes** de qualquer outra etapa; falha = `SECRET_ROTATION_FAILED` (saída 23). Sem o Writer, a rotação fica desligada e o token original vale até expirar.
- **Destino:** `<destination>/<request_id>/` com pacote e evidência.
- **Variáveis e segredos:** `ONEDRIVE_CLIENT_ID`, `ONEDRIVE_REFRESH_TOKEN`, `SOURCE_READ_TOKEN` (fine-grained: *Contents*, *Metadata* e *Issues*, somente leitura), **`PROJECTS_READ_TOKEN`** (clássico, `read:project`), `REPOSITORY_PRESERVATION_APP_ID` / `_PRIVATE_KEY` e, opcionais, `REPOSITORY_PRESERVATION_SECRETS_APP_ID` / `_PRIVATE_KEY` (Writer). `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` do environment são obsoletas (o HITL apaga).

## 8. Pendências
- [ ] Validade real do refresh token pessoal (~90 dias sem uso, **a confirmar**). Sem o Writer, repetir `onedrive_authorize.py` se expirar.
- [ ] Decidir se o App Writer será configurado (rotação automática) ou se o token manual basta.
- [ ] Apagar as variáveis obsoletas `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` (HITL).
- [ ] Exigir o check "Registry traceability" na proteção de branch (opcional, ver seção I.15 do guia).
- [ ] Definir validade e plano de renovação para os dois tokens de leitura: o `SOURCE_READ_TOKEN` (hoje sem expiração) e o `PROJECTS_READ_TOKEN` (HITL, opcional).
- [ ] Restringir o environment `onedrive-backup` à branch `main` (*Deployment branches*), para que workflow de outra branch não leia os secrets (HITL, opcional).
- [ ] Apagar do OneDrive os dados de Projects alheios gravados pelo run `req-20261003-002` (`api-project-items.json` e `api-projects.json` em `backups/backup_teste_issues/req-20261003-002/package/`), se o HITL não quiser essa cópia.
- [ ] Repositório grande: medir tempo e disco do runner (limite de 120 min).
- [ ] Nomes com acento no OneDrive e conteúdo de submódulos: testar ou aceitar como limite.
- [x] Rodar o `montar_repo_teste_issues.sh` e conferir o repositório e o quadro (02/10).
- [x] Dar *Issues: Read* ao `SOURCE_READ_TOKEN` (HITL, 02/10).
- [x] Criar o `PROJECTS_READ_TOKEN` (clássico, `read:project`) como secret do environment `onedrive-backup` (HITL, 03/10; o primeiro token foi exposto em uma captura de tela, revogado e substituído).
- [x] Runs reais das Etapas 2A e 2B-A com `Moriblo/backup_teste_issues`.
- [ ] Mesclar o PR #33 (registro do run real de `projects`).
- [ ] PR B (`BKP_PROJ`).

**Riscos:** repositórios grandes podem passar do limite do job ou do disco; sem o Writer, expiração do token interrompe o backup; tokens de leitura sem expiração ficam válidos até serem revogados.

---

## Issue #11: Restauração e Etapa 2: preservar e restaurar as 17 classes

*Aberta em 2026-09-29, atualizada em 2026-10-03, estado open.*

## Objetivo
O backup existe para que, depois de salvo no OneDrive, possa ser acessado direto de lá e **restituído ao GitHub**. **Um backup só está provado quando uma restauração funciona.**

> **Atualização (03/10/2026).** O **nível 1 está provado** (pacote restaurável restaurado em repositório novo, inclusive com LFS). A **Etapa 2A** (`labels`, `milestones`, `issues`) e a **Etapa 2B, parte A** (`projects` no `BKP_REPO`) estão **provadas em run real** (a de `projects` de forma **parcial**). O registro da prova de `projects` está no PR #33. Faltam o **PR B** (`BKP_PROJ`, backup só de Projects), a capability `restore_repository` e as demais classes. Contexto e histórico: issue #7.

## Estado
- ✅ **Pacote restaurável** no backup real: `git bundle` de todas as refs, objetos LFS, lista das refs (`refs.tsv`) e instantâneo legível (zip), com `RESTAURAR.txt` no pacote.
- ✅ **Teste de restauração do nível 1** (30/09 e 01/10): restaurado em repositório novo e vazio, refs iguais ao `refs.tsv`; com `Moriblo/backup_teste` (LFS, 2 branches, 2 tags, arquivo de ~25 MiB), 4 refs iguais e arquivo LFS com o tamanho real. Limite: do submódulo só o gitlink (`PARTIALLY-PRESERVED`).
- ✅ **Repositório de teste de issues e Projects** (`Moriblo/backup_teste_issues`, privado) e quadro nº 13 (7 itens: 6 issues e 1 rascunho; Status e Prioridade).
- ✅ **Etapa 2A** (`labels`, `milestones`, `issues`): run real `req-20261002-001` ([37076443670](https://github.com/Moriblo/repo_backup/actions/runs/37076443670)): 4 etiquetas, 1 marco, 6 issues, 5 comentários; `api-issues.json` conferido pelo HITL.
- ✅ **Etapa 2B, parte A** (`projects` no `BKP_REPO`), em 3 runs reais:
  - `req-20261003-001`: **falhou**. O token clássico `read:project` lista os Projects do dono mas não enxerga o repositório privado, o GraphQL devolve `null` no lugar dos repositórios ligados, e o leitor estourou e derrubou o job antes do pacote (PR #31: exceção vira `FAILED` sem derrubar o job).
  - `req-20261003-002`: funcionou, mas incluiu às cegas **3 Projects** (108 itens de outros repositórios); nomes de campo do GraphQL aceitos pela API real; paginação real (101 itens em 2 páginas).
  - `req-20261003-003` ([37089900544](https://github.com/Moriblo/repo_backup/actions/runs/37089900544)): **regra E** (PR #32), confirmação do vínculo pelos títulos das issues da origem. **1 Project (nº 13), `TITLE_MATCH` 6/6, 7 itens**; os nº 12 e nº 5 excluídos (`TITLES_DO_NOT_MATCH`) e **fora do pacote**. Classe `PARTIALLY-PRESERVED`: 6 itens com a referência da issue oculta; o estado do quadro (título, Status, Prioridade, etiquetas) **sobrevive**.
- ⏳ **Etapa 2B, parte B** (`backup_projects`, backup só de Projects): Mnemonic `BKP_PROJ`, workflow, job no Dispatcher, `MNEMONIC_WORKFLOWS`, schema da linha do log, escopos `SOURCE_REPOSITORY`, `PROJECT` e `OWNER_PROJECT_SET`. Reaproveita o leitor.
- ⏳ **Capability `restore_repository`**: ainda não existe (hoje a restauração é manual, pelo `RESTAURAR.txt`; os `api-*.json` são **registro**, não são restaurados).

## De onde vêm as restrições
Calculadas a cada execução: classes de `includes` (18) menos `implemented_classes` (fonte única, no `capabilities.yaml`). Hoje `[git, labels, milestones, issues, projects]` = **13 restrições** (14 se o `PROJECTS_READ_TOKEN` faltar, com o ID novo `RST-projects-ACCESS_PERMISSION_GAP`, que o HITL precisa aceitar).

## Níveis de restauração e as classes

| Nível | O que volta | Classes | Estado |
|---|---|---|---|
| **1. Código e histórico** | Branches, tags, histórico, LFS e submódulos (gitlink), com `git push --mirror` para um repositório novo | `git` | ☑ provado (manual, `RESTAURAR.txt`) |
| **2. Configuração** | Recriada pela API a partir dos dados preservados (segredos de webhook reemitidos) | `labels`, `milestones`, `rules_and_branch_protection`, `environments`, `variables`, `collaborators`, `webhooks` | `labels` e `milestones`: ☑ **preservadas** (run real), ☐ restauração pela API pendente; demais ☐ pendentes (5) |
| **3. Conteúdo colaborativo** | Cópia **equivalente**, sem a fidelidade original | `issues`, `pull_requests`, `projects` | `issues`: ☑ preservada; `projects`: ☑ **preservada de forma parcial** (run real); `pull_requests` ☐ pendente. Restauração pela API pendente nas três |
| **Sem restauração** | Fica como registro e evidência | `actions`, `checks`, `actions_caches`, `deployments`, `codespaces_repository_state_delta`, `secret_metadata`, `other_discovered_state` | ☐ pendente (7) |

O modo de restauração de cada classe é **previsto** e será confirmado no teste.

## Etapa 2B (`projects`): o que o token clássico permite e não permite
- **Credencial:** token fine-grained não alcança Projects v2 de **conta pessoal**; usa-se um token **clássico** com `read:project` (somente leitura), em `PROJECTS_READ_TOKEN`. Confirmado em run real.
- **Repositório privado:** o token **não enxerga** o repositório (isso exigiria `repo`, que dá escrita em todos os repositórios; **decisão do HITL: não dar**). Consequências provadas: o token sabe **quantos** repositórios estão ligados a um Project, não **quais** (`null`), e as issues do Project vêm com o conteúdo oculto (`content: null`). O que se perde é só a referência da issue (número, URL, repositório, estado); o título, Status, Prioridade e etiquetas sobrevivem e a ligação se reconstrói pelo título com a classe `issues`.
- **Regra E (heurística, não prova):** candidato com vínculo oculto fica se pelo menos 1 título dos itens de issue bate com as issues da origem e se bate pelo menos metade; senão é excluído e não entra no pacote. **Limites:** título repetido entre repositórios pode dar falso positivo; origem sem issues ou Project só com rascunhos não confirma nada (usar o escopo `PROJECT` do `BKP_PROJ`).

## Limites aceitos (não voltam idênticos)
- Refs `refs/pull/*`: gerenciadas pelo GitHub, não aceitam push de volta.
- Número, autor e datas originais de issues e pull requests.
- Histórico de execuções do Actions e artefatos.
- Valores de segredos (reemissão manual).
- Conteúdo de submódulos (só o gitlink).
- Referência das issues nos itens de Projects de repositório privado (ver acima).

## Ordem sugerida das classes
1. **Temporais primeiro:** `actions`, `checks`, `actions_caches`, `webhooks` (entregas).
2. Nível 2 (configuração).
3. Nível 3 (conteúdo colaborativo).
4. `secret_metadata`, `codespaces_repository_state_delta`, `other_discovered_state`.

*Decisões do HITL (02 e 03/10/2026):* a Etapa 2 começou por `labels`, `milestones` e `issues` e seguiu por `projects`, porque o repositório de teste já estava preparado; isso inverteu a ordem acima para esse grupo, e as classes temporais seguem como prioridade seguinte pelo protocolo (II.12). `projects` em **dois PRs**: A (leitor e `BKP_REPO`, feito) e B (`BKP_PROJ`, próximo). Token clássico com `read:project` aprovado, `repo` **não**; Mnemonic `BKP_PROJ` aprovado; regra E aprovada.

Cada classe precisa de: rota de leitura e permissão do App Reader (sem `workflows` e sem `codespaces secrets`, por decisão de segurança), modo de restauração, teste e entrada em `implemented_classes`.

## Critérios de aceite
- [x] Pacote restaurável gerado no primeiro backup real (bundle + LFS + lista de refs).
- [x] **Restauração de teste do nível 1** bem-sucedida em repositório novo e vazio, com resultado registrado (`BACKUP_REPOSITORY.md`, I.14).
- [ ] Capability `restore_repository` desenhada, aprovada e registrada.
- [ ] Cada uma das 17 classes: implementada, testada e em `implemented_classes` (acompanhar na tabela). Feitas: `git`, `labels`, `milestones`, `issues`, `projects` (leitura e preservação; `projects` parcial; restauração pela API pendente). Restam 13.
- [ ] **`projects`:** `BKP_PROJ` implementado e provado nos escopos `PROJECT` e `OWNER_PROJECT_SET` (PR B).
- [ ] Limites aceitos aparecem no relatório final como limitações explícitas.
- [ ] `BACKUP_REPOSITORY.md` (seção I.14) atualizado a cada classe entregue (feito para 2A e 2B-A; o registro da prova de `projects` está no PR #33).

Refs: #7 (SA-08), #10, PRs #28, #29, #30, #31, #32 e #33.
