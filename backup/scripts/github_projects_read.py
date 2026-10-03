#!/usr/bin/env python3
"""Leitor da classe `projects` do BKP_REPO: GitHub Projects v2 (somente leitura).

O QUE É
    Lê pela API GraphQL do GitHub o estado dos Projects (v2) e o grava como JSON para entrar
    no pacote do backup (package/api-projects*.json). Usa só a biblioteca padrão do Python.
    É UM leitor para dois caminhos: o backup completo (`BKP_REPO`, escopo SOURCE_REPOSITORY)
    e, mais adiante, o backup só de Projects (`backup_projects`, qualquer um dos 3 escopos).

ONDE SE ENCAIXA NO FLUXO
    Roda no workflow `backup-repository.yml`, depois do passo que lê labels, milestones e
    issues e ANTES do passo "Build restorable package". Quem decide a disposição final da
    classe (PRESERVED-AS-EQUIVALENT-REPRESENTATION, PARTIALLY-PRESERVED ou FAILED) é
    `bkp_repo.py build-evidence`, a partir do `api-projects-status.json` gravado aqui.

POR QUE UM TOKEN PRÓPRIO (PROJECTS_READ_TOKEN)
    Token fine-grained não alcança Projects v2 de CONTA PESSOAL (fontes consultadas: discussões
    e issues no GitHub, não a documentação oficial; confirmar no primeiro run real). A saída é um
    token CLÁSSICO com o escopo `read:project`, somente leitura. Esse token NÃO enxerga
    repositórios privados (isso exigiria o escopo `repo`, que dá escrita): ver "DESCOBERTA" e
    "LIMITES".

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - A origem é somente leitura: só consultas GraphQL (`query`). Uma consulta que contenha
      `mutation` é recusada ANTES de ser enviada (função `run_query`).
    - Dado de outro repositório não entra no pacote: Project candidato não confirmado é excluído.
    - Leitura incompleta nunca vira sucesso: cada projeto é conferido (total de itens,
      conexões truncadas, opções referenciadas). Divergência = FAILED; referência que o token
      não consegue ver = PARTIAL (nunca PRESERVED).
    - Erro de permissão ou de escopo nunca é "sem Projects": é FAILED com a causa.
    - Uma exceção inesperada NUNCA derruba o job (o backup do código segue): vira FAILED com a causa.
    - O token nunca é impresso nem gravado. Mensagens de erro mostram só o status e a rota.

ESCOPOS (variável PROJECTS_SCOPE; identificadores em PROJECTS_SCOPE_IDS, um JSON)
    SOURCE_REPOSITORY   {"source_repository": "dono/nome"}   Projects ligados ao repositório.
    PROJECT             {"owner": "dono", "number": 13}      Um Project.
    OWNER_PROJECT_SET   {"owner": "dono"}                    Todos os Projects do dono.

DESCOBERTA (SOURCE_REPOSITORY)
    1. `repository(...).projectsV2`: o caminho direto. Falha se o token não enxerga o repositório
       (repositório privado e token sem `repo`).
    2. Alternativa: lista os Projects do dono do repositório. Os que ligam a origem de forma
       VISÍVEL (`repositories`) entram. O GraphQL devolve `null` no lugar de um repositório ligado
       que o token não vê, então os que têm algum vínculo OCULTO são só CANDIDATOS: o token sabe
       QUANTOS repositórios estão ligados, mas não QUAIS.
    3. Confirmação dos candidatos pelos TÍTULOS (regra E): lê os itens do candidato e o compara com
       as issues da origem, que o passo anterior do workflow já gravou em package/api-issues.json
       (a leitura REST enxerga o repositório privado). O candidato fica se pelo menos 1 título de
       item de issue bate e se bate pelo menos metade deles; senão é EXCLUÍDO e NÃO entra no
       pacote: o inventário guarda só número, título do Project e contagens
       (extra.excluded_candidates). É uma heurística, não uma prova: título repetido entre
       repositórios pode dar falso positivo, e uma origem sem issues (ou um Project só com
       rascunhos) não confirma nada. Para um Project específico, use o escopo PROJECT.
    Todo esse caminho é PARTIAL, e "sem Projects ligados" não é uma prova.

VARIÁVEIS DE AMBIENTE
    PROJECTS_TOKEN        token clássico com `read:project` (o secret PROJECTS_READ_TOKEN).
    PROJECTS_SCOPE        um dos três escopos acima.
    PROJECTS_SCOPE_IDS    JSON com os identificadores do escopo.
    EVIDENCE_DIR          pasta de evidências (padrão: evidence). Recebe api-projects-status.json.
    PACKAGE_DIR           pasta do pacote (padrão: package). Recebe api-projects*.json.
    SOURCE_TITLES_DIR     pasta com o api-issues.json da origem (padrão: PACKAGE_DIR). No backup só de
                          Projects (BKP_PROJ) fica fora do pacote: as issues não fazem parte dele.
    GITHUB_API_URL        base da API (padrão: https://api.github.com); o GraphQL fica em
                          <base>/graphql. Existe para o teste local usar um servidor simulado.

ARQUIVOS GERADOS
    package/api-projects.json             cada Project: metadados, campos e opções, views,
                                          workflows, status updates, repositórios ligados e
                                          `linkage` (como o vínculo com a origem foi estabelecido)
    package/api-project-items.json        itens de todos os Projects (valores de campo,
                                          arquivados, referências a issues e PRs, rascunhos)
    package/api-projects-inventory.json   contagens, SHA-256 dos arquivos, conferências, limites
    evidence/api-projects-status.json     resultado da classe (lido pelo build-evidence)

CÓDIGOS DE SAÍDA
    0   terminou (a classe tem OK, PARTIAL ou FAILED no api-projects-status.json)
    64  uso incorreto, variável ausente ou escopo inválido
"""
import datetime
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

# Itens por página nas listagens (o GraphQL do GitHub aceita até 100 por conexão).
PAGE = 100
# Teto de páginas por listagem: evita laço infinito se a API devolver sempre "próxima página".
MAX_PAGES = 500
# Tentativas por requisição em falha transitória (rede, 5xx, limite de taxa).
MAX_TRIES = 5
# Espera máxima, em segundos, por um limite de taxa. Acima disso a leitura falha.
MAX_RATE_WAIT = 120

# Função de espera. É uma variável para o teste poder trocá-la e não esperar de verdade.
sleep = time.sleep

SCOPES = ("SOURCE_REPOSITORY", "PROJECT", "OWNER_PROJECT_SET")

# Limites permanentes desta versão do leitor, copiados para o inventário.
LIMITATIONS = [
    "Projects are preserved as an equivalent representation (GraphQL JSON); recreating a board "
    "through the API is the future restore capability, not part of this reader.",
    "Item content is read for issues and pull requests the token can see; a private repository "
    "needs the `repo` scope. Items whose content is hidden are counted as redacted and make the "
    "class PARTIAL. Issue and pull request text is preserved by their own object classes.",
    "Nested lists inside a single value (labels, users, pull requests of one field value) are read up to 20 entries.",
]

# Consultas. Cada uma tem nome (operationName) e só LÊ. Os trechos repetidos ficam em constantes.
_PROJECT_REF = "id number title"
_PAGEINFO = "pageInfo { hasNextPage endCursor }"
_FIELD_REF = "field { ... on ProjectV2FieldCommon { id name } }"

Q_OF_REPOSITORY = f"""query ProjectsOfRepository($owner: String!, $name: String!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    nameWithOwner
    projectsV2(first: {PAGE}, after: $after) {{ totalCount {_PAGEINFO} nodes {{ {_PROJECT_REF} }} }}
  }}
}}"""

_OWNER_PROJECTS = f"projectsV2(first: {PAGE}, after: $after) {{ totalCount {_PAGEINFO} nodes {{ {_PROJECT_REF} repositories(first: {PAGE}) {{ {_PAGEINFO} nodes {{ nameWithOwner }} }} }} }}"
Q_OF_OWNER = f"""query ProjectsOfOwner($owner: String!, $after: String) {{
  repositoryOwner(login: $owner) {{
    __typename login
    ... on User {{ {_OWNER_PROJECTS} }}
    ... on Organization {{ {_OWNER_PROJECTS} }}
  }}
}}"""

Q_BY_NUMBER = f"""query ProjectByNumber($owner: String!, $number: Int!) {{
  repositoryOwner(login: $owner) {{
    __typename login
    ... on User {{ projectV2(number: $number) {{ {_PROJECT_REF} }} }}
    ... on Organization {{ projectV2(number: $number) {{ {_PROJECT_REF} }} }}
  }}
}}"""

Q_DETAILS = f"""query ProjectDetails($id: ID!) {{
  node(id: $id) {{
    ... on ProjectV2 {{
      id number title shortDescription readme public closed closedAt createdAt updatedAt url
      owner {{ __typename ... on User {{ login }} ... on Organization {{ login }} }}
      creator {{ login }}
      repositories(first: {PAGE}) {{ totalCount {_PAGEINFO} nodes {{ nameWithOwner }} }}
      fields(first: {PAGE}) {{
        totalCount {_PAGEINFO}
        nodes {{
          __typename
          ... on ProjectV2FieldCommon {{ id name dataType createdAt updatedAt }}
          ... on ProjectV2SingleSelectField {{ options {{ id name color description }} }}
          ... on ProjectV2IterationField {{
            configuration {{
              duration startDay
              iterations {{ id title startDate duration }}
              completedIterations {{ id title startDate duration }}
            }}
          }}
        }}
      }}
      views(first: 50) {{ totalCount {_PAGEINFO} nodes {{ id name number layout filter createdAt updatedAt }} }}
      workflows(first: 50) {{ totalCount {_PAGEINFO} nodes {{ id name number enabled createdAt updatedAt }} }}
      statusUpdates(first: 50) {{ totalCount {_PAGEINFO} nodes {{ id body status startDate targetDate createdAt creator {{ login }} }} }}
    }}
  }}
}}"""

Q_ITEMS = f"""query ProjectItems($id: ID!, $after: String) {{
  node(id: $id) {{
    ... on ProjectV2 {{
      items(first: {PAGE}, after: $after) {{
        totalCount {_PAGEINFO}
        nodes {{
          id type isArchived createdAt updatedAt
          creator {{ login }}
          content {{
            __typename
            ... on Issue {{ id number title url state repository {{ nameWithOwner }} }}
            ... on PullRequest {{ id number title url state repository {{ nameWithOwner }} }}
            ... on DraftIssue {{ id title body createdAt updatedAt creator {{ login }} assignees(first: 20) {{ nodes {{ login }} }} }}
          }}
          fieldValues(first: 50) {{
            {_PAGEINFO}
            nodes {{
              __typename
              ... on ProjectV2ItemFieldTextValue {{ text {_FIELD_REF} }}
              ... on ProjectV2ItemFieldNumberValue {{ number {_FIELD_REF} }}
              ... on ProjectV2ItemFieldDateValue {{ date {_FIELD_REF} }}
              ... on ProjectV2ItemFieldSingleSelectValue {{ name optionId color description {_FIELD_REF} }}
              ... on ProjectV2ItemFieldIterationValue {{ title iterationId startDate duration {_FIELD_REF} }}
              ... on ProjectV2ItemFieldLabelValue {{ labels(first: 20) {{ nodes {{ name }} }} {_FIELD_REF} }}
              ... on ProjectV2ItemFieldUserValue {{ users(first: 20) {{ nodes {{ login }} }} {_FIELD_REF} }}
              ... on ProjectV2ItemFieldMilestoneValue {{ milestone {{ number title }} {_FIELD_REF} }}
              ... on ProjectV2ItemFieldRepositoryValue {{ repository {{ nameWithOwner }} {_FIELD_REF} }}
              ... on ProjectV2ItemFieldPullRequestValue {{ pullRequests(first: 20) {{ nodes {{ number url }} }} {_FIELD_REF} }}
            }}
          }}
        }}
      }}
    }}
  }}
}}"""


class GraphqlError(Exception):
    """Falha da API (HTTP ou lista `errors` do GraphQL) que não adianta repetir."""

    def __init__(self, kind, detail=""):
        self.kind = kind          # "HTTP 403", "NOT_FOUND", "INSUFFICIENT_SCOPES"...
        self.detail = detail
        super().__init__(f"{kind}" + (f": {detail}" if detail else ""))


def now():
    """Instante atual em UTC, formato ISO 8601 com Z."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Graph:
    """Cliente mínimo do GraphQL do GitHub: só consultas, com repetição em falha transitória."""

    def __init__(self, base, token):
        self.url = base.rstrip("/") + "/graphql"
        self.token = token

    def run_query(self, query, variables=None):
        """Envia UMA consulta e devolve `data`. Recusa qualquer coisa que não seja `query`."""
        if "mutation" in query.lower():
            raise GraphqlError("REFUSED", "mutation não é permitida neste leitor")
        name = query.split("(", 1)[0].split()[1] if query.lstrip().startswith("query") else ""
        body = json.dumps({"query": query, "variables": variables or {}, "operationName": name}).encode()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "repo-backup-bkp-repo",
        })
        for attempt in range(1, MAX_TRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429) and (exc.headers.get("Retry-After") or exc.headers.get("X-RateLimit-Remaining") == "0"):
                    wait = int(exc.headers.get("Retry-After") or 1)
                    if wait > MAX_RATE_WAIT or attempt == MAX_TRIES:
                        raise GraphqlError(f"HTTP {exc.code}", "limite de taxa da API esgotado") from None
                    sleep(wait)
                    continue
                if exc.code >= 500 and attempt < MAX_TRIES:
                    sleep(2 * attempt)
                    continue
                raise GraphqlError(f"HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == MAX_TRIES:
                    raise GraphqlError("NETWORK", f"falha de rede: {type(exc).__name__}") from None
                sleep(2 * attempt)
        # Qualquer erro GraphQL derruba a leitura: dado parcial nunca é aceito como completo.
        errors = payload.get("errors")
        if errors:
            first = errors[0]
            raise GraphqlError(first.get("type") or "GRAPHQL_ERROR", first.get("message", ""))
        return payload["data"]


def discover_by_repository(graph, source):
    """Projects ligados ao repositório pelo caminho direto. Devolve a lista de {id, number, title}."""
    owner, name = source.split("/", 1)
    found, after = [], None
    for _ in range(MAX_PAGES):
        data = graph.run_query(Q_OF_REPOSITORY, {"owner": owner, "name": name, "after": after})
        repo = data.get("repository")
        if repo is None:
            raise GraphqlError("NOT_FOUND", "repositório não visível para o token")
        conn = repo["projectsV2"]
        found += conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return found
        after = conn["pageInfo"]["endCursor"]
    raise GraphqlError("LIMIT", f"mais de {MAX_PAGES} páginas")


def list_owner_projects(graph, owner):
    """Todos os Projects do dono, com os repositórios ligados a cada um."""
    found, after = [], None
    for _ in range(MAX_PAGES):
        data = graph.run_query(Q_OF_OWNER, {"owner": owner, "after": after})
        who = data.get("repositoryOwner")
        if who is None:
            raise GraphqlError("NOT_FOUND", f"dono {owner} não encontrado")
        conn = who.get("projectsV2")
        if conn is None:
            raise GraphqlError("NOT_FOUND", f"{owner} não tem Projects v2 expostos ({who.get('__typename')})")
        found += conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return found
        after = conn["pageInfo"]["endCursor"]
    raise GraphqlError("LIMIT", f"mais de {MAX_PAGES} páginas")


def discover(graph, scope, ids):
    """Descobre os Projects do escopo. Devolve (lista de {id, number, title}, notas, parcial)."""
    notes, partial = [], False
    if scope == "SOURCE_REPOSITORY":
        source = ids["source_repository"]
        try:
            return discover_by_repository(graph, source), notes, partial
        except GraphqlError as exc:
            if exc.kind != "NOT_FOUND":
                raise
            # Caminho alternativo: o token não enxerga o repositório (privado, sem `repo`).
            # O GraphQL devolve `null` no lugar de um repositório ligado que o token não pode ver
            # (confirmado no run real req-20261003-001), então um vínculo oculto não dá para comparar
            # por nome. Entra o Project que liga a origem de forma VISÍVEL; o que tem algum vínculo
            # OCULTO é só CANDIDATO (`_candidate`) e cmd_read o confirma ou exclui pelos títulos
            # (regra E, ver confirm_candidate). O 1º run com candidatos incluídos às cegas trouxe 2
            # Projects alheios (108 itens).
            owner = source.split("/", 1)[0]
            matched, candidates = [], []
            for project in list_owner_projects(graph, owner):
                linked = ((project.get("repositories") or {}).get("nodes")) or []
                visible = [r for r in linked if r]
                if any(r["nameWithOwner"].lower() == source.lower() for r in visible):
                    matched.append(project)
                elif len(visible) < len(linked):
                    candidates.append(dict(project, _candidate=True))
            notes.append("repository not visible to the token: Projects discovered by listing the owner's Projects "
                         "and filtering by linked repository; a link to a private repository is hidden from the token, "
                         "so an empty result is not proof of absence.")
            return matched + candidates, notes, True
    if scope == "OWNER_PROJECT_SET":
        return list_owner_projects(graph, ids["owner"]), notes, partial
    # PROJECT
    data = graph.run_query(Q_BY_NUMBER, {"owner": ids["owner"], "number": int(ids["number"])})
    who = data.get("repositoryOwner")
    project = None if who is None else (who.get("projectV2"))
    if project is None:
        raise GraphqlError("NOT_FOUND", f"Project {ids['owner']}#{ids['number']} não encontrado")
    return [project], notes, partial


def normalize_title(text):
    """Título comparável: sem espaços sobrando e sem diferença de caixa."""
    return " ".join(str(text).split()).casefold()


def load_source_titles(package):
    """Títulos das issues da origem, lidos de package/api-issues.json (gravado pelo passo anterior).

    Devolve um conjunto (possivelmente vazio) ou None se o arquivo não existe ou não é legível:
    sem os títulos nenhum candidato pode ser confirmado."""
    path = package / "api-issues.json"
    try:
        return {normalize_title(i["title"]) for i in json.loads(path.read_text(encoding="utf-8")) if i.get("title")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def item_title(item, title_field_id):
    """Título de um item: do conteúdo, se visível, ou do valor do campo Title (que sobrevive à ocultação)."""
    content = item.get("content")
    if content and content.get("title"):
        return content["title"]
    for value in item["fieldValues"]["nodes"]:
        if value.get("__typename") == "ProjectV2ItemFieldTextValue" and (value.get("field") or {}).get("id") == title_field_id:
            return value.get("text")
    return None


def confirm_candidate(details, items, source, titles):
    """Regra E: um Project com vínculo oculto está ligado à origem? Devolve {linked, matched, issue_items, reason}.

    Conta os itens de issue: um item bate se o conteúdo visível é da origem ou se o título bate com uma
    issue da origem. Fica se bateu pelo menos 1 e pelo menos metade (heurística: ver o cabeçalho)."""
    title_field = next((f["id"] for f in details["fields"]["nodes"] if f.get("dataType") == "TITLE"), None)
    issue_items = [i for i in items if i["type"] == "ISSUE"]
    matched = 0
    for item in issue_items:
        content = item.get("content") or {}
        if ((content.get("repository") or {}).get("nameWithOwner") or "").lower() == source.lower():
            matched += 1
        elif titles and normalize_title(item_title(item, title_field) or "") in titles:
            matched += 1
    linked = matched >= 1 and matched * 2 >= len(issue_items)
    if linked:
        reason = None
    elif not issue_items:
        reason = "NO_ISSUE_ITEMS"
    elif titles is None or (not titles and not matched):
        reason = "NO_SOURCE_TITLES"
    else:
        reason = "TITLES_DO_NOT_MATCH"
    return {"linked": linked, "matched": matched, "issue_items": len(issue_items), "reason": reason}


def read_items(graph, project_id):
    """Todos os itens de um Project, paginados. Devolve (itens, totalCount informado)."""
    items, after, total = [], None, None
    for _ in range(MAX_PAGES):
        data = graph.run_query(Q_ITEMS, {"id": project_id, "after": after})
        conn = data["node"]["items"]
        total = conn["totalCount"]
        items += conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return items, total
        after = conn["pageInfo"]["endCursor"]
    raise GraphqlError("LIMIT", f"mais de {MAX_PAGES} páginas")


def check_project(details, items, total):
    """Conferências de UM Project. Devolve (checks, quantidade de itens ocultos)."""
    checks = []
    # 1. Total de itens lido contra o totalCount que a API informa.
    checks.append({"check": "items_total", "expected": total, "read": len(items), "ok": total == len(items)})
    # 2. Nenhuma conexão cortada: o que a API ainda tinha para entregar e não foi lido.
    truncated = [n for n in ("repositories", "fields", "views", "workflows", "statusUpdates")
                 if (details.get(n) or {}).get("pageInfo", {}).get("hasNextPage")]
    truncated += ["item.fieldValues" for i in items if i["fieldValues"]["pageInfo"]["hasNextPage"]][:1]
    checks.append({"check": "connections_complete", "ok": not truncated, "truncated": truncated})
    # 3. Todo valor de campo de seleção cita uma opção que existe no campo.
    options = {f["id"]: {o["id"] for o in f.get("options", [])} for f in details["fields"]["nodes"] if "options" in f}
    bad = sorted({(v["field"]["name"], v["optionId"]) for i in items for v in i["fieldValues"]["nodes"]
                  if v.get("__typename") == "ProjectV2ItemFieldSingleSelectValue" and v.get("field")
                  and v["field"]["id"] in options and v["optionId"] not in options[v["field"]["id"]]})
    checks.append({"check": "select_options_exist", "ok": not bad, "unknown": [list(b) for b in bad]})
    # 4. Itens ocultos: o token não vê o conteúdo (repositório privado sem `repo`). Não é falha de
    #    leitura, mas é lacuna de preservação: torna a classe PARTIAL (informativo, `ok` = True).
    redacted = sum(1 for i in items if i["type"] == "REDACTED" or (i["type"] in ("ISSUE", "PULL_REQUEST") and not i.get("content")))
    checks.append({"check": "redacted_items", "ok": True, "redacted": redacted})
    return checks, redacted


def write_json(path, data):
    """Grava JSON legível e determinístico; devolve o conteúdo em bytes (para o SHA-256)."""
    raw = (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


def parse_env():
    """Lê e valida as variáveis de ambiente. Sai com 64 se algo faltar ou for inválido."""
    try:
        token, scope, raw = os.environ["PROJECTS_TOKEN"], os.environ["PROJECTS_SCOPE"], os.environ["PROJECTS_SCOPE_IDS"]
    except KeyError as exc:
        print(f"::error::Variável ausente: {exc.args[0]}")
        sys.exit(64)
    try:
        ids = json.loads(raw)
    except ValueError:
        print("::error::PROJECTS_SCOPE_IDS não é um JSON válido.")
        sys.exit(64)
    needed = {"SOURCE_REPOSITORY": ("source_repository",), "PROJECT": ("owner", "number"), "OWNER_PROJECT_SET": ("owner",)}
    if scope not in SCOPES or not isinstance(ids, dict) or any(k not in ids for k in needed[scope]):
        print(f"::error::Escopo inválido ou identificadores faltando para {scope}.")
        sys.exit(64)
    if scope == "SOURCE_REPOSITORY" and "/" not in str(ids["source_repository"]):
        print("::error::source_repository deve ter o formato dono/nome.")
        sys.exit(64)
    return token, scope, ids


def cmd_read():
    """Lê os Projects do escopo e grava os arquivos do pacote e o api-projects-status.json."""
    token, scope, ids = parse_env()
    evidence = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))
    package = pathlib.Path(os.environ.get("PACKAGE_DIR", "package"))
    evidence.mkdir(parents=True, exist_ok=True)
    package.mkdir(parents=True, exist_ok=True)
    graph = Graph(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token)

    entry = {"status": "FAILED", "count": 0, "files": [], "checks": []}
    projects, all_items, notes, partial, excluded = [], [], [], False, []
    # Títulos das issues da origem (só para confirmar candidatos do escopo SOURCE_REPOSITORY).
    # SOURCE_TITLES_DIR: pasta onde está o api-issues.json (padrão: o próprio pacote). No backup só de Projects
    # (BKP_PROJ) as issues são lidas numa pasta de trabalho, fora do pacote, só para esta conferência.
    titles = load_source_titles(pathlib.Path(os.environ.get("SOURCE_TITLES_DIR", package))) if scope == "SOURCE_REPOSITORY" else None
    try:
        found, notes, partial = discover(graph, scope, ids)
        for ref in found:
            details = graph.run_query(Q_DETAILS, {"id": ref["id"]})["node"]
            items, total = read_items(graph, ref["id"])
            if ref.get("_candidate"):
                verdict = confirm_candidate(details, items, ids["source_repository"], titles)
                if not verdict["linked"]:
                    # Não confirmado: dado de outro repositório não entra no pacote (só número, título e contagens).
                    excluded.append({"number": details["number"], "title": details["title"], "reason": verdict["reason"],
                                     "matched": verdict["matched"], "issue_items": verdict["issue_items"],
                                     "hidden_repository_links": details["repositories"]["totalCount"]})
                    continue
                details["linkage"] = {"method": "TITLE_MATCH", "matched": verdict["matched"], "issue_items": verdict["issue_items"]}
            else:
                details["linkage"] = {"method": "VISIBLE_LINK"} if scope == "SOURCE_REPOSITORY" else {"method": "REQUESTED_SCOPE"}
            checks, redacted = check_project(details, items, total)
            details["items_summary"] = {"total": total, "read": len(items), "redacted": redacted,
                                        "by_type": {t: sum(1 for i in items if i["type"] == t) for t in sorted({i["type"] for i in items})}}
            details["checks"] = checks
            projects.append(details)
            all_items += [dict(i, project_number=details["number"], project_id=details["id"]) for i in items]
            partial = partial or redacted > 0
    except GraphqlError as exc:
        hint = " (token clássico com read:project? o repositório é privado e o token não tem `repo`?)" if exc.kind in ("HTTP 401", "HTTP 403", "NOT_FOUND", "INSUFFICIENT_SCOPES", "FORBIDDEN") else ""
        entry["detail"] = f"{exc}{hint}"
        print(f"::error::API_READ projects: FAILED: {exc}{hint}")
    except Exception as exc:  # noqa: BLE001
        # Resposta fora do formato esperado (por exemplo, um campo que o schema real não tem) ou bug
        # deste leitor. Nunca derruba o job: o backup do código segue e a classe fica FAILED com a causa.
        entry["detail"] = f"Unexpected {type(exc).__name__} while reading Projects: {exc}"
        print(f"::error::API_READ projects: FAILED: {entry['detail']}")
    else:
        files = []
        for name, data in (("api-projects.json", projects), ("api-project-items.json", all_items)):
            raw = write_json(package / name, data)
            files.append({"file": name, "items": len(data), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        checks = [dict(c, project_number=p["number"]) for p in projects for c in p["checks"]]
        failed = [c for c in checks if not c["ok"]]
        # Notas do que a regra E decidiu (aparecem no detail do status e na limitação do manifest).
        confirmed = [p for p in projects if p["linkage"]["method"] == "TITLE_MATCH"]
        if confirmed:
            notes.append("Project(s) " + ", ".join(f"#{p['number']}" for p in confirmed) + " have repository links hidden from the token and were "
                         "confirmed as related by title match (" + "; ".join(f"#{p['number']}: {p['linkage']['matched']}/{p['linkage']['issue_items']} issue items" for p in confirmed) + ").")
        if excluded:
            notes.append("Project(s) " + ", ".join(f"#{e['number']}" for e in excluded) + " have hidden repository links but were NOT confirmed "
                         "as related by title match and were excluded from the package (see extra.excluded_candidates).")
        status = "FAILED" if failed else ("PARTIAL" if partial else "OK")
        entry = {"status": status, "count": len(projects), "files": files, "checks": checks,
                 "extra": {"scope": scope, "items": len(all_items), "redacted_items": sum(p["items_summary"]["redacted"] for p in projects),
                           "discovery_notes": notes, "excluded_candidates": excluded}}
        if failed:
            entry["detail"] = "Reconciliation check failed: " + ", ".join(sorted({c["check"] for c in failed}))
        elif partial:
            entry["detail"] = " ".join(notes) or "Some item content is hidden from the token (redacted items)."
        print(f"API_READ projects: {status} count={len(projects)} items={len(all_items)}")

    inventory = {"schema": "api-projects-inventory/1", "scope": scope, "scope_identifiers": ids, "captured_at": now(),
                 "api_base": graph.url, "classes": {"projects": entry}, "limitations": LIMITATIONS}
    write_json(package / "api-projects-inventory.json", inventory)
    write_json(evidence / "api-projects-status.json", inventory)


COMMANDS = {"read": cmd_read}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(64)
    COMMANDS[sys.argv[1]]()
