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
import re
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
# Início da mensagem do erro interno do GitHub (costuma ser transitório).
INTERNAL_ERROR = "Something went wrong while executing your query"
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
    "Views are read with layout, filter, visible fields, grouping, vertical grouping and sorting. Workflows are read only as name, "
    "number and enabled state: the GitHub API does not expose their rules (trigger, filter, action). Project collaborators are not read.",
    "Items are read in POSITION order, archived items included (`position_order` is the index in that order).",
]

# Uma operação `mutation` (ou `subscription`) no documento: a palavra sozinha, seguida do nome opcional e de `{` ou `(`.
# Não pega `mutationType` (campo da introspecção) nem a palavra entre aspas (`__type(name: "Mutation")`).
MUTATION_OP = re.compile(r"(?<![\w\"])(mutation|subscription)(?![\w\"])\s*(\w+\s*)?[({]", re.IGNORECASE)

# Consultas. Cada uma tem nome (operationName) e só LÊ. Os trechos repetidos ficam em constantes.
_PROJECT_REF = "id number title"
_PAGEINFO = "pageInfo { hasNextPage endCursor }"
_FIELD_REF = "field { ... on ProjectV2FieldCommon { id name } }"
_FIELD_CONN = "totalCount " + _PAGEINFO + " nodes { ... on ProjectV2FieldCommon { id name } }"

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

_ITEM_NODE = f"""id type isArchived createdAt updatedAt
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
          }}"""


def _items_query(name, args, node):
    """Consulta dos itens de um Project; `args` são os argumentos extras de `items` (estados de arquivamento, ordem)."""
    return f"""query {name}($id: ID!, $after: String) {{
  node(id: $id) {{
    ... on ProjectV2 {{
      items(first: {PAGE}, after: $after{args}) {{
        totalCount {_PAGEINFO}
        nodes {{
          {node}
        }}
      }}
    }}
  }}
}}"""


# Principal: a forma que já rodou em run real (os itens não arquivados). As leituras opcionais abaixo são separadas: se uma falhar,
# a classe fica PARTIAL com a causa e o resto do backup é guardado.
Q_ITEMS = _items_query("ProjectItems", "", _ITEM_NODE)
Q_ITEMS_ARCHIVED = _items_query("ProjectItemsArchived", ", archivedStates: [ARCHIVED]", _ITEM_NODE)
Q_ITEMS_ORDER = _items_query("ProjectItemsOrder", ", archivedStates: [ARCHIVED, NOT_ARCHIVED], orderBy: {field: POSITION, direction: ASC}", "id")


def _view_part(name, selection):
    return f"""query ProjectView{name}($id: ID!) {{
  node(id: $id) {{
    ... on ProjectV2 {{ views(first: 50) {{ nodes {{ number {selection} }} }} }}
  }}
}}"""


# Partes das views, uma consulta cada, para uma falha do GitHub num atributo não perder os outros.
VIEW_PARTS = {
    "visibleFields": _view_part("VisibleFields", f"configuration {{ visibleFields(first: 100) {{ {_FIELD_CONN} }} }}"),
    # `visibleFields(first: 100)` falhou no GitHub real de forma persistente (erro interno, run req-20261004-004). Duas alternativas, cada
    # uma por conta própria: a mesma consulta com página menor (grava no mesmo `configuration`) e a lista `fields` da própria view.
    "visibleFieldsSmall": _view_part("VisibleFieldsSmall", f"configuration {{ visibleFields(first: 20) {{ {_FIELD_CONN} }} }}"),
    "fields": _view_part("Fields", f"fields(first: 100) {{ {_FIELD_CONN} }}"),
    "groupByFields": _view_part("GroupBy", f"groupByFields(first: 20) {{ {_FIELD_CONN} }}"),
    "verticalGroupByFields": _view_part("VerticalGroupBy", f"verticalGroupByFields(first: 20) {{ {_FIELD_CONN} }}"),
    "sortByFields": _view_part("SortBy", f"sortByFields(first: 20) {{ totalCount {_PAGEINFO} nodes {{ direction {_FIELD_REF} }} }}"),
}


# Sonda do schema: só introspecção (consulta). Registra no inventário o que a API oferecia no momento do backup,
# para as decisões de restauração (o que se pode criar) e de cobertura do backup (o que se pode ler) terem evidência.
_TYPE_REF = "kind name ofType { kind name ofType { kind name ofType { kind name } } }"
Q_PROBE_MUTATIONS = "query ProbeMutations { __schema { mutationType { fields { name } } } }"
Q_PROBE_TYPE = f"""query ProbeType($name: String!) {{
  __type(name: $name) {{
    name kind
    fields {{ name type {{ {_TYPE_REF} }} args {{ name type {{ {_TYPE_REF} }} }} }}
    inputFields {{ name type {{ {_TYPE_REF} }} }}
    enumValues {{ name }}
    possibleTypes {{ name }}
  }}
}}"""
# Tipos que importam para a restauração de Projects (entradas de criação) e para a cobertura do backup (views, workflows...).
PROBE_TYPES = ("ProjectV2", "ProjectV2View", "ProjectV2Workflow", "ProjectV2StatusUpdate", "ProjectV2Item", "ProjectV2FieldCommon",
               "CreateProjectV2Input", "CopyProjectV2Input", "CreateProjectV2FieldInput", "UpdateProjectV2FieldInput",
               "UpdateProjectV2Input", "AddProjectV2DraftIssueInput", "UpdateProjectV2ItemPositionInput",
               "CreateProjectV2StatusUpdateInput", "ProjectV2CustomFieldType")
# A sonda também segue: a entrada de cada mutation de ProjectV2 (`<Mutation>Input`) e todo tipo citado, pelos campos, argumentos,
# entradas ou tipos possíveis, cujo nome contém "ProjectV2" (a vizinhança do schema). Teto de tipos para a sonda não crescer sem fim.
PROBE_MAX_TYPES = 150


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
        if not query.lstrip().startswith("query") or MUTATION_OP.search(query):
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
                # Erro interno do GitHub ("Something went wrong while executing your query"): tenta de novo.
                first_error = (payload.get("errors") or [{}])[0]
                if payload.get("errors") and str(first_error.get("message", "")).startswith(INTERNAL_ERROR) and attempt < MAX_TRIES:
                    sleep(2 * attempt)
                    continue
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
            # O nome da operação vai junto: com várias consultas, é o que diz qual delas o GitHub recusou.
            raise GraphqlError(first.get("type") or "GRAPHQL_ERROR", f"[{name or 'sem nome'}] {first.get('message', '')}")
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


def read_items(graph, project_id, query=None):
    """Todos os itens de um Project, paginados. Devolve (itens, totalCount informado)."""
    items, after, total = [], None, None
    for _ in range(MAX_PAGES):
        data = graph.run_query(query or Q_ITEMS, {"id": project_id, "after": after})
        conn = data["node"]["items"]
        total = conn["totalCount"]
        items += conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return items, total
        after = conn["pageInfo"]["endCursor"]
    raise GraphqlError("LIMIT", f"mais de {MAX_PAGES} páginas")


def check_project(details, items, total, archived_total=None):
    """Conferências de UM Project. Devolve (checks, quantidade de itens ocultos).
    `total` é o totalCount dos itens não arquivados; `archived_total`, o dos arquivados (None = leitura opcional indisponível)."""
    checks = []
    # 1. Total de itens lido contra o totalCount que a API informa (não arquivados e, à parte, arquivados).
    live = sum(1 for i in items if not i.get("isArchived"))
    checks.append({"check": "items_total", "expected": total, "read": live, "ok": total == live})
    # 2. Nenhuma conexão cortada: o que a API ainda tinha para entregar e não foi lido.
    truncated = [n for n in ("repositories", "fields", "views", "workflows", "statusUpdates")
                 if (details.get(n) or {}).get("pageInfo", {}).get("hasNextPage")]
    truncated += ["item.fieldValues" for i in items if i["fieldValues"]["pageInfo"]["hasNextPage"]][:1]
    for view in (details.get("views") or {}).get("nodes", []):
        conns = {"visibleFields": (view.get("configuration") or {}).get("visibleFields"), "fields": view.get("fields"), "groupByFields": view.get("groupByFields"),
                 "verticalGroupByFields": view.get("verticalGroupByFields"), "sortByFields": view.get("sortByFields")}
        truncated += [f"views[{view['number']}].{n}" for n, c in conns.items() if c and c.get("pageInfo", {}).get("hasNextPage")]
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
    # 5. Itens arquivados: o GitHub só os entrega se pedidos (`archivedStates`), numa leitura opcional à parte. Disponível: o total
    #    lido tem de bater com o informado. Indisponível: não é falha de leitura, mas a classe fica PARTIAL (ver cmd_read).
    archived = sum(1 for i in items if i.get("isArchived"))
    if archived_total is None:
        checks.append({"check": "archived_items", "ok": True, "unavailable": True})
    else:
        checks.append({"check": "archived_items", "ok": archived_total == archived, "expected": archived_total, "read": archived})
    return checks, redacted


def type_name(ref):
    """Texto curto de uma referência de tipo da introspecção, como `String!` ou `[ID!]!`."""
    if ref is None:
        return "?"
    inner = ref.get("name") or (type_name(ref.get("ofType")) if ref.get("ofType") else "?")
    if ref.get("kind") == "NON_NULL":
        return inner + "!"
    if ref.get("kind") == "LIST":
        return f"[{inner}]"
    return inner


def base_name(ref):
    """Nome do tipo nomeado por baixo de NON_NULL e LIST."""
    while ref is not None and not ref.get("name"):
        ref = ref.get("ofType")
    return ref.get("name") if ref else None


def related_types(node):
    """Tipos de Projects citados por um tipo da introspecção (campos, argumentos, entradas e tipos possíveis)."""
    names = set()
    for f in node.get("fields") or []:
        names.add(base_name(f["type"]))
        names.update(base_name(a["type"]) for a in f.get("args") or [])
    for f in node.get("inputFields") or []:
        names.add(base_name(f["type"]))
    names.update(p["name"] for p in node.get("possibleTypes") or [])
    return {n for n in names if n and "ProjectV2" in n}


def probe_schema(graph):
    """Introspecção do schema (só consulta). Nunca levanta: qualquer falha vira {"status": "UNAVAILABLE", ...}."""
    try:
        fields = graph.run_query(Q_PROBE_MUTATIONS)["__schema"]["mutationType"]["fields"]
        mutations = sorted(f["name"] for f in fields)
        project_mutations = [m for m in mutations if "projectv2" in m.lower()]
        queue = list(PROBE_TYPES) + [m[0].upper() + m[1:] + "Input" for m in project_mutations]
        types, seen, truncated = {}, set(), []
        while queue:
            name = queue.pop(0)
            if name in seen:
                continue
            if len(seen) >= PROBE_MAX_TYPES:
                truncated.append(name)
                continue
            seen.add(name)
            node = graph.run_query(Q_PROBE_TYPE, {"name": name})["__type"]
            if node is None:
                types[name] = None
                continue
            entry = {"kind": node["kind"]}
            if node.get("fields") is not None:
                entry["fields"] = {f["name"]: type_name(f["type"]) for f in node["fields"]}
                args = {f["name"]: {a["name"]: type_name(a["type"]) for a in f["args"]} for f in node["fields"] if f.get("args")}
                if args:
                    entry["field_args"] = args
            if node.get("inputFields") is not None:
                entry["input_fields"] = {f["name"]: type_name(f["type"]) for f in node["inputFields"]}
            if node.get("enumValues") is not None:
                entry["enum_values"] = [v["name"] for v in node["enumValues"]]
            if node.get("possibleTypes"):
                entry["possible_types"] = sorted(p["name"] for p in node["possibleTypes"])
            types[name] = entry
            queue.extend(sorted(related_types(node) - seen))
        return {"status": "OK", "captured_at": now(), "mutations_total": len(mutations), "project_v2_mutations": project_mutations,
                "types": types, "types_not_followed": sorted(set(truncated))}
    except GraphqlError as exc:
        return {"status": "UNAVAILABLE", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "UNAVAILABLE", "detail": f"Unexpected {type(exc).__name__}: {exc}"}


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


def optional_read(graph_call, label, extras):
    """Executa uma leitura opcional. Falha vira `extras[label] = UNAVAILABLE` com a causa (e devolve None): nunca derruba o resto."""
    try:
        result = graph_call()
        extras[label] = {"status": "OK"}
        return result
    except GraphqlError as exc:
        extras[label] = {"status": "UNAVAILABLE", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        extras[label] = {"status": "UNAVAILABLE", "detail": f"Unexpected {type(exc).__name__}: {exc}"}
    return None


def read_optional_parts(graph, ref, details, items):
    """Leituras opcionais de UM Project, cada uma numa consulta própria: partes das views, itens arquivados e ordem por posição.
    Mescla o que veio em `details` e em `items`. Devolve (extras por rótulo, total informado de arquivados ou None, itens arquivados lidos)."""
    extras = {}
    for part, query in VIEW_PARTS.items():
        nodes = optional_read(lambda q=query: graph.run_query(q, {"id": ref["id"]})["node"]["views"]["nodes"], f"views.{part}", extras)
        by_number = {v["number"]: v for v in nodes or []}
        for view in details["views"]["nodes"]:
            for key, value in (by_number.get(view["number"]) or {}).items():
                if key != "number":
                    view[key] = value
    archived = optional_read(lambda: read_items(graph, ref["id"], Q_ITEMS_ARCHIVED), "items.archived", extras)
    order = optional_read(lambda: read_items(graph, ref["id"], Q_ITEMS_ORDER)[0], "items.position", extras)
    archived_items, archived_total = archived if archived else ([], None)
    if order is not None:
        position = {i["id"]: n for n, i in enumerate(order)}
        for item in items + archived_items:
            item["position_order"] = position.get(item["id"])
    return extras, archived_total, archived_items


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
            # Leituras opcionais (views por parte, itens arquivados, ordem): falha de uma vira nota e PARTIAL, o resto é guardado.
            extras, archived_total, archived_items = read_optional_parts(graph, ref, details, items)
            items = items + archived_items
            checks, redacted = check_project(details, items, total, archived_total)
            details["items_summary"] = {"total": total, "read": len(items) - len(archived_items), "archived": len(archived_items), "redacted": redacted,
                                        "by_type": {t: sum(1 for i in items if i["type"] == t) for t in sorted({i["type"] for i in items})}}
            details["checks"] = checks
            details["optional_reads"] = extras
            for label, state in sorted(extras.items()):
                if state["status"] != "OK":
                    notes.append(f"Project #{details['number']}: optional read {label} unavailable ({state['detail']}).")
                    partial = True
            if any(f.get("dataType") == "MULTI_SELECT" for f in details["fields"]["nodes"]):
                notes.append(f"Project #{details['number']} has multi-select fields: their options and item values are not read by this reader version.")
                partial = True
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
                 "api_base": graph.url, "classes": {"projects": entry}, "limitations": LIMITATIONS,
                 "schema_probe": probe_schema(graph)}
    print(f"API_PROBE projects: {inventory['schema_probe']['status']}")
    write_json(package / "api-projects-inventory.json", inventory)
    write_json(evidence / "api-projects-status.json", inventory)


COMMANDS = {"read": cmd_read}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(64)
    COMMANDS[sys.argv[1]]()
