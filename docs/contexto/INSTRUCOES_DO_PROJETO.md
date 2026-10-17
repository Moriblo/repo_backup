# Instruções do projeto Repo_Backup (continuidade de contexto)

**Versão do documento:** 17/10/2026, 18h55. **Instruções do projeto vigentes: v2.0** (ver 1.1).
**Para que serve:** garantir que qualquer sessão nova, em qualquer produto do Claude, retome o projeto sem depender da memória de conversas anteriores, e que nada decidido ou feito se perca entre sessões.

---

## 1. Texto para as instruções personalizadas do projeto

> O campo de instruções do projeto no claude.ai só pode ser editado pelo usuário (menu do projeto → instruções). Copie o bloco abaixo para lá. Ele é curto de propósito: é carregado em toda conversa, e o detalhe fica neste documento.

```text
INSTRUÇÕES v2.0 (16/10/2026)

Projeto: backup de repositórios do GitHub (conta Moriblo) para o OneDrive pessoal, executado pelos workflows do Moriblo/repo_backup. Eu sou o HITL. Responda em português.

PAPÉIS
- Engine (opera): a sessão do Claude Code "Engine de backup de repositórios", que segue só o protocolo do repositório (seção I.0). Ela monta ECRs, grava o commands.log depois do meu GO e abre PRs. No futuro, o aplicativo do repositório (claude/APP_FRONTEND_SPEC.md) também será engine.
- Sessão deste projeto (não opera): mantém os documentos do projeto, revisa PRs, prepara os prompts para a Engine e confere resultados (evidência no OneDrive pelo meu computador, GitHub pelo navegador). Não grava no commands.log nem dispara comandos.

INÍCIO DE TODA SESSÃO
1. Leia claude/STATUS_ATUAL.md (onde estamos e próximos passos) e claude/INSTRUCOES_DO_PROJETO.md (índice e ritual).
2. A fonte da verdade é o repositório Moriblo/repo_backup, branch main. Se tiver acesso, leia a versão atual de BACKUP_REPOSITORY.md e backup/capabilities.yaml e compare o commit do main com o registrado no STATUS. Se o main andou, avise e atualize os espelhos.
3. As cópias no projeto são espelho e contexto. Nunca autorizam nada. Em divergência, vale o main.

REGRAS QUE NUNCA SE QUEBRAM
- Nada executa sem ECR apresentado e GO explícito meu, digitado, para o pedido exato. GO não se reutiliza.
- A origem é sempre somente leitura. Código só entra por pull request. Escrita direta no main só no commands.log, depois do GO.
- Falta de evidência nunca é sucesso. O que não foi lido fica NOT-VERIFIED, nunca "ausente".
- Nenhum token ou segredo em chat, arquivo ou projeto.

FIM DE TODA SESSÃO EM QUE ALGO MUDOU (run real, merge, decisão minha, mudança de plano)
- Atualize claude/STATUS_ATUAL.md: data, commit do main, estado, próximos passos e registro de decisões.
- Reespelhe no projeto os arquivos do repositório que mudaram (lista em claude/INSTRUCOES_DO_PROJETO.md, seção 3).
- Me diga em uma linha o que foi salvo. Se não conseguir salvar, me diga o que ficou de fora.
Decisão tomada só no chat e não registrada é decisão perdida.
```


### 1.1 Versionamento das instruções do projeto

O texto acima é o que está salvo no campo "Instruções" do projeto. Ele é versionado para sabermos de que versão saímos e para qual vamos.

**Regra.**
- A primeira linha do campo "Instruções" traz a versão vigente: `INSTRUÇÕES v<maior>.<menor> (<data>)`.
- **Maior** (v2.0 → v3.0): muda papel, regra que nunca se quebra ou o ritual de início ou fim de sessão. **Menor** (v2.0 → v2.1): ajuste de redação, correção ou acréscimo que não muda comportamento.
- Toda mudança é proposta pela sessão do projeto com: versão de origem, versão de destino, o que muda e por quê, e o texto completo novo. O HITL salva no campo e confirma; só então a sessão registra a linha no histórico abaixo e no registro de decisões do `STATUS_ATUAL.md`.
- O texto completo da versão vigente fica sempre na seção 1 deste documento.

**Histórico.**

| Versão | Data | De → para | O que mudou |
|---|---|---|---|
| v1.0 | 13/10/2026 | (inicial) | "Você é o engine"; início de sessão (ler STATUS e instruções, conferir o `main`); regras que nunca se quebram; ritual de fim de sessão. |
| v2.0 | 16/10/2026 | v1.0 → v2.0 | Seção **PAPÉIS**: a Engine (sessão do Claude Code "Engine de backup de repositórios") opera; a sessão do projeto **não opera** (documentos, revisão de PRs, prompts, conferência). GO passa a ser explicitamente **digitado**. Cita o aplicativo futuro como engine (`claude/APP_FRONTEND_SPEC.md`). Saída da v1.0: o engine deixou de ser a sessão do projeto (sessões antigas e `01YHH` arquivadas em 16/10). |

---

## 2. Como iniciar uma sessão

1. Ler `claude/STATUS_ATUAL.md`.
2. Ler este documento.
3. Se a sessão tiver acesso ao GitHub (conector, clone ou `gh`):
   - ler a versão atual de `BACKUP_REPOSITORY.md` e `backup/capabilities.yaml` no `main`;
   - comparar o último commit do `main` com o registrado no topo do `STATUS_ATUAL.md`;
   - se o `main` andou, ler os commits novos, avisar o HITL e atualizar o status e os espelhos antes de seguir.
4. Se a sessão **não** tiver acesso ao GitHub, trabalhar com os espelhos do projeto e avisar o HITL de que podem estar velhos (data no topo do `STATUS_ATUAL.md`). Nesse caso, **não gravar** no `commands.log` nem disparar nada: sem leitura do `main` não há como confirmar ids, ECR atual e estado do registro.
5. Para operar uma capability, seguir a seção I.0 do `BACKUP_REPOSITORY.md` (o prompt inicial do engine e os arquivos que cada capability exige).

## 3. Índice dos documentos do projeto

### Contexto vivo (escritos pelo engine; mantenha atualizados)

| Documento | Conteúdo |
|---|---|
| `claude/MIT_BACKUP_FULL_COBERTURA.md` | O que o backup full do `Moriblo/MIT` precisa cobrir, o que já cobre e os prazos. |
| `claude/STATUS_ATUAL.md` | Onde o projeto está, referências (runs, pastas, repositórios), próximos passos, riscos e registro de decisões do HITL. **Ler primeiro.** |
| `claude/INSTRUCOES_DO_PROJETO.md` | Este documento. |
| `claude/ISSUES_ABERTAS_repo_backup.md` | Cópia do corpo e dos comentários das issues abertas (hoje #5 e #70), que guardam decisões fora dos arquivos do repositório. |
| `claude/APP_FRONTEND_SPEC.md` | Especificação da v1 do aplicativo (web estático no Pages, o app como engine, spikes S1–S4, PRs A–D). Decisão do HITL de 16/10. Depois do PR A, a versão vigente fica em `docs/app/SPEC.md` no repositório. |
| `claude/STAGE_1_ONEDRIVE_DELEGATED_DECISION.md` | Decisões e setup de 29/09 (rota OneDrive delegada, fluxo acordado). **Histórico**: o estado atual está no `STATUS_ATUAL.md`. |

### Espelhos do repositório `Moriblo/repo_backup` (fonte: `main`)

| Documento no projeto | Arquivo no repositório | Para quê |
|---|---|---|
| `BACKUP_REPOSITORY.md` | `BACKUP_REPOSITORY.md` | Protocolo e guia operacional (em português). Seção I.14 tem o registro detalhado de provas. |
| `capabilities.yaml` | `backup/capabilities.yaml` | Registro: menu, Mnemonics, parâmetros, classes implementadas, artefatos. |
| `command-request.schema.yaml` | `backup/schemas/command-request.schema.yaml` | Schema do ECR. |
| `claude/commands-log-line.schema.yaml` | `backup/schemas/commands-log-line.schema.yaml` | Schema da linha do `commands.log`, com um ramo por Mnemonic. |
| `evidence.schema.yaml` | `backup/schemas/evidence.schema.yaml` | Schema da evidência 2.0. |
| `backup-manifest.schema.yaml` | `backup/schemas/backup-manifest.schema.yaml` | Schema do manifest 2.0. |
| `claude/commands.log` | `commands.log` | Todas as autorizações já dadas (ids usados, parâmetros, decisões de preflight). |
| `claude/repo_backup_README.md` | `README.md` | Porta de entrada do repositório. |
| `claude/workflows/backup-repository.yml` | `.github/workflows/backup-repository.yml` | Executor do `BKP_REPO`; o ECR está nos `inputs`. |
| `claude/workflows/backup-projects.yml` | `.github/workflows/backup-projects.yml` | Executor do `BKP_PROJ`. |
| `claude/workflows/backup-issues.yml` | `.github/workflows/backup-issues.yml` | Executor do `BKP_ISSUES`. |
| `claude/workflows/restore-repository.yml` | `.github/workflows/restore-repository.yml` | Executor do `RST_REPO`. |
| `claude/workflows/dispatcher.yml` | `.github/workflows/dispatcher.yml` | Dispatcher: valida o push e chama o workflow de cada Mnemonic. |
| `claude/workflows/traceability.yml` | `.github/workflows/traceability.yml` | Check "Registry traceability" dos PRs. |

**No sentido inverso:** `STATUS_ATUAL.md`, `INSTRUCOES_DO_PROJETO.md` e `ISSUES_ABERTAS_repo_backup.md` têm cópia no repositório, em `docs/contexto/` (ver a seção 4).

**Fora do espelho, de propósito:** os scripts (`backup/scripts/*.py`, `*.sh`) e os testes (`backup/tests/`). São código, mudam com cada PR e só são úteis com acesso ao repositório, onde já estão.

### Especificação das Fases 0 e 1 (histórico, versões finais)

| Documento | Observação |
|---|---|
| `FULL_REPOSITORY_PRESERVATION_PROCESS_v1.11.yaml` | Especificação do processo. **Versão vigente** (as anteriores foram removidas do projeto em 17/10). |
| `PHASE_0_CAPABILITY_ASSESSMENT_AND_EXPORTABILITY_MATRIX_v1.10.md` | Matriz de capacidades e exportabilidade. **Versão vigente** (as anteriores foram removidas do projeto em 17/10). |
| `PHASE_1_FULL_SOURCE_INVENTORY_Moriblo_MIT_v1.0.md` | Inventário do `Moriblo/MIT`, consolidado. **Versão vigente** (as anteriores foram removidas do projeto em 17/10). |
| `Backup Process.docx` | Documento de processo original. |

### Obsoletos (não usar)

| Documento | Motivo |
|---|---|
| `onedrive-appfolder-oidc-read-test.yml` | Workflow da rota OIDC/app-only, descartada em 29/09 e removida do repositório. |


### Referência

| Documento | Observação |
|---|---|
| `Imagem do Codex 26 de set. de 2026 23_20_43.png` | Imagem da sessão do Codex de 26/09. |

## 4. Ritual de atualização (fim de sessão ou depois de cada entrega)

Faça sempre que houver: run real, PR mergeado, decisão do HITL, mudança de plano ou de próximos passos.

1. **`claude/STATUS_ATUAL.md`**: ler, ajustar e regravar por inteiro (o projeto não tem edição parcial). Atualizar:
   - a data e o commit do `main` no topo;
   - as tabelas de capabilities e classes, se algo foi provado ou implementado;
   - as referências (último backup, última restauração, próximo `request_id`);
   - os próximos passos;
   - o registro de decisões, com data, para **toda** decisão do HITL tomada na sessão.
2. **Espelhos**: para cada arquivo da tabela da seção 3 que mudou no `main`, regravar no mesmo caminho do projeto (`project_write` com `local_path`, a partir de um clone atualizado). Na dúvida, regravar todos: é barato. Um arquivo novo importante no repositório (workflow ou schema) entra na tabela e no espelho.
3. **Issues**: se as issues abertas (hoje #5 e #70) mudaram, ou se uma issue nova passou a guardar decisões, regravar `claude/ISSUES_ABERTAS_repo_backup.md`.
4. **Cópia no repositório**: abrir um PR que atualiza `docs/contexto/` no `repo_backup` com as versões novas destes documentos, para que sessões do Claude Code sem acesso ao projeto também os enxerguem. O PR pode ir junto com um PR de código da mesma sessão.
5. **Avisar o HITL** em uma linha o que foi salvo, ou o que não foi possível salvar.

**Qual cópia vale.** A versão **principal** do status, destas instruções e da cópia das issues é a do **projeto** (`claude/...`), atualizada a cada sessão. A de `docs/contexto/` no repositório é uma cópia, atualizada por PR e mergeada pelo HITL. Se divergirem, vale a de data mais recente no topo do documento.

**O que nunca vai para o projeto:** valores de tokens, refresh tokens, chaves privadas ou qualquer segredo. O conteúdo do backup (pacotes, JSON de issues e Projects) também não: fica no OneDrive.

## 5. Onde cada tipo de informação deve morar

| Informação | Lugar definitivo | Também no projeto? |
|---|---|---|
| Regras, fluxo, provas de cada etapa | `BACKUP_REPOSITORY.md` no `main` (por PR) | Sim, espelho |
| Capabilities, classes, parâmetros | `backup/capabilities.yaml` no `main` (por PR) | Sim, espelho |
| Autorizações dadas | `commands.log` no `main` | Sim, espelho |
| Evidência de cada execução | OneDrive e artefato do run | Não; só a referência no status |
| Estado atual, próximos passos, decisões do HITL | `claude/STATUS_ATUAL.md` | É o próprio |
| Discussão e planejamento de etapas | Issues do `repo_backup` | Sim, cópia das abertas |

Uma decisão que muda o comportamento do sistema entra também no repositório, por PR, no `BACKUP_REPOSITORY.md` ou no `capabilities.yaml`. O status registra a decisão; o repositório a torna regra.
