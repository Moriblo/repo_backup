# Backup Repository — Protocolo de Execução Portável

> Ponto de entrada oficial do processo de preservação completa de repositórios. Um engine de IA **deve** conseguir começar por aqui, sem nenhuma memória de conversas anteriores.

## 1. Resumo

**O que é.** Um sistema que copia um repositório do GitHub (a **origem**, sempre somente leitura) para o OneDrive pessoal (o **destino**). Cada execução exige autorização humana (o **HITL**) e gera evidência verificável. A origem nunca é alterada.

**Objetivo final: restaurar.** O backup existe para que, depois de salvo no OneDrive, ele possa ser acessado direto de lá e, se necessário, **restituído ao GitHub**. Um backup só está provado quando uma restauração funciona. A restauração é **PLANEJADA** (seção I.14).

**Como funciona, em seis linhas**
1. O **engine** (o assistente de IA) apresenta o menu de capabilities. O HITL escolhe `backup_repository` e informa origem e destino.
2. O engine apresenta o pedido exato (o **ECR**), lido do workflow. O HITL dá **GO**.
3. O engine grava **uma linha** no `commands.log`, no `main`.
4. O **Dispatcher** valida a linha e chama o workflow `BKP_REPO`.
5. O `BKP_REPO` faz a verificação prévia (preflight), lê a origem e gera a evidência. Gravar no OneDrive é **planejado**.
6. O engine lê o resultado e o reporta ao HITL.

**Regras que nunca se quebram**
- Nada é inferido. Sem GO explícito para o pedido exato, nada executa, e o GO não é reutilizável.
- A origem é sempre somente leitura.
- Falta de evidência nunca é sucesso. O que não pôde ser lido fica `NOT-VERIFIED`, nunca "ausente".
- Segredo em texto puro nunca aparece em chat, log ou evidência.
- Código só entra por pull request. A única escrita direta no `main` é o `commands.log`, e só depois do GO.

**Estado atual (29/09/2026).** A leitura do GitHub e o fluxo até o preflight estão validados em execuções reais. O destino OneDrive ainda não existe, então toda execução termina `BLOCKED` no passo do destino (seção I.4, passo 11). Hoje o backup cobre só o **código Git**; as demais 17 classes são restrições aceitas (seção I.14). **Ainda não existe restauração.**

**Onde encontrar cada coisa**

| Preciso de… | Vá para |
|---|---|
| Abrir um chat novo (o primeiro prompt e os arquivos a ler) | **I.0** |
| Operar um backup, passo a passo | Parte I, seções I.3 e I.4 |
| Saber quem faz o quê | I.1 |
| Entender uma linha do `commands.log` | I.6 |
| Saber de onde vem o Mnemonic | I.7 |
| Interpretar um resultado ou um código de saída | I.8 e I.12 |
| Saber onde ficam segredos e variáveis | I.10 |
| Saber o que dá para restaurar e o que não dá | **I.14** |
| Fazer o check de rastreabilidade bloquear merges (opcional) | **I.15** |
| Ver as regras obrigatórias | Parte II |
| Saber o significado de um termo | Parte III |

## 2. Como ler este documento

| Parte | Conteúdo | Natureza |
|---|---|---|
| **Parte I: Operação** | Players, fluxo passo a passo, arquivos, segredos, códigos de saída | Guia prático |
| **Parte II: Protocolo normativo** | Regras obrigatórias do processo | **Normativa**: em caso de conflito, vale esta parte |
| **Parte III: Glossário** | Termos usados e seus significados | Referência |

**Convenções**
- **DEVE**, **NÃO DEVE**, **PODE**: obrigação, proibição e permissão, nesse sentido estrito.
- Nomes de campos, códigos e estados ficam no original, em `MAIÚSCULAS` ou em código (`PRESERVED`, `BLOCKED`, `request_id`), porque os scripts e os schemas os usam.
- Termos técnicos consagrados (Capability Preflight, Source Inventory, HITL, ECR) são mantidos e explicados na Parte III.
- Itens marcados **PLANEJADO** ainda não existem. Serão atualizados quando forem implementados.

---

# Parte I: Operação

## I.0 Como iniciar: o primeiro prompt de um chat novo

**Princípio.** O engine não tem memória. Tudo o que ele precisa está no repositório **`Moriblo/repo_backup`**, branch **`main`**. Ele **deve** ler a versão **atual** de lá, e nunca confiar em conversas anteriores. Por isso o primeiro prompt só precisa dizer **onde ler**.

**1. Arquivos que o engine lê no início (sempre os dois, nesta ordem)**

| Ordem | Arquivo (no `main`) | Para quê |
|---|---|---|
| 1 | `BACKUP_REPOSITORY.md` | O protocolo: regras, fluxo e guia operacional. |
| 2 | `backup/capabilities.yaml` | O registro: a lista de capabilities do **menu**, o Mnemonic e os parâmetros de cada uma. |

Endereço: `https://github.com/Moriblo/repo_backup/blob/main/<caminho>`. O engine precisa de acesso de leitura ao repositório (conector do GitHub ou clone).

**2. Arquivos que o engine lê depois que você escolhe uma capability**

| Capability | Lê também | Observação |
|---|---|---|
| `backup_repository` | `.github/workflows/backup-repository.yml` (o ECR, nos `inputs`); `backup/schemas/commands-log-line.schema.yaml` (formato da linha); `commands.log` (ids já usados) | O caminho do workflow vem do registro: `command_workflow.artifact_id` da capability, resolvido em `implementation_artifacts[].location.path`. |
| `backup_projects` | Nada além dos dois iniciais | **Ainda não tem Mnemonic nem executor.** O engine informa que não consegue gerar comando e aponta a seção II.15. |
| `list_capabilities`, `show_status`, `validate_evidence`, `help` | Só os dois iniciais | O engine responde na conversa. Não geram linha de comando. |
| `restore_repository` (**PLANEJADA**) | Ainda não existe no registro | Restaurar a partir do OneDrive para o GitHub (seção I.14). Quando existir, terá Mnemonic e workflow próprios, sob GO do HITL. |
| Capability nova no futuro | O que o registro indicar para ela | O menu vem do registro: **o prompt inicial não muda** quando uma capability é acrescentada. |

**Regra geral.** Se a capability escolhida tem `mnemonic` no registro, o engine lê o workflow apontado em `command_workflow` e apresenta o ECR a partir dele. Se não tem, responde na conversa e não grava nada.

**3. Prompt inicial (copie e cole)**

```text
Você é o engine do processo de backup de repositórios.
Repositório: Moriblo/repo_backup, branch main.

Leia, nesta ordem, a versão ATUAL do main:
1) BACKUP_REPOSITORY.md   (protocolo e guia operacional)
2) backup/capabilities.yaml   (registro; lista as capabilities do menu)

Depois apresente o Capability Menu e aguarde a minha escolha.
Não escolha por mim, não infira parâmetros e não grave nada até eu dar GO
para o pedido exato (ECR). Quando eu escolher uma capability, leia também os
arquivos que o registro indicar para ela.
Se não conseguir ler o repositório, pare e me avise. Não siga de memória.
```

**4. Pré-requisitos do chat**
- Acesso de **leitura** ao repositório, para todas as capabilities.
- Para gravar o `commands.log` (passo 5): acesso de escrita ao `main` e **autorização explícita do HITL**. O engine não grava sem ela.
- Nenhum token, chave ou segredo é colado no chat.

## I.1 Players

| Player | Papel |
|---|---|
| **HITL** (humano) | Escolhe a capability, informa os parâmetros, dá GO ou NO-GO, decide `STOP` ou `CONTINUE_WITH_RESTRICTIONS` no preflight e faz o merge dos PRs. |
| **Engine** (hoje, a sessão do Claude Code; outros no futuro, ver a issue #5) | Lê este protocolo, apresenta o menu e o ECR, grava a linha no `commands.log` depois do GO e reporta o resultado. Nunca infere autorização. |
| **Repositório `repo_backup`** (`main`) | Guarda o código, o `commands.log`, os segredos e as variáveis. Código só entra por pull request. |
| **GitHub Actions** | Executa o Dispatcher e o workflow do `BKP_REPO` (e, temporariamente, o diagnóstico). |
| **GitHub App "Repository Preservation Reader"** | Emite, a cada execução, um token de curta duração só de leitura (`contents: read`, `metadata: read`) para a origem. |
| **GitHub App Writer** (**PLANEJADO**) | Grava o novo refresh token do OneDrive no secret. Só *Secrets: Read and write*, instalado só neste repositório. |
| **Repositório de origem** | É lido e **nunca alterado**. |
| **Microsoft Entra, app público** (**PLANEJADO**) | Emite os tokens do OneDrive pela autoridade `consumers` (conta pessoal). |
| **Microsoft Graph e OneDrive** (**PLANEJADO**) | Recebem o pacote e a evidência em `Apps/<nome do registro>/…`. |

## I.2 Preparação, uma única vez (**PLANEJADO**)

Depende do PR do OneDrive (SA-08 da issue #7).
- **0a.** O HITL configura o registro no Entra: aceitar contas pessoais, permitir fluxos de cliente público e conceder as permissões **delegadas** `Files.ReadWrite.AppFolder` e `offline_access`.
- **0b.** O HITL cria o GitHub App Writer.
- **0c.** O HITL roda `onedrive_authorize.py` no computador dele e faz o login. O script imprime o refresh token só no terminal.
- **0d.** O HITL grava os segredos e as variáveis (seção I.10).

## I.3 Os 15 passos de cada execução (índice)

O texto de cada passo, com o que entra, o que acontece, o que pode falhar e o que sai, está na seção I.4, nos títulos "Passo N". Os títulos não são links de propósito: links internos não funcionam em todas as telas do GitHub.

| # | Passo | Quem | Hoje |
|---|---|---|---|
| 1 | O engine apresenta o Capability Menu | Engine | Existe |
| 2 | O HITL escolhe a capability e informa os parâmetros | HITL | Existe |
| 3 | O engine apresenta o ECR (Exact Command Request) | Engine | Existe |
| 4 | O HITL dá GO ou NO-GO | HITL | Existe |
| 5 | O engine grava a linha no `commands.log` | Engine | Existe |
| 6 | O Dispatcher valida o push e as linhas novas | Actions | Existe |
| 7 | O Dispatcher chama o workflow do Mnemonic | Actions | Existe |
| 8 | O `BKP_REPO` começa | Actions | Existe |
| 9 | Capability Preflight | Actions | Existe |
| 10 | O engine reporta e o HITL decide | Engine e HITL | Existe |
| 11 | Validação do destino | Actions | Falha fechado; validação real **PLANEJADA** |
| 12 | Leitura da origem | Actions | Parcial |
| 13 | Pacote e envio ao OneDrive | Actions | **PLANEJADO** |
| 14 | Evidência e manifest | Actions | Parcial |
| 15 | O engine reporta o resultado | Engine e HITL | Existe |

**Observações**
- Com as 17 restrições atuais (todas as classes, menos o Git), toda execução exige **duas linhas** no `commands.log`: a primeira termina `BLOCKED`, e a segunda carrega a decisão do HITL.
- O `request_id` de uma linha rejeitada ou bloqueada fica **queimado**: o log só recebe acréscimos, e a autorização não é reutilizável.
- Enquanto o passo 11 falhar fechado, nada é copiado e nenhuma evidência é gerada.

## I.4 Fluxo detalhado, passo a passo

Cada passo diz **quem** age, o que **entra**, o que **acontece** (com os arquivos), o que pode **falhar** e o que **sai**.

#### Passo 1: o engine apresenta o Capability Menu
- **Quem:** engine.
- **Entra:** o pedido do HITL para iniciar um backup.
- **Acontece:** o engine lê, na versão atual do `main` de `Moriblo/repo_backup`, este documento e o `backup/capabilities.yaml` (seção I.0) e apresenta as capabilities que o registro lista (hoje, seis). Ele **não escolhe** por conta própria.
- **Falha:** se não conseguir ler o registro, o engine para e avisa. Nada é inferido de conversas anteriores.
- **Sai:** o menu.

#### Passo 2: o HITL escolhe a capability e informa os parâmetros
- **Quem:** HITL (o engine consulta o registro).
- **Acontece:**
  1. O HITL escolhe `backup_repository`.
  2. O engine consulta o registro e descobre o **Mnemonic** (`mnemonic: BKP_REPO`) e os **parâmetros requeridos** (`source_repository` e `destination`).
  3. Se a capability não tem Mnemonic no registro (ex.: `backup_projects`), o engine informa que não consegue gerar comando e para.
  4. O HITL informa ou confirma cada valor. O engine pode **propor**, nunca substituir a confirmação, e nada é herdado de execuções anteriores.
- **Falha:** valor fora do formato é recusado. Exemplo real: o `destination` dado como URL do OneDrive foi recusado, porque o destino é um caminho **relativo ao AppFolder** (sem `/` inicial e sem `..`).
- **Sai:** capability, Mnemonic e valores.

#### Passo 3: o engine apresenta o ECR (Exact Command Request)
- **Quem:** engine.
- **Acontece:** lê os `inputs` do `on: workflow_call` de `.github/workflows/backup-repository.yml` e apresenta cada campo **exatamente como o workflow o define**, com a restrição e o valor proposto. Antes disso, confere os valores contra `commands-log-line.schema.yaml`.
- **Sai:** o ECR completo, para o GO.

#### Passo 4: o HITL dá GO ou NO-GO
- **Quem:** HITL.
- **Acontece:** o GO vale **somente para este ECR exato**. Mudou qualquer valor, é um novo ECR e um novo GO.
- **Falha:** NO-GO, resposta ambígua ou ausente: nada é gravado. Nenhuma autorização é presumida.

#### Passo 5: o engine grava a linha no `commands.log`
- **Quem:** engine, com uma identidade autorizada a gravar no `main` (hoje, o actor `Moriblo`; ver a issue #5).
- **Acontece:**
  1. Monta **uma linha JSON** (seção I.6), com o Mnemonic copiado do registro e a hora atual em `ts`.
  2. Roda localmente o mesmo verificador do dispatcher, como boa prática adotada nas execuções reais.
  3. Faz um commit **só com o `commands.log`** e o envia direto ao `main`. É a **única escrita direta** permitida no `main`.
- **Falha:** push recusado: nada dispara.
- **Sai:** um push no `main`, que dispara o Dispatcher.

#### Passo 6: o Dispatcher valida o push e as linhas novas
- **Quem:** GitHub Actions, `dispatcher.yml`, job **"Validate push and new command lines"**, com `backup/scripts/dispatch_check.py`.
- **Acontece:** sete verificações, nesta ordem. **A primeira que falhar recusa o push inteiro.**
  1. **Intervalo:** há commit anterior e ele é ancestral do novo (recusa criação de branch e reescrita de histórico).
  2. **Escrita direta:** todo commit da linha principal que altera arquivos além do `commands.log` precisa vir de um pull request mergeado (consulta à API). Commit que mistura `commands.log` com outros arquivos é recusado.
  3. **O log mudou?** Se não mudou (ex.: merge de PR), o push é aceito e não há nada a despachar. É o run verde e curto a cada merge.
  4. **Só acréscimo:** o conteúdo antigo é prefixo exato do novo, a última linha termina em quebra de linha e não há linha em branco.
  5. **Cada linha nova:** é JSON válido e cumpre `commands-log-line.schema.yaml`.
  6. **`request_id` único:** não repete nenhum id já presente no log nem outro do mesmo push.
  7. **Mnemonic:** consta no mapeamento fixo do dispatcher **e** o registro concorda com o workflow mapeado.
- **Falha:** saída **30**, job vermelho, **nada é despachado**. A mensagem diz qual regra falhou, sem repetir o conteúdo da linha.
- **Sai:** a linha `DISPATCH_ACCEPTED {json}` com os `request_id` aceitos.

#### Passo 7: o Dispatcher chama o workflow do Mnemonic
- **Quem:** GitHub Actions, job **`run-bkp-repo`**.
- **Acontece:** **traduz o Mnemonic em workflow** pelo mapeamento fixo (`BKP_REPO` → `.github/workflows/backup-repository.yml`) e o chama por `workflow_call`, com `secrets: inherit`, passando `request_id`, `source_repository`, `destination` e `preflight_decision`. Há um grupo de concorrência por `request_id`.
- **Falha:** sem linhas novas, o job é **pulado** (aparece cinza). Não é erro.

#### Passo 8: o `BKP_REPO` começa
- **Quem:** GitHub Actions, `backup-repository.yml`.
- **Acontece:**
  1. Baixa este repositório (scripts e schemas) e instala as bibliotecas do validador.
  2. **`validate-inputs`:** revalida os inputs contra o mesmo schema (segunda barreira). Recusa também `preflight_decision` ilegível e a reutilização de autorização (`previous_request_id` igual ao `request_id`).
  3. **Token do App Reader:** emite um token de curta duração, só de leitura, válido só para a origem.
- **Falha:** entrada inválida termina com saída **2** e `BKP_RESULT` **`REJECTED`**. A falha do **token não derruba** o workflow: vira uma lacuna no preflight.

#### Passo 9: Capability Preflight
- **Quem:** GitHub Actions, `bkp_repo.py preflight`.
- **Acontece:** avalia as **18 classes** de objeto do `backup_repository`, cada rota separadamente. Hoje só a classe **`git`** tem rota implementada. As outras **17** viram lacuna `EXECUTION_CAPABILITY_GAP`. Se o token não foi emitido, o `git` também vira lacuna (`ACCESS_PERMISSION_GAP`), e são **18**. O resultado é validado contra o schema do command-request e gravado em `evidence/preflight.json`.
- **De onde vêm as restrições:** não são uma lista fixa. São **calculadas** a cada execução: as classes de `includes` (em `backup/capabilities.yaml`) menos as de `implemented_classes` (também em `backup/capabilities.yaml`, na capability). Hoje, 18 menos 1 (`git`) dá **17**. Cada uma tem o nome `RST-<classe>-<tipo da lacuna>`. A lista completa aparece no `BKP_RESULT`, no `preflight.json` e, depois de aceita, no `commands.log`. Implementar uma classe a retira das restrições (seção I.14).
- **Decisão:** a execução só segue se **todas** as restrições exigidas estiverem em `preflight_decision.accepted_restrictions`. Aceitar menos mantém o bloqueio.
- **Sai:** saída **10**, `BKP_RESULT` **`BLOCKED`** (`CAPABILITY_PREFLIGHT_GAPS`) com a lista `required_restrictions`, e os passos seguintes são pulados. Ou saída **0**, `BKP_RESULT` **`PREFLIGHT_OK`**. O artefato `bkp-repo-<request_id>` é publicado sempre.

#### Passo 10: o engine reporta e o HITL decide
- **Quem:** engine e HITL.
- **Acontece:** o engine lê a linha `BKP_RESULT` no log do passo "Capability Preflight" (Actions, run do Dispatcher, job "BKP_REPO `<request_id>`") e apresenta as restrições exigidas. O HITL escolhe:
  - **`STOP`:** encerra. Nenhuma linha é gravada, e o `request_id` usado continua queimado.
  - **`CONTINUE_WITH_RESTRICTIONS`:** o engine monta uma **nova linha**: **novo `request_id`**, os **mesmos** `source_repository` e `destination`, e o bloco `preflight_decision` (`previous_request_id` = a requisição bloqueada; `hitl_decision`; `accepted_restrictions` = os itens de `required_restrictions`). O fluxo **volta ao passo 3**, com novo ECR e novo GO.

#### Passo 11: validação do destino
- **Quem:** GitHub Actions, `bkp_repo.py validate-destination`.
- **Hoje:** **sempre falha fechado**: saída **20**, `BKP_RESULT` **`BLOCKED`** (`DESTINATION_NOT_VALIDATED`). Nada é copiado. É o comportamento correto até o OneDrive existir.
- **PLANEJADO:** renova o access token, grava o novo refresh token no secret pelo App Writer e faz uma escrita e uma leitura de teste no AppFolder.

#### Passo 12: leitura da origem
- **Quem:** GitHub Actions, passos bash do `backup-repository.yml`. Só chegam aqui com o destino validado.
- **Acontece:**
  1. Confere `git` e `git-lfs` no runner.
  2. Clona a origem com `git clone --mirror`. O token vai por cabeçalho HTTP, só nesse processo: não aparece na URL, no log nem na configuração do mirror.
  3. Grava `refs.tsv`, `git-fsck.txt` (`fsck --full --strict`) e `git-count-objects.txt`.
  4. Enumera Git LFS em todas as refs. Havendo objetos, baixa todos e confere o SHA-256 de cada um. Falhas: saída **3** (não listou), **4** (OID inválido), **5** (objeto ausente), **6** (hash divergente).
  5. Varre o histórico inteiro atrás de submódulos (`gitmodules-history.tsv`, `gitlinks-history.tsv`).
- **PLANEJADO:** comparar as refs da **origem** com as do mirror (`git ls-remote` contra `refs.tsv`).
- **Falha:** o job fica vermelho no passo que falhou.

#### Passo 13: pacote e envio ao OneDrive (**PLANEJADO**)
- Gera o **pacote restaurável**: o `git bundle` de todas as refs, os **objetos LFS** (o bundle não os carrega) e a lista das refs. Envia em blocos ao OneDrive e confere o SHA-256 depois do envio.
- O pacote só conta como pronto se uma **restauração de teste** o aceitar (seção I.14).

#### Passo 14: evidência e manifest
- **Quem:** GitHub Actions, `bkp_repo.py build-evidence`.
- **Acontece:** recusa-se a rodar sem destino validado (saída **21**). Monta o `evidence.json` (classe `git` como `PRESERVED`, `PARTIALLY-PRESERVED` se houver submódulos ou `FAILED` se nenhuma ref foi lida; demais classes como `NOT-VERIFIED`, ligadas às restrições aceitas) e o `manifest.json`. **Valida ambos contra os schemas 2.0 antes de gravar.** O status final é `COMPLETE`, `COMPLETE_WITH_EXCEPTIONS` ou `FAILED` (saída **1**).
- **Sai:** a linha final `BKP_RESULT` com `evidence_sha256`, `manifest_sha256` e a reconciliação, e o artefato com os arquivos. **PLANEJADO:** enviar a evidência ao OneDrive.

#### Passo 15: o engine reporta o resultado
- **Quem:** engine.
- **Acontece:** lê o `BKP_RESULT` final e reporta ao HITL o estado, as restrições aceitas, a reconciliação e os hashes. **Sucesso do workflow não prova preservação**: vale o manifest validado.

## I.5 Diagrama do fluxo

```mermaid
sequenceDiagram
    autonumber
    actor HITL
    participant Engine
    participant Main as main e commands.log
    participant Disp as Dispatcher
    participant BKP as BKP_REPO
    participant Src as Origem no GitHub
    participant OD as OneDrive

    Engine->>Engine: Le BACKUP_REPOSITORY.md e capabilities.yaml
    Engine->>HITL: Capability Menu
    HITL->>Engine: backup_repository, source_repository, destination
    Note over Engine: O Mnemonic BKP_REPO vem do capabilities.yaml
    Engine->>HITL: ECR lido do backup-repository.yml
    HITL->>Engine: GO
    Engine->>Main: Acrescenta uma linha no commands.log
    Main->>Disp: O push dispara o Dispatcher
    Disp->>Disp: Valida push, log, schema, request_id e Mnemonic
    Disp->>BKP: workflow_call com os 4 inputs
    BKP->>BKP: Revalida os inputs
    BKP->>Src: Token do App Reader, somente leitura
    BKP->>BKP: Capability Preflight
    alt lacunas sem decisao do HITL
        BKP-->>Engine: BKP_RESULT BLOCKED com required_restrictions
        Engine->>HITL: Reporta as restricoes
        HITL->>Engine: STOP ou CONTINUE_WITH_RESTRICTIONS
        Note over Engine,Main: Se CONTINUE, nova linha e novo GO, volta ao ECR
    else sem lacunas ou decisao ja aceita
        BKP->>OD: Valida o destino, PLANEJADO
        Note over BKP,OD: Hoje o destino falha fechado, saida 20
        BKP->>Src: Mirror, fsck, LFS e submodulos
        BKP->>OD: Pacote e evidencia, PLANEJADO
        BKP-->>Engine: BKP_RESULT final
        Engine->>HITL: Reporta estado, restricoes e reconciliacao
    end
```

## I.6 Anatomia da linha do `commands.log`

Uma linha JSON por comando, sem quebras internas. O schema é `backup/schemas/commands-log-line.schema.yaml`.

| Campo | O que é | Regra |
|---|---|---|
| `request_id` | Identificador único da requisição | 8 a 64 caracteres `[A-Za-z0-9._-]`. Não repete nenhum id já usado. Convenção adotada: `req-AAAAMMDD-NNN`. |
| `mnemonic` | Comando a executar | Vem do registro. Valor aceito hoje: `BKP_REPO`. |
| `ts` | Hora da gravação | UTC, ISO 8601. |
| `authorization` | O GO do HITL | `decision: GO`, `authorized_scope: EXACT_COMMAND_REQUEST`, `reusable: false`. |
| `params.source_repository` | Origem do backup | Formato `dono/nome`. Somente leitura. |
| `params.destination` | Pasta no OneDrive | Relativa ao AppFolder; sem `/` inicial e sem `..`. |
| `params.preflight_decision` | Só na linha de continuação | `previous_request_id`, `hitl_decision: CONTINUE_WITH_RESTRICTIONS`, `accepted_restrictions` (lista). |

Exemplo de uma primeira linha (numa única linha no arquivo real):

```json
{"request_id":"req-20260929-001","mnemonic":"BKP_REPO","ts":"2026-09-29T04:35:22Z","authorization":{"decision":"GO","authorized_scope":"EXACT_COMMAND_REQUEST","reusable":false},"params":{"source_repository":"Moriblo/Minha_Caixinha_de_Saude","destination":"Minha_Caixinha_de_Saude"}}
```

A linha de continuação é igual, com outro `request_id` e, em `params`, o bloco `preflight_decision` (abreviado):

```json
"preflight_decision":{"previous_request_id":"req-20260929-001","hitl_decision":"CONTINUE_WITH_RESTRICTIONS","accepted_restrictions":[{"restriction_id":"RST-issues-EXECUTION_CAPABILITY_GAP","object_class":"issues","restriction":"...","source_gap_classification":"EXECUTION_CAPABILITY_GAP"}]}
```

O log só recebe acréscimos: uma linha nunca é editada nem apagada. Uma linha recusada continua no arquivo, e o `request_id` dela fica **queimado**.

## I.7 De onde vem o Mnemonic

| Etapa | Quem | O que faz |
|---|---|---|
| **Declaração** | Registro (`backup/capabilities.yaml`, capability `backup_repository`, campo `mnemonic: BKP_REPO`) | É a **origem**. Rótulo fixo, declarado por escrito e alterado só por pull request. |
| **Leitura** | Engine (passos 1 e 2) | Descobre o Mnemonic ao ler o registro e ao HITL escolher a capability. |
| **Transporte** | Engine (passo 5) | Copia o Mnemonic para a linha, depois do GO. Não o calcula. |
| **Validação** | Dispatcher (passo 6) | Confere se o schema o aceita, se o mapeamento fixo o conhece e se o registro concorda. |
| **Tradução** | Dispatcher (passo 7) | Converte o Mnemonic em workflow: `BKP_REPO` → `backup-repository.yml`. |

Ninguém, em execução, **escolhe** o Mnemonic. Um Mnemonic novo precisa ser declarado, por pull request, em quatro lugares: o registro, o `enum` do schema da linha, o mapa do `dispatch_check.py` e um job do `dispatcher.yml`. Sem isso, o dispatcher recusa a linha.

## I.8 Situações de uma requisição e o que fazer

| Situação | Como se chega | O que fazer |
|---|---|---|
| **Proposta** | Passo 3 | O HITL revisa o ECR. |
| **Autorizada** | GO no passo 4 | O engine grava a linha. |
| **Recusada pelo Dispatcher** | Violação no passo 6 (saída 30) | Corrigir a causa. Se a linha já entrou no log, o `request_id` está queimado: a nova tentativa usa **outro** id, e a linha ruim permanece no log. |
| **`REJECTED`** | Inputs inválidos no passo 8 (saída 2) | Corrigir e gravar nova linha com novo id. |
| **`BLOCKED` (preflight)** | Lacunas sem decisão (saída 10) | O HITL decide `STOP` ou `CONTINUE_WITH_RESTRICTIONS` (passo 10). |
| **`BLOCKED` (destino)** | Destino não validado (saída 20) | Resolver a causa. Hoje depende do PR do OneDrive. |
| **`PREFLIGHT_OK`** | Preflight passou | A execução segue para o destino e a leitura da origem. |
| **`COMPLETE`, `COMPLETE_WITH_EXCEPTIONS`, `FAILED`** | Fim da preservação (passo 14) | O engine reporta (passo 15). Só o manifest validado prova a preservação. |

## I.9 Inventário de arquivos

Estados: **EXISTE**, **TEMPORÁRIO**, **A REMOVER**, **PLANEJADO**.

| Arquivo | Papel | Estado |
|---|---|---|
| `BACKUP_REPOSITORY.md` | Este documento. | EXISTE |
| `README.md` | Apresentação do repositório. | EXISTE |
| `commands.log` | Fila de comandos autorizados (JSON Lines, só acréscimo). O push nele dispara o Dispatcher. | EXISTE |
| `backup/capabilities.yaml` | Registro canônico: capabilities, Mnemonics, rotas, políticas e artefatos. | EXISTE |
| `backup/schemas/commands-log-line.schema.yaml` | Formato de cada linha do `commands.log`. | EXISTE |
| `backup/schemas/command-request.schema.yaml` | Formato do ECR, incluindo o Capability Preflight. | EXISTE |
| `backup/schemas/evidence.schema.yaml` | Formato da evidência de preservação (2.0). | EXISTE |
| `backup/schemas/backup-manifest.schema.yaml` | Formato do manifest e da reconciliação (2.0). | EXISTE |
| `.github/workflows/dispatcher.yml` | Dispara no push do `main`; valida e chama o workflow do Mnemonic. | EXISTE |
| `backup/scripts/dispatch_check.py` | Verificações do Dispatcher; produz a matriz de despacho. | EXISTE |
| `.github/workflows/backup-repository.yml` | Executor do `BKP_REPO`. Seus inputs são o ECR. | EXISTE |
| `backup/scripts/bkp_repo.py` | Revalidação, preflight, gate de destino e montagem da evidência. | EXISTE |
| `.github/workflows/diagnostic-git-read.yml` | Teste manual da leitura Git. Não faz parte do fluxo e não prova preservação. | TEMPORÁRIO |
| `.github/workflows/onedrive-appfolder-oidc-read-test.yml` | Teste OIDC antigo, contrário à regra "sem OIDC". | A REMOVER (SA-08) |
| `backup/scripts/onedrive.py` | Access token, rotação do secret, destino, upload em blocos e SHA-256. | PLANEJADO (SA-08) |
| `backup/scripts/onedrive_authorize.py` | Login local único (device code) que entrega o refresh token. | PLANEJADO (SA-08) |
| `backup/scripts/validate_traceability.py` | Confere o registro contra o repositório (existência, SHA, órfãos, caminhos, evidência de `VALIDATED`) e atualiza os SHAs com `--update`. | EXISTE |
| `.github/workflows/traceability.yml` | Roda o script em todo pull request. | EXISTE |

## I.10 Segredos e variáveis

Ficam em Settings → Secrets and variables → Actions do `repo_backup`. Os valores **nunca** aparecem em chat, log, evidência ou arquivo.

| Nome | Tipo | Quem cria | Quem usa | Estado |
|---|---|---|---|---|
| `REPOSITORY_PRESERVATION_APP_ID` | variável | HITL | Token do App Reader (`backup-repository.yml`, diagnóstico) | EXISTE |
| `REPOSITORY_PRESERVATION_APP_PRIVATE_KEY` | secret | HITL | Idem | EXISTE |
| `ONEDRIVE_CLIENT_ID` | variável | HITL | `onedrive.py` | PLANEJADO |
| `ONEDRIVE_REFRESH_TOKEN` | secret | HITL (valor inicial) e App Writer (rotação) | `onedrive.py` | PLANEJADO |
| `REPOSITORY_PRESERVATION_SECRETS_APP_ID` | variável | HITL | Gravação do secret pelo App Writer | PLANEJADO |
| `REPOSITORY_PRESERVATION_SECRETS_APP_PRIVATE_KEY` | secret | HITL | Idem | PLANEJADO |

O environment `onedrive-backup` e as variáveis `AZURE_CLIENT_ID` e `AZURE_TENANT_ID` pertencem ao teste OIDC antigo e ficam obsoletos; o HITL as apaga depois do SA-08. O `secrets: inherit` repassa só secrets de repositório e de organização, **não** de environment.

## I.11 Autenticação do OneDrive (**PLANEJADO**)

- O OneDrive é **pessoal**, o que exige permissão **delegada**: um login real, com consentimento. O workflow roda sem ninguém presente e não consegue fazer esse login.
- Por isso o login é feito **uma vez**, no computador do HITL, por `onedrive_authorize.py` (device code). A Microsoft entrega um **refresh token**, que o script imprime **só no terminal**, nunca em arquivo, repositório, log ou chat. O HITL o grava no secret `ONEDRIVE_REFRESH_TOKEN`.
- A cada backup, o workflow troca o refresh token por um access token de curta duração, pela autoridade `https://login.microsoftonline.com/consumers`, sem client secret e sem tenant ID (app público). O destino é `/me/drive/special/approot`, isto é, `Apps/<nome do registro>/`.
- **Rotação:** a Microsoft devolve um refresh token novo a cada uso. O App Writer grava o novo valor no secret **antes** de qualquer outra etapa que use o OneDrive. Se a gravação falhar, o workflow para com erro claro.
- Repetir o login só se o token expirar por falta de uso (cerca de 90 dias, segundo a documentação da Microsoft; **a confirmar**).

## I.12 Códigos de saída e contrato de resultado

### Códigos de saída

| Código | Onde | Significado |
|---|---|---|
| 0 | todos | Etapa concluída. |
| 1 | `bkp_repo.py build-evidence` | Preservação com objeto `FAILED`. |
| 2 | `bkp_repo.py validate-inputs`; diagnóstico | Entrada rejeitada (schema, `preflight_decision` ilegível ou reutilização de autorização). |
| 3 | passo de LFS (bash) | Não foi possível listar os ponteiros LFS. |
| 4, 5, 6 | passo de LFS (bash) | OID inválido, objeto LFS ausente, hash LFS divergente. |
| 10 | `bkp_repo.py preflight` | `BLOCKED`: lacunas materiais sem decisão do HITL. |
| 20 | `bkp_repo.py validate-destination` | Destino não validado (falha fechado). |
| 21 | `bkp_repo.py build-evidence` | Recusa de gerar evidência sem destino validado. |
| 30 | `dispatch_check.py` | Violação no push: nada é despachado. |
| 64 | `bkp_repo.py`, `dispatch_check.py`, `validate_traceability.py` | Uso incorreto ou variável de ambiente ausente. |
| 1 | `validate_traceability.py` | O registro não bate com o repositório (a linha `ERRO <CÓDIGO>` diz o quê). |

### Linhas `BKP_RESULT` e afins

O workflow do `BKP_REPO` imprime, no log do job, **uma linha `BKP_RESULT {json}`** por desfecho (a mesma vai para o resumo do job). É ela que o engine lê.

| `status` | Quando | Campos principais |
|---|---|---|
| `REJECTED` | Entrada inválida | `reason`: `INPUT_VALIDATION` ou `AUTHORIZATION_REUSE` |
| `BLOCKED` | Preflight com lacunas sem decisão | `reason`: `CAPABILITY_PREFLIGHT_GAPS`, `hitl_decision`: `PENDING`, `required_restrictions` |
| `BLOCKED` | Destino não validado | `reason`: `DESTINATION_NOT_VALIDATED`, `detail` |
| `PREFLIGHT_OK` | Preflight passou | `preflight_status`, `hitl_decision`, `accepted_restrictions` |
| `COMPLETE`, `COMPLETE_WITH_EXCEPTIONS`, `FAILED` | Fim da preservação | `evidence_sha256`, `manifest_sha256`, `reconciliation` |

Outras linhas úteis: `DISPATCH_ACCEPTED {json}` (job `guard` do Dispatcher) e `DIAG_RESULT {json}` (diagnóstico da leitura Git). O log do job só fica legível depois que o job termina. A evidência bruta fica como artefato do Actions (`bkp-repo-<request_id>`, 7 dias; `diag-git-read-<run>`, 3 dias).

## I.13 Ciclo de vida dos temporários e manutenção

- **`diagnostic-git-read.yml`** é TEMPORÁRIO: existe porque o gate de destino falha fechado antes do clone. Sai, com o registro dele no `capabilities.yaml`, quando o OneDrive funcionar.
- **`onedrive-appfolder-oidc-read-test.yml`** sai no SA-08.
- **`backup_projects`** aparece no menu, mas não tem Mnemonic nem executor: hoje não gera comando.
- **Registro de artefatos:** depois de alterar qualquer arquivo registrado, rode `python3 backup/scripts/validate_traceability.py --update` e confira com `--check`. O `traceability.yml` roda o `--check` em todo pull request. Um artefato só fica `VALIDATED` com evidência registrada (URL do run e commit) e a nota do que foi e do que não foi provado.
- **Manutenção deste documento:** ao criar, mover ou remover um arquivo, um segredo ou um código de saída, atualize as seções I.9, I.10 e I.12 no mesmo PR. Itens *planejados* viram *existentes* no PR que os implementa.

## I.14 Restauração e Etapa 2 (**PLANEJADO**)

**Objetivo.** Depois de salvo no OneDrive, o backup deve poder ser acessado direto de lá e **restituído ao GitHub**. Hoje **nada disso existe**: o plano cobre a preservação do Git e o envio ao OneDrive, mas não há capability, procedimento ou teste de restauração.

**O que muda no plano**
1. **Pacote restaurável** no passo 13: bundle de todas as refs, objetos LFS e lista das refs.
2. **Teste de restauração** logo depois do primeiro backup real: restaurar para um repositório **novo e vazio** e registrar o resultado. Um backup só está provado quando a restauração funciona.
3. **Capability `restore_repository`** no menu, com Mnemonic e workflow próprios, sob GO do HITL, no mesmo padrão do `BKP_REPO`.
4. **Etapa 2: preservar e restaurar as 17 classes**, uma de cada vez. Cada classe implementada é acrescentada a `implemented_classes` no registro, e a restrição correspondente deixa de ser exigida. As classes **temporais** (que expiram) vêm primeiro, como manda o protocolo (seção II.12).

**Níveis de restauração**

| Nível | O que volta | Classes |
|---|---|---|
| **1. Código e histórico** | Branches, tags, histórico, LFS e submódulos, com `git push --mirror` para um repositório novo. Os arquivos de workflow vão junto, pois são arquivos Git. | `git` (**coberto** pelo plano atual) |
| **2. Configuração** | Recriada pela API a partir dos dados preservados. Segredos de webhook são reemitidos. | `labels`, `milestones`, `rules_and_branch_protection`, `environments`, `variables`, `collaborators`, `webhooks` |
| **3. Conteúdo colaborativo** | Recriado como **cópia equivalente**, sem a fidelidade original. | `issues`, `pull_requests`, `projects` |
| **Sem restauração** | Fica como registro e evidência. | `actions` (execuções, logs, artefatos), `checks`, `actions_caches`, `deployments`, `codespaces_repository_state_delta` (o conteúdo pode ser recuperado como commits ou patch), `secret_metadata` (valores não são exportáveis; os segredos são criados de novo), `other_discovered_state` (depende do que for descoberto) |

O modo de restauração de cada classe é **previsto** e será confirmado no teste de restauração.

**Limites aceitos: o que não volta idêntico**
- As refs `refs/pull/*`: o GitHub as gerencia e não aceita enviá-las de volta.
- O número, o autor e as datas originais de issues e pull requests.
- O histórico de execuções do Actions e os artefatos.
- Os valores de segredos.

Quando a restauração existir, esses limites entram no relatório final como limitações explícitas, e não como falhas.

## I.15 Facilidades opcionais

### Fazer o check de rastreabilidade bloquear merges (opcional, hoje **não ativado**)

**O que é.** O check **"Registry traceability"** (workflow `traceability.yml`) roda em todo pull request e mostra ✔ ou ✘. Por padrão ele **não impede o merge**. Se você quiser que um PR com o registro incoerente não possa ser mergeado, exija esse check na proteção de branch.

**Como ativar (uma vez)**
1. Settings → Branches → **Add branch protection rule** (ou Settings → Rules → Rulesets → New branch ruleset).
2. Em "Branch name pattern", informe `main`.
3. Marque **Require status checks to pass before merging**.
4. Em "Search for status checks", escolha **Registry traceability**. O check só aparece na lista se tiver **concluído com sucesso** no repositório nos **últimos 7 dias**: abra ou atualize um pull request para ele rodar e passar.
5. Salve.

**Cuidado: não quebre a gravação do `commands.log`.** O engine grava o `commands.log` direto no `main`, sem pull request. Regras que exigem pull request ou checks podem recusar essa gravação e parar o fluxo.

| Ponto | Situação |
|---|---|
| Por padrão, as restrições da regra **não se aplicam a quem tem permissão de administrador** no repositório | **Confirmado** na documentação do GitHub |
| A opção **"Do not allow bypassing the above settings"** faz as restrições valerem também para administradores | **Confirmado** na documentação |
| "Require a pull request before merging" impede push direto em condições normais | **Confirmado** na documentação (com a exceção de administradores acima) |
| O check só pode ser exigido se tiver concluído com sucesso nos últimos 7 dias | **Confirmado** na documentação |
| A credencial que o engine usa para gravar o `commands.log` é tratada como **administrador** e passa pela regra | **Confirmado em teste real** (29/09/2026): um push direto ao `main`, com a regra ativa, foi aceito com o aviso `Bypassed rule violations ... Required status check "Registry traceability" is expected`. O Dispatcher rodou verde. Commit `42a211e`, run `36622116351` |

Por isso:
- **não** marque "Require a pull request before merging";
- **não** marque "Do not allow bypassing the above settings", para o administrador (`Moriblo`, a identidade que o engine usa hoje) continuar podendo gravar o log;
- depois de ativar, **faça uma gravação real de teste** antes de depender da regra. Foi o que se fez em 29/09/2026 (linha acima da tabela). Se você trocar a credencial do engine, repita o teste. Se a gravação for recusada, desative a regra ou ajuste-a.

Fontes (documentação do GitHub): [Sobre branches protegidos](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches), [Gerenciar uma regra de proteção de branch](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule) e [Solução de problemas de status checks obrigatórios](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks).

**Como desativar.** Remova a regra em Settings → Branches.

**Lembrete.** O controle de escrita direta no `main` continua sendo detectivo (passo 6): ele avisa com um run vermelho, mas não impede o push.

---

# Parte II: Protocolo normativo

> Esta parte é **normativa**. Ela traduz, sem mudar o sentido, as regras do protocolo original em inglês. Os nomes de campos, códigos e estados permanecem no original.

## II.1 Finalidade

Ponto de entrada oficial e portável do processo de preservação completa de repositórios (Full Repository Preservation Process). Um engine de IA **DEVE** conseguir começar sem memória de conversas anteriores.

## II.2 Sequência obrigatória de execução

```text
Capability (capacidade escolhida)
    ↓
Parâmetros
    ↓
Exact Command Request (pedido de comando exato)
    ↓
GO do HITL
    ↓
Capability Preflight (verificação prévia de capacidade)
    ↓
Avaliar a capacidade efetiva de execução para tudo que possa afetar a preservação
    ↓
        LACUNA MATERIAL?
       /                \
     NÃO                SIM
      |                  |
      |          divulgar exatamente:
      |          - o que não pode ser lido ou descoberto
      |          - a classificação da lacuna
      |          - a capacidade requerida
      |          - as rotas avaliadas
      |          - a causa
      |          - o impacto na preservação
      |          - a restrição resultante
      |                  ↓
      |                HITL
      |          /               \
      |        STOP   CONTINUE_WITH_RESTRICTIONS
      |          |                |
      |          |      registrar as restrições aceitas
      |          |                |
      |          └────────┬───────┘
      |                   |
      └───────────────────┘
                          ↓
                   Source Inventory (inventário da origem)
```

Esta sequência **NÃO DEVE** ser encurtada, reordenada nem ignorada em silêncio para `backup_repository`.

## II.3 Regras inegociáveis

1. Nunca inferir autorização.
2. Nunca alterar o `SOURCE_REPOSITORY` (a origem).
3. Nunca herdar parâmetros específicos de uma execução anterior.
4. Nunca tratar a falta de evidência como sucesso.
5. Nunca expor segredo em texto puro em chat, logs, evidência em texto puro ou artefatos comuns de preservação.
6. O HITL vale apenas para o Command Request exato apresentado.
7. A autorização não é transitiva nem reutilizável.
8. O backup nunca autoriza limpeza, exclusão, renomeação, arquivamento ou alteração da origem.
9. A incapacidade de ler ou descobrir um objeto **NÃO DEVE** ser interpretada como prova de que o objeto não existe.
10. Toda restrição aceita pelo HITL **DEVE** permanecer rastreável pelo Source Inventory, pela evidência, pela reconciliação, pelo manifest e pelo relatório final.
11. Uma limitação atual do engine ou do conector **NÃO DEVE**, por si só, reabrir uma lacuna arquitetural para a qual já existe rota aprovada.
12. `FACTUAL_INVENTORY_PENDING` não é uma lacuna do Capability Preflight.
13. Nenhuma capability de preservação depende, para a sua existência arquitetural, de um único engine, conector, workflow ou executor determinístico.
14. O Source Inventory **NÃO DEVE** começar antes de o gate do Capability Preflight estar resolvido.

## II.4 Procedimento de início

1. Ler este documento e `backup/capabilities.yaml`, na versão atual do `main` do repositório `Moriblo/repo_backup` (nunca de memória de conversas anteriores).
2. Apresentar o Capability Menu.
3. Aguardar o humano escolher uma capability.
4. Obter, ou confirmar explicitamente, todo parâmetro de execução requerido.
5. Montar o Exact Command Request com `backup/schemas/command-request.schema.yaml`.
6. Apresentar esse Exact Command Request ao HITL quando exigido.
7. Executar somente depois do `GO`.
8. Rejeitar em caso de `NO-GO`, parâmetro ausente, divergência de escopo ou ausência de autorização.
9. Para `backup_repository`, executar o Capability Preflight obrigatório depois do `GO` e antes do Source Inventory.
10. Avaliar a capacidade efetiva de execução para cada classe de objeto aplicável.
11. Se não houver lacuna material, seguir para o Source Inventory.
12. Se houver lacuna material, divulgá-la e pedir `STOP` ou `CONTINUE_WITH_RESTRICTIONS`.
13. Em caso de `STOP`, não iniciar o Source Inventory.
14. Em caso de `CONTINUE_WITH_RESTRICTIONS`, registrar cada restrição aceita com um `restriction_id` estável e prosseguir somente sob essas restrições.
15. Preservar as restrições aceitas na evidência, na reconciliação, no manifest e no relatório final.
16. Coletar e validar a evidência.
17. Reportar o estado da preservação, as limitações, as restrições aceitas, o resultado da reconciliação e o próximo HITL aplicável.

## II.5 Fluxo de comando governado (Governed command flow)

Caminho: engine → `commands.log` → dispatcher → workflow.

1. O engine lê este documento e apresenta o Capability Menu. O HITL escolhe uma capability.
2. O `backup/capabilities.yaml` associa a capability ao seu Mnemonic (`backup_repository` → `BKP_REPO`) e aos parâmetros requeridos. O HITL informa os valores.
3. O workflow ligado ao Mnemonic (`command_workflow.artifact_id`, cujo caminho é resolvido no registro de artefatos) define integralmente o Exact Command Request: os `inputs` do `on: workflow_call` mais as restrições declaradas em suas descrições. O engine lê o workflow e apresenta o ECR exatamente como definido.
4. Somente depois do `GO`, o engine acrescenta uma linha JSON ao `commands.log`, no `main` (`backup/schemas/commands-log-line.schema.yaml`): `request_id`, `mnemonic`, `ts`, `authorization` e os `params` autorizados. O conjunto de `params` varia conforme o schema de cada Mnemonic. O `commands.log` só recebe acréscimos e é o único arquivo escrito diretamente no `main`.
5. O `dispatcher.yml` roda em todo push no `main` (para que o controle de escrita direta também veja pushes que não tocam o `commands.log`), despacha somente quando o `commands.log` mudou, lê só as linhas novas, valida-as (schema, `request_id` único, nenhuma edição de linhas existentes e nenhum push direto de arquivos além do `commands.log`, exceto se o commit pertencer a um pull request mergeado; qualquer violação falha o push inteiro e não despacha nada) e chama o workflow do Mnemonic por um mapeamento fixo (`workflow_call`, `secrets: inherit`).
6. O workflow revalida seus inputs, executa o Capability Preflight, executa e produz evidência no schema 2.0. O engine reporta o resultado ao HITL.

O workflow reporta cada desfecho como uma linha `BKP_RESULT {json}` no log do job (status `REJECTED`, `BLOCKED`, `PREFLIGHT_OK`, `COMPLETE`, `COMPLETE_WITH_EXCEPTIONS`, `FAILED`). O engine lê essa linha para reportar ao HITL. Um resultado `BLOCKED` por lacunas do preflight lista `required_restrictions`, e a linha de continuação no `commands.log` **DEVE** aceitar todas elas. A validação do destino falha fechada antes do Source Inventory, e a evidência só é gerada depois que ela passa.

Lacunas do Capability Preflight: o workflow roda sem supervisão, então uma lacuna material encerra a execução como `BLOCKED`, com evidência `GAPS_IDENTIFIED`. O HITL decide então `STOP` ou `CONTINUE_WITH_RESTRICTIONS`. A segunda exige uma nova linha no `commands.log` com `preflight_decision` e valores de `restriction_id` estáveis. A autorização nunca é reutilizada.

Limites: alterações de código somente por pull request; escrita direta no `main` somente no `commands.log` e somente depois do `GO`; a origem é sempre SOMENTE LEITURA; o OneDrive usa OAuth delegado (`Files.ReadWrite.AppFolder`), nunca OIDC.

## II.6 Capability Menu

As definições canônicas estão em `backup/capabilities.yaml`.

- `backup_repository`
- `backup_projects`
- `list_capabilities`
- `show_status`
- `validate_evidence`
- `help`

O engine **NÃO DEVE** escolher em silêncio uma capability de preservação.

Só uma capability que tenha Mnemonic e workflow ligado pode produzir uma linha no `commands.log`. Hoje, isso vale apenas para `backup_repository` (`BKP_REPO`). O `backup_projects` não tem Mnemonic nem executor (`NOT_MATERIALIZED`). As capabilities informativas e de validação são respondidas pelo engine na conversa e nunca produzem linha de comando.

## II.7 Obtenção de parâmetros pelo HITL

Os valores específicos de cada execução **DEVEM** ser informados ou confirmados explicitamente pelo humano. O contexto da conversa **PODE** propor um valor, mas **NÃO DEVE** substituir a confirmação.

`backup_repository` exige, no mínimo:
- `source_repository`;
- `destination`.

`backup_projects` exige:
- `scope`: `SOURCE_REPOSITORY`, `PROJECT` ou `OWNER_PROJECT_SET`;
- os `scope_identifiers` correspondentes;
- `destination`.

Alterar qualquer parâmetro de execução depois da autorização exige um novo Exact Command Request e uma nova autorização do HITL.

## II.8 Capability Preflight

### Finalidade
O Capability Preflight responde a uma única pergunta antes do Source Inventory:

> O processo de execução autorizado tem capacidade efetiva de LEITURA e descoberta para tudo que possa afetar materialmente o escopo de preservação pedido?

Ele não pergunta apenas o que o engine de IA ou o conector atual consegue fazer.

### Capacidade efetiva de execução
A capacidade efetiva de execução é avaliada a partir da combinação autorizada de rotas disponíveis para a execução, incluindo, quando aplicável:

- capacidade nativa do engine;
- conectores;
- Git;
- APIs REST;
- APIs GraphQL;
- rotas autorizadas de API ou de identidade;
- capacidades especializadas;
- executores determinísticos.

Cada rota **DEVE** ser avaliada separadamente. A avaliação agregada determina se a capacidade de LEITURA e descoberta requerida está disponível.

Um executor determinístico **PODE** implementar uma rota, mas não é a definição da capability em si.

As referências a executores determinísticos estão registradas em `backup/capabilities.yaml`. `github_actions:backup-repository.yml` está `MATERIALIZED` (workflow `.github/workflows/backup-repository.yml`, executor do Mnemonic `BKP_REPO`). `github_actions:backup-projects.yml` está `NOT_MATERIALIZED`, e a sua existência histórica é `NOT_DETERMINED`. A ausência de um executor **NÃO DEVE** ser tratada, por si só, como prova de que uma capability arquitetural não existe.

### Terminologia de roteamento
Os resultados de roteamento são:

- `DIRECT`
- `ROUTED`
- `PARTIAL`
- `LIMITATION`

`WORKFLOW` é preservado como mecanismo histórico e especializado de rota. Não é sinônimo do resultado generalizado `ROUTED`.

### Classificação
O Capability Preflight distingue:

- `ARCHITECTURAL_GAP`: não há arquitetura suficiente de preservação ou leitura definida para um requisito material;
- `EXECUTION_CAPABILITY_GAP`: a arquitetura existe, mas a execução autorizada no momento não tem rota operacional suficiente;
- `ACCESS_PERMISSION_GAP`: uma rota aplicável está bloqueada por acesso, autorização, identidade ou permissão;
- `FACTUAL_INVENTORY_PENDING`: a arquitetura e a rota existem, mas um fato da instância da origem só pode ser estabelecido durante o Source Inventory.

Só os três primeiros são lacunas materiais do Capability Preflight.

`FACTUAL_INVENTORY_PENDING` **DEVE** ser registrado à parte. **NÃO DEVE** ser colocado em `gaps[]`, **NÃO DEVE** mudar `PASS` para `GAPS_IDENTIFIED` e **NÃO DEVE** acionar, por si só, o segundo HITL.

### HITL diante de lacuna material
Para cada lacuna material, divulgar:

- a classe de objeto;
- o que não pode ser lido ou descoberto;
- a classificação da lacuna;
- a capacidade requerida;
- as rotas avaliadas;
- a causa;
- o impacto na preservação;
- a restrição resultante.

O HITL **DEVE** decidir explicitamente:

- `STOP`; ou
- `CONTINUE_WITH_RESTRICTIONS`.

`STOP` impede que o Source Inventory comece.

`CONTINUE_WITH_RESTRICTIONS` autoriza o Source Inventory somente sob as restrições explicitamente aceitas pelo HITL. Cada restrição aceita **DEVE** receber um `restriction_id` estável.

Uma limitação **NUNCA DEVE** ser convertida em prova de que um objeto está ausente, não se aplica, foi preservado ou foi verificado com sucesso.

## II.9 Registro de capabilities e requisitos de roteamento

O `backup/capabilities.yaml` é o registro principal, legível por máquina, das políticas e controles de:

- classes de objeto aplicáveis;
- capacidades de LEITURA e descoberta requeridas;
- tipos de rota aprovados;
- controles de preservação temporal;
- controles de Informação Protegida;
- semântica de alertas;
- preservação de Projects;
- delta de estado do repositório em Codespaces;
- controle de descoberta de conjunto aberto O-24.

As rotas definidas na arquitetura continuam definidas mesmo quando uma execução específica não consegue usá-las no momento. A disponibilidade operacional é estabelecida pelo Capability Preflight.

## II.10 Rastreabilidade entre capability e artefato de implementação

O `backup/capabilities.yaml` é o único registro canônico da rastreabilidade entre capability e artefato. Um registro separado de artefatos **NÃO DEVE** ser exigido.

Todo artefato de implementação, declarativo ou executável, que implemente, valide, defina schema, configure ou apoie o sistema de capabilities **DEVE** ter um `artifact_id` lógico estável e relações explícitas com um ou mais valores de `capability_id` registrados.

Os tipos canônicos de relação são:

- `EXECUTES`
- `VALIDATES`
- `SCHEMATIZES`
- `CONFIGURES`
- `SUPPORTS`

O modelo de relação é N:M. Uma capability **PODE** se relacionar com vários artefatos, e um artefato **PODE** se relacionar com várias capabilities. A relação autoritativa é guardada uma única vez no registro de artefatos de implementação. A consulta inversa (artefato → capability) **DEVE** ser derivada dessas relações, e não mantida como uma segunda fonte de verdade.

Cada artefato de implementação registrado guarda, no mínimo:

- `artifact_id` estável;
- tipo do artefato;
- repositório e caminho;
- finalidade;
- estado de materialização;
- mecanismo de rastreio de integridade e versão;
- estado de validação;
- relações com capabilities.

### Materialização e integridade
`MATERIALIZED` e `NOT_MATERIALIZED` descrevem se o artefato de implementação registrado existe no momento. São distintos do estado de validação e de integridade.

Para artefatos materializados versionados em Git, o SHA de blob Git atual é o identificador canônico de integridade e de versão do conteúdo, e o SHA do commit Git é a proveniência da mudança. O SHA de blob atual **DEVE** ser obtido e comparado durante a validação de rastreabilidade. Um artefato **NÃO DEVE** ser obrigado a embutir o próprio SHA de blob.

Um executor ou workflow `NOT_MATERIALIZED` **PODE** continuar registrado como referência arquitetural de implementação. A sua ausência não remove, por si só, a definição da capability.

### Integridade referencial e detecção de referência quebrada
Para todo artefato `MATERIALIZED` registrado, o repositório e o caminho registrados **DEVEM** resolver para um objeto existente. A falha produz `BROKEN_REFERENCE` e **NÃO DEVE** ser interpretada como validação bem-sucedida.

Um artefato `NOT_MATERIALIZED` registrado pode não ter nenhum objeto no seu caminho futuro.

### Detecção de artefato órfão
As localizações de artefatos de implementação **DEVEM** ser verificadas em busca de artefatos do sistema de capabilities que existam sem `artifact_id` registrado. Esse objeto é um `ORPHAN_ARTIFACT`.

A detecção de órfãos vale para artefatos de implementação dentro das localizações gerenciadas e **NÃO DEVE** classificar como artefato de implementação qualquer arquivo não relacionado do repositório apenas porque ele existe.

### Análise de impacto e revalidação
Sempre que um artefato de implementação materializado mudar:

1. identificar o seu `artifact_id` estável;
2. derivar, das relações canônicas, todas as capabilities afetadas;
3. marcar a relação entre capability e artefato afetada para revalidação;
4. executar a validação aplicável antes de tratar a implementação alterada como validada.

Uma mudança no SHA de blob Git dispara, portanto, a análise de impacto. Ela não muda o `artifact_id` lógico.

Estas regras de rastreabilidade são genéricas e valem igualmente para capabilities atuais e futuras. Nenhuma capability recebe um modelo de rastreabilidade privado ou mais fraco.

## II.11 Preservação Git

A classe de objeto Git inclui os requisitos de:

- preservar o grafo completo de objetos Git com semântica de espelho completo (full mirror);
- preservar todas as refs materiais, inclusive as refs de pull request quando presentes;
- detectar Git LFS e preservar e verificar os objetos LFS quando usados;
- detectar submódulos e registrar uma disposição explícita;
- preservar uma árvore navegável do repositório, separada do mirror Git;
- reconciliar as refs e os SHAs de destino da origem e do preservado;
- verificar a integridade dos objetos Git.

São requisitos de preservação, não permissão para alterar a origem.

## II.12 Preservação temporal, MRBI e CRR

As classes temporais e voláteis **DEVEM** ser priorizadas antes dos objetos estáveis assim que a execução do backup for autorizada.

O `MRBI` (Maximum Recommended Backup Interval, intervalo máximo recomendado de backup) é regido pela menor janela de retenção determinística aplicável entre as classes de preservação temporal descobertas.

Um risco de expulsão sem prazo **NÃO DEVE** ser convertido em MRBI numérico. Ele produz `VOLATILITY_ALERT`.

O `CRR` (Current Repository Retention, retenção atual do repositório) é a retenção efetiva aplicável observada para a origem. **DEVE** ser lido e preservado como evidência quando aplicável e **NÃO DEVE** ser inferido de um padrão do produto.

Os alertas canônicos são:

- `RETENTION_ALERT`
- `VOLATILITY_ALERT`
- `ACCESSIBILITY_ALERT`

A indisponibilidade temporal **DEVE** ser evidenciada, e não tratada em silêncio como ausência na origem.

## II.13 Informação Protegida

O tratamento de Informação Protegida prioriza metadados e considera a recuperabilidade.

O texto puro de um segredo não é exigido para afirmar a completude arquitetural quando a plataforma de origem não o expõe. Preservar os metadados permitidos, o escopo, a proveniência, as relações, as referências à fonte autoritativa, a recuperabilidade, o estado de preservação do valor e o tratamento de reconstrução ou reemissão.

Qualquer tentativa de recuperar o texto puro de uma fonte autoritativa externa, quando não coberta pelo Exact Command Request, exige autorização específica do HITL para a operação.

## II.14 Webhooks e entregas

Preservar a configuração legível dos webhooks e a evidência legível das entregas.

As entregas de webhook são evidência temporal e ficam sujeitas aos controles de preservação temporal.

O texto puro do segredo do webhook não é exigido quando a plataforma não o expõe. Preservar a presença, os metadados e o tratamento de reemissão quando observáveis.

Os sistemas externos alcançados pelos webhooks continuam sendo apenas referência, salvo se forem incluídos no escopo de forma separada e explícita. A preservação de webhooks **NÃO DEVE** ampliar em silêncio a autorização para sistemas externos.

## II.15 Backup Projects

O `backup_projects` pode ser chamado de forma independente e também é incluído pelo `backup_repository` quando existem Projects relacionados.

Escopos aprovados:

- `SOURCE_REPOSITORY`
- `PROJECT`
- `OWNER_PROJECT_SET`

A preservação de Projects pode exigir superfícies de LEITURA REST e GraphQL autorizadas, combinadas. Nenhuma superfície única é presumida completa.

Preservar, quando exposto e aplicável:

- identidade e metadados;
- relações com repositórios;
- campos, opções e configuração;
- itens;
- draft issues;
- referências a issues e pull requests;
- valores de campos;
- estado de itens arquivados;
- views e configuração;
- workflows e automações;
- atualizações de status;
- outros estados de Project descobertos.

A representação preservada **DEVE** ser legível por máquina, controlada por schema, reconstruível como representação equivalente, ciente de proveniência e reutilizável adiante.

A ausência de uma superfície Project Graph no engine ou conector atual é um fato da superfície de execução, e não, automaticamente, uma lacuna arquitetural.

## II.16 Delta de estado do repositório em Codespaces

O escopo autoritativo de preservação para Codespaces é apenas o delta de estado do repositório:

- commits não enviados;
- arquivos rastreados modificados;
- conteúdo do repositório não rastreado.

A ordem exigida é:

`descobrir → avaliar acessibilidade → verificar o delta de estado do repositório → registrar evidência e estado → avaliar a retenção`.

O estado da VM ou do ambiente fora do delta de estado do repositório fica excluído, salvo se incluído no escopo separadamente.

Uma exportação oficial, por si só, **NÃO DEVE** ser tratada como prova de que a preservação do delta de estado do repositório está completa.

O risco de acessibilidade e o risco de retenção são independentes. Usar `ACCESSIBILITY_ALERT` e os alertas temporais conforme aplicável.

## II.17 O-24: controle de descoberta de conjunto aberto

`other_discovered_state` não é uma lacuna arquitetural em aberto. É o controle O-24 de descoberta de conjunto aberto.

Todo novo estado material da origem ou da plataforma que for descoberto **DEVE** ser:

1. classificado;
2. associado a uma rota de preservação aplicável ou a uma disposição explícita;
3. evidenciado;
4. reconciliado.

**NÃO DEVE** ser omitido em silêncio.

## II.18 Fronteira de execução

`Moriblo/repo_backup` é o repositório de execução e controle.

As rotas de execução autorizadas podem LER a origem autorizada e ESCREVER somente no destino de preservação autorizado. **NÃO DEVEM** escrever no `SOURCE_REPOSITORY`.

Nenhum workflow específico do GitHub Actions é exigido para a existência arquitetural de uma capability.

## II.19 Estados de preservação

Todo objeto material descoberto na origem recebe, ao final, um destes estados:

- `PRESERVED`
- `PRESERVED-AS-EQUIVALENT-REPRESENTATION`
- `PARTIALLY-PRESERVED`
- `NON-EXPORTABLE`
- `FAILED`
- `NOT-VERIFIED`

Nenhum estado material pode ser omitido em silêncio.

## II.20 Evidência, restrições, manifest e relatório final

O `backup/schemas/evidence.schema.yaml` registra a evidência de preservação e carrega as restrições do Capability Preflight por referências `restriction_id` estáveis.

O `backup/schemas/backup-manifest.schema.yaml` é a reconciliação autoritativa, legível por máquina, dos artefatos preservados, das restrições aceitas, das limitações e das contagens de reconciliação.

Enquanto não existir um schema dedicado de relatório final, o manifest validado é a entrada autoritativa para as restrições aceitas e as limitações no relatório final. O relatório final **NÃO DEVE** remover, enfraquecer nem resolver em silêncio as restrições presentes no manifest.

Um resultado `COMPLETE` limpo só é permitido quando não houver:

- restrições aceitas;
- limitações;
- objetos parciais;
- objetos não exportáveis;
- objetos com falha;
- objetos não verificados;
- classes de objeto sem reconciliação;
- e as restrições estiverem reconciliadas.

Caso contrário, uma preservação concluída com exceções explicitamente aceitas **DEVE** usar `COMPLETE_WITH_EXCEPTIONS`, sujeita à validação do schema.

A conclusão de um workflow, de uma rota ou de uma ferramenta, por si só, nunca prova a conclusão da preservação.

## II.21 Comportamento de falha fechada (fail-closed)

Parar em vez de inferir quando:

- faltarem parâmetros;
- o HITL for ambíguo;
- a autorização divergir do Exact Command Request;
- o escopo exceder a autorização;
- não for possível garantir a origem SOMENTE LEITURA;
- o destino não puder ser validado;
- a evidência for insuficiente;
- uma lacuna material do Capability Preflight não tiver recebido a decisão exigida do HITL.

Uma lacuna material do Capability Preflight não é automaticamente fatal. Ela é divulgada ao HITL. Se o HITL escolher `CONTINUE_WITH_RESTRICTIONS`, as restrições aceitas continuam sendo limitações materiais durante o Source Inventory, a evidência, a reconciliação, o manifest e o relatório final.

## II.22 Portabilidade

Nenhum engine pode depender de estado oculto de conversa, de execuções anteriores, de identificadores de repositório lembrados ou de um HITL anterior como autorização.

O contexto de execução é estabelecido pelo Exact Command Request atual e pela sua autorização do HITL.

O protocolo é agnóstico quanto ao engine: a capacidade é a capacidade efetiva de execução autorizada, e não o conjunto de recursos nativos do engine que por acaso esteja conversando com o humano.

---

# Parte III: Glossário

| Termo | Significado |
|---|---|
| **AppFolder** | Pasta que o OneDrive reserva a um aplicativo, em `Apps/<nome do registro>/`. O escopo `Files.ReadWrite.AppFolder` só permite gravar ali. |
| **Access token** | Credencial de curta duração para chamar uma API. Obtido a partir do refresh token. |
| **Artefato de implementação** | Arquivo que implementa, valida, define schema, configura ou apoia o sistema. Tem `artifact_id` estável no registro. |
| **BKP_REPO** | Mnemonic do backup de repositório. Liga a capability `backup_repository` ao workflow `backup-repository.yml`. |
| **BKP_RESULT** | Linha JSON que o workflow imprime no log do job com o desfecho de cada etapa. É ela que o engine lê. |
| **Burned (queimado)** | Diz-se do `request_id` que já entrou no `commands.log`. Não pode ser reutilizado, mesmo que a linha tenha sido recusada. |
| **Capability** | Uma função do menu (ex.: `backup_repository`). Só as que têm Mnemonic e workflow geram linha de comando. |
| **Capability Menu** | Lista das capabilities que o engine apresenta ao HITL no início. |
| **Capability Preflight** | Verificação prévia, depois do GO e antes do Source Inventory, de que a execução autorizada consegue LER tudo o que importa. |
| **commands.log** | Arquivo na raiz do `main` com uma linha JSON por comando autorizado. Só recebe acréscimos. |
| **CONTINUE_WITH_RESTRICTIONS** | Decisão do HITL de seguir aceitando explicitamente as restrições de uma lacuna material. |
| **CRR** | Current Repository Retention: retenção efetiva atual do repositório, lida como evidência e nunca inferida. |
| **Dispatcher** | Workflow `dispatcher.yml`. Valida o push e as linhas novas do log e chama o workflow do Mnemonic. |
| **Device code** | Forma de login em que o usuário digita um código numa página da Microsoft, usada uma vez para obter o refresh token. |
| **ECR (Exact Command Request)** | Pedido de comando exato: os inputs do workflow, com valores e restrições. O GO vale só para ele. |
| **Engine** | O assistente de IA que conduz o fluxo (hoje, o Claude Code). |
| **Evidência** | O `evidence.json`: o que foi capturado e a disposição de cada objeto. |
| **Fail-closed (falha fechada)** | Na dúvida, parar. Nunca inferir nem seguir por presunção. |
| **GO / NO-GO** | Autorização ou recusa do HITL, válida só para o ECR apresentado. |
| **Gitlink** | Entrada de submódulo numa árvore Git (modo `160000`). |
| **HITL** | Human in the loop: o humano que autoriza e decide. |
| **Lacuna material** | Falta de capacidade de leitura que exige decisão do HITL: `ARCHITECTURAL_GAP`, `EXECUTION_CAPABILITY_GAP` ou `ACCESS_PERMISSION_GAP`. |
| **LFS** | Git Large File Storage: arquivos grandes guardados fora do repositório Git, referenciados por ponteiros. |
| **Manifest** | O `manifest.json`: reconciliação legível por máquina do que foi preservado, das restrições e das limitações. |
| **Mirror** | Clone espelho (`git clone --mirror`): todas as refs e todo o grafo de objetos. |
| **Mnemonic** | Nome curto e fixo de um comando (`BKP_REPO`), declarado no registro e copiado para a linha do log. |
| **MRBI** | Maximum Recommended Backup Interval: intervalo máximo recomendado entre backups, dado pela menor janela de retenção. |
| **O-24** | Controle de descoberta de conjunto aberto: todo estado novo descoberto deve ser classificado, evidenciado e reconciliado. |
| **Órfão (`ORPHAN_ARTIFACT`)** | Arquivo de implementação numa pasta gerenciada, sem `artifact_id` registrado. |
| **SHA de blob** | Identificador do conteúdo de um arquivo no Git. O registro guarda o `current_sha` de cada artefato para detectar mudanças. |
| **Refresh token** | Credencial de longa duração que renova o access token. Obtida uma vez por login do HITL. |
| **Registro** | O `backup/capabilities.yaml`, fonte canônica de capabilities, Mnemonics, políticas e artefatos. |
| **restriction_id** | Identificador estável de uma restrição aceita (ex.: `RST-issues-EXECUTION_CAPABILITY_GAP`). |
| **Source Inventory** | Inventário da origem: levantamento dos objetos a preservar, depois do preflight resolvido. |
| **STOP** | Decisão do HITL de encerrar, sem iniciar o Source Inventory. |
| **`secrets: inherit`** | Faz o workflow chamado receber os secrets de repositório e de organização do chamador (não os de environment). |
| **`workflow_call`** | Forma de um workflow chamar outro. Os `inputs` do workflow chamado definem o ECR. |
