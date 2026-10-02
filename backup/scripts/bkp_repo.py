#!/usr/bin/env python3
"""Auxiliar determinístico do workflow BKP_REPO (backup-repository.yml).

O QUE É
    Um único script com quatro subcomandos. O workflow chama cada um em um
    ponto fixo da execução. Manter a lógica aqui (e não em YAML inline) permite
    testar localmente e revisar com facilidade.

ONDE SE ENCAIXA NO FLUXO
    Passo 6 do "Governed command flow" (BACKUP_REPOSITORY.md): o workflow
    recebe os valores autorizados, revalida, roda o preflight, executa e gera
    a evidência (schemas 2.0). Este script cuida de tudo isso, exceto do
    clone/verificação Git, que ficam como passos bash no próprio workflow.

SUBCOMANDOS
    validate-inputs       revalida os inputs contra commands-log-line.schema.yaml
                          (saída 2 = rejeitado)
    preflight             Capability Preflight
                          (saída 10 = BLOCKED, exige decisão do HITL)
    validate-destination  gate de destino, falha fechada: valida o OneDrive de verdade
                          (escrita, leitura e remoção de teste no AppFolder) e faz a
                          rotação do secret do refresh token
                          (saída 20 = destino não validado)
    build-evidence        monta e valida evidence.json e manifest.json
                          (saída 21 = destino não validado; 1 = FAILED)

CONTRATO DE SAÍDA
    Todo desfecho é impresso como UMA linha `BKP_RESULT {json}` no log do job.
    É essa linha que o engine lê para reportar ao HITL. O mesmo JSON vai para o
    resumo do job ($GITHUB_STEP_SUMMARY).

VARIÁVEIS DE AMBIENTE (definidas pelo workflow)
    REQUEST_ID, SOURCE_REPOSITORY, DESTINATION   inputs autorizados
    PREFLIGHT_DECISION                           JSON da decisão CONTINUE_WITH_RESTRICTIONS (opcional)
    TOKEN_OUTCOME                                resultado do passo que fornece o token de leitura (SOURCE_READ_TOKEN)
    PROJECTS_TOKEN_OUTCOME                       resultado do passo que fornece o token de Projects (PROJECTS_READ_TOKEN);
                                                 sem ele, a classe projects vira ACCESS_PERMISSION_GAP
    EVIDENCE_DIR                                 pasta de evidências (padrão: "evidence")
    DESTINATION_VALIDATED                        "true" só depois do gate de destino
    STARTED_AT                                   início da execução (opcional)

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - A origem é somente leitura: nada aqui escreve na origem.
    - Falta de evidência nunca vira sucesso: classes não lidas ficam NOT-VERIFIED.
    - Segredos não são lidos nem impressos.
"""
import datetime
import hashlib
import json
import os
import pathlib
import sys

import jsonschema
import yaml

# Raiz do repositório (este arquivo fica em backup/scripts/, dois níveis abaixo).
# Usada para achar capabilities.yaml e os schemas, independente do diretório atual.
ROOT = pathlib.Path(__file__).resolve().parents[2]

# Pasta onde as evidências são gravadas. O workflow define EVIDENCE_DIR=evidence.
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))

# Classificações de lacuna usadas no preflight (vocabulário de BACKUP_REPOSITORY.md).
# ACCESS_PERMISSION_GAP: a rota existe, mas o acesso/permissão a bloqueia.
GAP_ACCESS = "ACCESS_PERMISSION_GAP"
# EXECUTION_CAPABILITY_GAP: a arquitetura existe, mas esta execução não tem rota operacional.
GAP_EXEC = "EXECUTION_CAPABILITY_GAP"

# Classes lidas pela API: labels, milestones e issues por github_api_read.py (REST) e projects por
# github_projects_read.py (GraphQL). Só valem se estiverem em `implemented_classes`.
API_CLASSES = ("labels", "milestones", "issues", "projects")
# Arquivos de evidência de cada classe de API (além dos arquivos de dados, listados no status).
API_EVIDENCE = {c: ["api-status.json", "api-inventory.json"] for c in ("labels", "milestones", "issues")}
API_EVIDENCE["projects"] = ["api-projects-status.json", "api-projects-inventory.json"]
# O que cada classe de API NÃO devolve idêntico (vai para as limitações do manifest).
API_LIMITATION = {
    "labels": "Labels are preserved as API JSON (equivalent representation); restoring them requires recreating them through the API.",
    "milestones": "Milestones are preserved as API JSON (equivalent representation); restoring them requires recreating them through the API.",
    "issues": "Issues, comments, events and reactions are preserved as API JSON (equivalent representation): original numbers, authors and dates cannot be restored identically; relationships are limited to fields in the issue JSON; pull requests are not included.",
    "projects": "Projects are preserved as GraphQL JSON (equivalent representation): fields, views, workflows, status updates and items with field values; recreating a board requires the API and is not part of this backup.",
}



def load(path):
    """Lê um arquivo YAML relativo à raiz do repositório e devolve o conteúdo."""
    with open(ROOT / path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def now():
    """Data/hora atual em UTC, formato ISO 8601 com 'Z' (ex.: 2026-09-29T12:00:00Z)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def result(status, **extra):
    """Emite o desfecho da etapa como uma linha `BKP_RESULT {json}`.

    `status` é o estado (REJECTED, BLOCKED, PREFLIGHT_OK, COMPLETE,
    COMPLETE_WITH_EXCEPTIONS, FAILED). `extra` traz detalhes específicos.
    Também grava o JSON no resumo do job quando executado no GitHub Actions.
    """
    payload = {"status": status, "request_id": os.environ.get("REQUEST_ID", ""), **extra}
    print("BKP_RESULT " + json.dumps(payload, sort_keys=True), flush=True)
    # GITHUB_STEP_SUMMARY só existe dentro do Actions; localmente é ignorado.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("```json\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```\n")


def set_output(key, value):
    """Grava `key=value` em $GITHUB_OUTPUT para os passos seguintes do workflow lerem."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def parse_decision():
    """Lê PREFLIGHT_DECISION (JSON da decisão do HITL). Devolve None se vazio.

    JSON inválido encerra com saída 2: uma decisão ilegível nunca é ignorada.
    """
    raw = os.environ.get("PREFLIGHT_DECISION", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"::error::preflight_decision is not valid JSON: {exc}")
        sys.exit(2)


def cmd_validate_inputs():
    """Revalida os inputs do workflow (o dispatcher já validou; aqui é a 2ª barreira).

    Monta uma linha sintética de commands.log com os valores recebidos e a valida
    contra commands-log-line.schema.yaml. Assim existe UMA só definição das
    regras (o schema), sem duplicar padrões de regex neste script.
    Falha com saída 2 e status REJECTED.
    """
    # Valores recebidos do workflow. Chegam por variável de ambiente (nunca interpolados no shell), o que evita injeção de comando.
    params = {
        "source_repository": os.environ.get("SOURCE_REPOSITORY", ""),
        "destination": os.environ.get("DESTINATION", ""),
    }
    # Decisão do HITL (só existe em linha de continuação). None = 1ª execução.
    decision = parse_decision()
    if decision is not None:
        params["preflight_decision"] = decision
    # A linha sintética usa GO porque este workflow só é chamado após autorização.
    line = {
        "request_id": os.environ.get("REQUEST_ID", ""),
        "mnemonic": "BKP_REPO",
        "ts": now(),
        "authorization": {"decision": "GO", "authorized_scope": "EXACT_COMMAND_REQUEST", "reusable": False},
        "params": params,
    }
    # O schema da linha do commands.log é a ÚNICA definição das regras de entrada: nada de regex duplicada aqui.
    schema = load("backup/schemas/commands-log-line.schema.yaml")
    # Coleta TODOS os erros de uma vez, em ordem estável, para o log mostrar tudo que está errado.
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(line), key=str)
    if errors:
        # Cada erro vira uma anotação ::error:: no log do job (visível na tela do run).
        for e in errors:
            print(f"::error::input validation: {e.message}")
        result("REJECTED", reason="INPUT_VALIDATION")
        sys.exit(2)
    # Autorização não é reutilizável (regra 7): a decisão de continuar deve
    # referenciar OUTRA requisição (a que terminou BLOCKED), nunca a atual.
    if decision and decision["previous_request_id"] == line["request_id"]:
        print("::error::preflight_decision.previous_request_id must differ from request_id")
        result("REJECTED", reason="AUTHORIZATION_REUSE")
        sys.exit(2)
    # Só chega aqui se a entrada passou em todas as verificações.
    print(f"Inputs valid for {params['source_repository']}")


def build_preflight():
    """Avalia cada classe de objeto do backup_repository. Devolve (assessments, gaps, pending).

    assessments: por classe, as rotas avaliadas e a capacidade efetiva de leitura.
    gaps:        lacunas MATERIAIS (só ARCHITECTURAL/EXECUTION/ACCESS), que exigem HITL.
    pending:     fatos que só o Source Inventory descobre (FACTUAL_INVENTORY_PENDING).
                 Ficam separados e NUNCA entram em `gaps` (regra 12 do protocolo).
    """
    caps = load("backup/capabilities.yaml")
    cap = next(c for c in caps["capabilities"] if c["id"] == "backup_repository")
    # O passo do workflow que fornece o token de leitura usa continue-on-error;
    # aqui o resultado dele decide se a leitura da origem está disponível.
    token_ok = os.environ.get("TOKEN_OUTCOME") == "success"
    # Classes que o executor JÁ lê e preserva, declaradas no registro (`implemented_classes`).
    # Toda outra classe de `includes` vira EXECUTION_CAPABILITY_GAP até existir uma rota
    # implementada; nunca é tratada como ausente nem como preservada (regra 9 do protocolo).
    # Para implementar uma nova classe: implemente a leitura no workflow E acrescente-a
    # a `implemented_classes` no capabilities.yaml.
    implemented_classes = set(cap.get("implemented_classes", []))
    # Classes que precisam de uma credencial PRÓPRIA além do token de leitura da origem
    # (hoje: projects, com o PROJECTS_READ_TOKEN). `class_credentials` no registro dá o nome do
    # secret e a variável com o resultado do passo que o fornece (ex.: PROJECTS_TOKEN_OUTCOME).
    class_credentials = cap.get("class_credentials", {})
    assessments, gaps, pending = [], [], []
    # Uma avaliação por classe de objeto que o backup_repository cobre (lista `includes` do capabilities.yaml).
    for cls in cap["includes"]:
        # spec = definição da classe: leituras exigidas e rotas aprovadas.
        spec = caps["object_classes"][cls]
        # True só para classes com rota realmente implementada neste executor.
        implemented = cls in implemented_classes
        # Credencial própria da classe (se houver): sem ela a classe não é lida, mesmo implementada.
        cred = class_credentials.get(cls)
        cred_ok = (not cred) or os.environ.get(cred["outcome_env"]) == "success"
        cred_msg = f"Class credential ({cred['secret']}) is missing or could not be provided." if cred else ""
        # Rotas avaliadas desta classe; cada tipo de rota aprovado é avaliado separadamente.
        routes = []
        # Cada tipo de rota aprovado é avaliado separadamente (BACKUP_REPOSITORY.md).
        for rt in spec["approved_route_types"]:
            if rt == "DETERMINISTIC_EXECUTOR":
                # Este workflow É o executor determinístico; é a única rota avaliada de fato.
                if implemented and token_ok and cred_ok:
                    state, lim = "AVAILABLE", None
                elif implemented and not token_ok:
                    state, lim = "UNAVAILABLE", "Read-only source token could not be issued for the source repository."
                elif implemented:
                    state, lim = "UNAVAILABLE", cred_msg
                else:
                    state, lim = "UNAVAILABLE", "This executor has no implemented route for the object class."
                routes.append({"route_type": rt, "route_reference": "github_actions:backup-repository.yml",
                               "state": state, "authorized": True, "limitation": lim})
            else:
                # Demais rotas (conector, REST direto, etc.) não estão no escopo desta
                # execução: ficam NOT_VERIFIED e não autorizadas. Não afirmamos que
                # não existem, apenas que não foram avaliadas.
                routes.append({"route_type": rt, "route_reference": None, "state": "NOT_VERIFIED",
                               "authorized": False, "limitation": "Route not assessed; only the deterministic executor is in scope of this execution."})
        # Capacidade efetiva: só é AVAILABLE se a classe está implementada E o token de leitura foi emitido.
        effective = "AVAILABLE" if implemented and token_ok and cred_ok else "UNAVAILABLE"
        assessments.append({
            "object_class": cls,
            "architectural_status": spec["architectural_status"],
            # other_discovered_state (O-24) usa `required_behavior` em vez de `required_read_capabilities`.
            "required_read_capabilities": spec.get("required_read_capabilities") or spec.get("required_behavior"),
            "routes": routes,
            "effective_read_capability": effective,
        })
        # Sem leitura efetiva, a classe vira lacuna MATERIAL declarada; nunca é tratada como ausente nem como preservada.
        if effective != "AVAILABLE":
            # Classe implementada sem token = problema de ACESSO.
            # Classe não implementada = falta de CAPACIDADE DE EXECUÇÃO.
            cls_gap = GAP_ACCESS if implemented else GAP_EXEC
            cause = (("Read-only source token (SOURCE_READ_TOKEN) is missing or could not be provided for the source repository."
                      if not token_ok else cred_msg)
                     if implemented else "Executor route for this object class is not implemented yet.")
            gaps.append({
                "object_class": cls,
                "gap_classification": cls_gap,
                "limitation": f"{cls} cannot be read or discovered by this execution.",
                "required_capability": ", ".join(spec.get("required_read_capabilities") or spec.get("required_behavior")),
                "routes_assessed": [r["route_type"] for r in routes],
                "cause": cause,
                "preservation_impact": f"{cls} is not preserved; its state is NOT-VERIFIED, never treated as absent.",
                "resulting_restriction": f"{cls} excluded from this preservation and reported NOT-VERIFIED.",
            })
        if cls == "git":
            # Se a origem usa LFS ou submódulos só se descobre no Source Inventory.
            # A rota existe (route_defined), portanto NÃO é uma lacuna.
            pending.append({"object_class": "git", "pending_fact": "Git LFS usage and submodule presence in the source", "route_defined": True})
    return assessments, gaps, pending


def restriction_id(gap):
    """ID estável da restrição correspondente a uma lacuna (ex.: RST-issues-EXECUTION_CAPABILITY_GAP).

    Determinístico de propósito: o HITL aceita restrições por ID na linha de
    continuação do commands.log, e o mesmo ID acompanha evidência e manifest.
    """
    return f"RST-{gap['object_class']}-{gap['gap_classification']}"


def cmd_preflight():
    """Capability Preflight (roda APÓS o GO e ANTES do Source Inventory).

    Resultados possíveis:
      - sem lacunas             -> PASS, segue (saída 0)
      - lacunas + decisão OK    -> CONTINUE_WITH_RESTRICTIONS, segue (saída 0)
      - lacunas sem decisão     -> BLOCKED, PENDING (saída 10): o HITL precisa
                                   decidir STOP ou CONTINUE_WITH_RESTRICTIONS
    A decisão só vale se aceitar TODAS as restrições exigidas; aceitar menos que
    o necessário mantém o BLOCKED. Depois de gravar preflight.json, o resultado é
    validado contra o schema do command-request.
    """
    assessments, gaps, pending = build_preflight()
    pf = {"required": True, "status": "PASS", "assessments": assessments, "gaps": gaps,
          "factual_inventory_pending": pending, "hitl_decision": "NOT_REQUIRED", "accepted_restrictions": []}
    # Código de saída do preflight: 0 = segue; 10 = BLOCKED (falta decisão do HITL).
    exit_code = 0
    # Com lacunas materiais, o preflight não passa sozinho: precisa da decisão do HITL.
    if gaps:
        pf["status"] = "GAPS_IDENTIFIED"
        pf["hitl_decision"] = "PENDING"
        # Restrições que o HITL PRECISA aceitar, indexadas por restriction_id.
        needed = {restriction_id(g): g for g in gaps}
        decision = parse_decision()
        # Restrições que o HITL de fato aceitou na linha de continuação (vazio se não houve decisão).
        accepted = {a["restriction_id"]: a for a in (decision or {}).get("accepted_restrictions", [])}
        # `<=` entre conjuntos: todo ID exigido precisa constar entre os aceitos.
        if decision and set(needed) <= set(accepted):
            pf["hitl_decision"] = "CONTINUE_WITH_RESTRICTIONS"
            # As restrições registradas vêm das lacunas reais (não do texto que o
            # HITL enviou), para o registro refletir o que de fato foi avaliado.
            pf["accepted_restrictions"] = [
                {"restriction_id": rid, "object_class": g["object_class"],
                 "restriction": g["resulting_restriction"], "source_gap_classification": g["gap_classification"]}
                for rid, g in needed.items()
            ]
        else:
            exit_code = 10
    # Valida o resultado contra o sub-schema `capability_preflight` do command-request.
    schema = load("backup/schemas/command-request.schema.yaml")["properties"]["capability_preflight"]
    jsonschema.validate(pf, schema)
    # preflight.json é gravado mesmo no BLOCKED: o workflow o publica como artefato (rastro da decisão pendente).
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "preflight.json").write_text(json.dumps(pf, indent=2, sort_keys=True) + "\n")
    # Lista pronta para o HITL copiar na linha de continuação do commands.log.
    required = [{"restriction_id": restriction_id(g), "object_class": g["object_class"],
                 "restriction": g["resulting_restriction"], "source_gap_classification": g["gap_classification"]} for g in gaps]
    # BLOCKED: emite BKP_RESULT com a lista de restrições para o engine apresentar ao HITL.
    if exit_code:
        result("BLOCKED", reason="CAPABILITY_PREFLIGHT_GAPS", hitl_decision="PENDING",
               required_restrictions=required)
        print("::error::Material Capability Preflight gaps require HITL: STOP or CONTINUE_WITH_RESTRICTIONS.")
    else:
        result("PREFLIGHT_OK", preflight_status=pf["status"], hitl_decision=pf["hitl_decision"],
               accepted_restrictions=[a["restriction_id"] for a in pf["accepted_restrictions"]])
    sys.exit(exit_code)


def cmd_validate_destination():
    """Gate de destino: falha FECHADA se o destino não puder ser validado.

    Regra de fail-closed do protocolo ("destination cannot be validated"). Valida o OneDrive
    de verdade, pela biblioteca onedrive.py: renova o token, grava o novo refresh token no
    secret (rotação) e faz uma escrita, uma leitura e uma remoção de teste em
    <AppFolder>/<destination>/<request_id>/. Qualquer falha, inclusive configuração ausente,
    encerra com saída 20 e nada da origem é lido.
    """
    import onedrive                      # mesma pasta deste script

    prefix = f"{os.environ.get('DESTINATION', '').strip('/')}/{os.environ.get('REQUEST_ID', '')}"
    try:
        onedrive.Session.from_env().validate_destination(prefix)
    except onedrive.OneDriveError as exc:
        # A mensagem já é segura: nomes de variáveis e códigos de erro, nunca valores de token.
        result("BLOCKED", reason="DESTINATION_NOT_VALIDATED", detail=f"{exc.code}: {exc.message}")
        print("::error::Destination cannot be validated; failing closed before Source Inventory.")
        sys.exit(20)
    set_output("destination_validated", "true")
    result("DESTINATION_OK", destination=prefix)


def lines(name):
    """Linhas não vazias de um arquivo de evidência; lista vazia se o arquivo não existe."""
    p = EVIDENCE / name
    return [x for x in p.read_text(errors="replace").splitlines() if x] if p.exists() else []


def sha256(path):
    """SHA-256 (hex) de um arquivo, lido em blocos de 1 MiB para não carregar tudo na memória."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_build_evidence():
    """Monta evidence.json e manifest.json (schemas 2.0) e valida ambos.

    Só roda se o destino foi validado (DESTINATION_VALIDATED=true); caso
    contrário sai com 21. Regra: nenhuma evidência é produzida sem destino
    validado, pois o schema exige `destination_validated: true`.

    Disposição por classe:
      git            PRESERVED; PARTIALLY-PRESERVED se há submódulos (só os gitlinks
                     são registrados); FAILED se nenhuma ref foi enumerada ou se nenhum
                     pacote foi enviado ao OneDrive (evidence/onedrive-package.json).
      labels, milestones, issues, projects
                     lidas pela API (evidence/api-status.json e api-projects-status.json):
                     PRESERVED-AS-EQUIVALENT-REPRESENTATION se a leitura passou nas
                     conferências; PARTIALLY-PRESERVED se parte do conteúdo ficou oculta para
                     o token (projects com itens ocultos); PRESERVED (sem objetos) se o recurso
                     está desativado na origem; FAILED se a leitura falhou ou alguma conferência
                     reprovou. Classe com restrição aceita (ex.: projects sem token) é NOT-VERIFIED.
      demais classes NOT-VERIFIED, ligadas ao restriction_id aceito pelo HITL.
    Status final: COMPLETE só sem restrições, limitações ou falhas; senão
    COMPLETE_WITH_EXCEPTIONS (ou FAILED).
    """
    if os.environ.get("DESTINATION_VALIDATED") != "true":
        print("::error::Refusing to build evidence: destination was not validated.")
        sys.exit(21)
    # Daqui em diante o destino JÁ foi validado (checado acima); é seguro montar a evidência.
    request_id = os.environ["REQUEST_ID"]
    source = os.environ["SOURCE_REPOSITORY"]
    # Resultado do preflight desta mesma execução, gravado antes pelo subcomando `preflight`.
    pf = json.loads((EVIDENCE / "preflight.json").read_text())
    # Restrições aceitas indexadas por classe de objeto.
    accepted = {a["object_class"]: a for a in pf["accepted_restrictions"]}
    started = os.environ.get("STARTED_AT", now())

    # Lista de objetos da evidência: primeiro o git, depois as classes de API (labels, milestones, issues) e,
    # por fim, as classes ainda NOT-VERIFIED.
    objects = []
    # --- Classe git: disposição decidida pelos arquivos que os passos bash gravaram ---
    git_disp, git_lim = "PRESERVED", None
    if lines("gitlinks-history.tsv"):
        # Há submódulos no histórico: registramos os gitlinks, não o conteúdo deles.
        git_disp = "PARTIALLY-PRESERVED"
        git_lim = "Submodule gitlinks recorded; submodule contents are not preserved by this executor."
    if not lines("refs.tsv"):
        # Nenhuma ref enumerada: o mirror não pode ser considerado preservado.
        git_disp, git_lim = "FAILED", "No refs enumerated from the mirror."
    # Pacote restaurável já enviado ao OneDrive (onedrive.py upload grava esta lista).
    package_file = EVIDENCE / "onedrive-package.json"
    package = json.loads(package_file.read_text()) if package_file.exists() else []
    if git_disp != "FAILED" and not package:
        # Sem pacote no destino não há preservação: o mirror local some com o runner.
        git_disp, git_lim = "FAILED", "No preservation package was uploaded to the destination."
    objects.append({"object_class": "git", "object_id": source, "disposition": git_disp,
                    "evidence": ["refs.tsv", "refs-compare.txt", "git-fsck.txt", "git-count-objects.txt", "lfs-files.txt",
                                 "lfs-verification.tsv", "gitmodules-history.tsv", "gitlinks-history.tsv",
                                 "onedrive-package.json"],
                    "limitation": git_lim})
    # Limitações do manifest, cada uma ligada à sua restrição aceita (rastreabilidade).
    limitations = []
    if git_lim:
        limitations.append({"limitation_id": "LIM-git-submodules", "description": git_lim,
                            "restriction_id": None, "object_class": "git"})
    # --- Classes lidas pela API (labels, milestones, issues) ---
    # Só entram aqui as que estão em `implemented_classes` do registro e que este leitor cobre.
    # A leitura nunca vira sucesso por omissão: sem api-status.json a classe é FAILED.
    caps = load("backup/capabilities.yaml")
    implemented = set(next(c for c in caps["capabilities"] if c["id"] == "backup_repository").get("implemented_classes", []))
    # Uma classe implementada, mas com restrição ACEITA nesta execução (ex.: projects sem o
    # PROJECTS_READ_TOKEN), não foi lida: fica NOT-VERIFIED no laço abaixo.
    api_classes = [c for c in API_CLASSES if c in implemented and c not in accepted]
    # Resultado de cada leitor: api-status.json (labels, milestones, issues) e
    # api-projects-status.json (projects). Os dois têm o formato {"classes": {classe: {...}}}.
    api_status = {}
    for status_name in ("api-status.json", "api-projects-status.json"):
        status_file = EVIDENCE / status_name
        if status_file.exists():
            api_status.update(json.loads(status_file.read_text())["classes"])
    for cls in api_classes:
        st = api_status.get(cls)
        api_files = API_EVIDENCE[cls] + [f["file"] for f in (st or {}).get("files", [])]
        if st is None:
            disp, lim = "FAILED", "The API read step did not produce a result for this class."
        elif st["status"] == "OK":
            disp, lim = "PRESERVED-AS-EQUIVALENT-REPRESENTATION", API_LIMITATION[cls]
        elif st["status"] == "PARTIAL":
            # Leu, mas parte do conteúdo ficou oculta para o token: lacuna declarada, nunca PRESERVED.
            disp, lim = "PARTIALLY-PRESERVED", f"{API_LIMITATION[cls]} Partial: {st.get('detail', 'some content is hidden from the token.')}"
        elif st["status"] == "DISABLED":
            disp, lim = "PRESERVED", None
        else:
            disp, lim = "FAILED", st.get("detail", "The API read failed.")
        objects.append({"object_class": cls, "object_id": f"{source}#{cls}", "disposition": disp,
                        "evidence": api_files, "limitation": lim})
        if lim and disp != "FAILED":
            limitations.append({"limitation_id": f"LIM-{cls}-{'partial' if disp == 'PARTIALLY-PRESERVED' else 'equivalent'}",
                                "description": lim, "restriction_id": None, "object_class": cls})
    # --- Demais classes: NOT-VERIFIED, rastreáveis até a restrição aceita ---
    for cls, a in accepted.items():
        if cls == "git" or cls in api_classes:
            continue
        objects.append({"object_class": cls, "object_id": f"{source}#{cls}", "disposition": "NOT-VERIFIED",
                        "evidence": ["preflight.json"], "limitation": a["restriction"],
                        "restriction_ids": [a["restriction_id"]]})
        limitations.append({"limitation_id": f"LIM-{cls}", "description": a["restriction"],
                            "restriction_id": a["restriction_id"], "object_class": cls})

    # Versão do preflight para a evidência (o schema da evidência tem menos campos que o do request).
    pf_ev = {"status": pf["status"],
             "gaps": [{k: g[k] for k in ("object_class", "gap_classification", "limitation", "preservation_impact", "resulting_restriction")} for g in pf["gaps"]],
             "factual_inventory_pending": [{"object_class": p["object_class"], "pending_fact": p["pending_fact"]} for p in pf["factual_inventory_pending"]],
             "hitl_decision": pf["hitl_decision"], "accepted_restrictions": pf["accepted_restrictions"]}
    # Corpo do evidence.json (schema 2.0).
    evidence = {
        "schema_version": "2.0", "request_id": request_id, "capability_id": "backup_repository",
        "source_scope": {"source_repository": source, "destination": os.environ["DESTINATION"]},
        "captured_at": now(), "capability_preflight": pf_ev, "objects": objects,
        "execution": {"source_repository_mode": "READ_ONLY", "destination_validated": True,
                      "authorization_request_id": request_id, "restrictions_reconciled": True,
                      # MRBI/CRR ficam nulos: este executor ainda não lê classes temporais
                      # (nunca inferir CRR a partir de valor padrão do produto).
                      "temporal_preservation": {"mrbi": None, "crr": None,
                                                "notes": ["Temporal classes are not read by this executor version."]}},
    }
    # Valida ANTES de gravar: evidência fora do schema derruba a etapa (não existe 'sucesso' sem evidência válida).
    jsonschema.validate(evidence, load("backup/schemas/evidence.schema.yaml"))
    (EVIDENCE / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")

    # Quantos objetos têm cada disposição (base da reconciliação do manifest).
    count = lambda d: sum(o["disposition"] == d for o in objects)
    # O manifest lista, primeiro, os arquivos do pacote enviados ao OneDrive (caminho no destino,
    # SHA-256 e tamanho) e, depois, cada arquivo de evidência com SHA-256 (exceto o próprio manifest).
    arts = [{"path": f["remote_path"], "sha256": f["sha256"], "object_class": "preservation-package", "bytes": f["bytes"]}
            for f in package]
    arts += [{"path": p.name, "sha256": sha256(p), "object_class": "evidence", "bytes": p.stat().st_size}
             for p in sorted(EVIDENCE.iterdir()) if p.is_file() and p.name != "manifest.json"]
    # Há exceções se existirem limitações, restrições aceitas ou objetos FAILED.
    exceptions = bool(limitations or pf["accepted_restrictions"] or count("FAILED"))
    # FAILED se algum objeto falhou; COMPLETE_WITH_EXCEPTIONS se há exceções aceitas; COMPLETE só sem nenhuma.
    status = "FAILED" if count("FAILED") else ("COMPLETE_WITH_EXCEPTIONS" if exceptions else "COMPLETE")
    # Corpo do manifest.json (schema 2.0): reconciliação legível por máquina.
    manifest = {
        "schema_version": "2.0", "request_id": request_id, "capability_id": "backup_repository",
        "scope": evidence["source_scope"], "started_at": started, "completed_at": now(), "status": status,
        "capability_preflight": {"status": pf["status"], "hitl_decision": pf["hitl_decision"],
                                 "accepted_restrictions": pf["accepted_restrictions"]},
        "artifacts": arts,
        "reconciliation": {"preserved": count("PRESERVED"), "equivalent": count("PRESERVED-AS-EQUIVALENT-REPRESENTATION"),
                           "partial": count("PARTIALLY-PRESERVED"), "non_exportable": count("NON-EXPORTABLE"),
                           "failed": count("FAILED"), "not_verified": count("NOT-VERIFIED"),
                           "unreconciled_object_classes": [], "restrictions_reconciled": True},
        "limitations": limitations,
    }
    # Também validado antes de gravar.
    jsonschema.validate(manifest, load("backup/schemas/backup-manifest.schema.yaml"))
    (EVIDENCE / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    result(status, evidence_sha256=sha256(EVIDENCE / "evidence.json"),
           manifest_sha256=sha256(EVIDENCE / "manifest.json"), reconciliation=manifest["reconciliation"],
           package_files=[f["remote_path"] for f in package])
    # Conclusão do workflow nunca prova preservação: FAILED derruba o job.
    if status == "FAILED":
        sys.exit(1)


# Mapa subcomando -> função. Para adicionar um subcomando: crie a função e registre-a aqui.
COMMANDS = {"validate-inputs": cmd_validate_inputs, "preflight": cmd_preflight,
            "validate-destination": cmd_validate_destination, "build-evidence": cmd_build_evidence}

if __name__ == "__main__":
    # Uso incorreto (sem argumento, ou subcomando desconhecido): mostra a ajuda e sai com 64.
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(64)
    COMMANDS[sys.argv[1]]()
