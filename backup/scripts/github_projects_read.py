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
    2. Alternativa: lista os Projects do dono do repositório e fica com os que têm o repositório
       entre os ligados (`repositories`). O GraphQL devolve `null` no lugar de um repositório
       ligado que o token não vê; por isso, além dos Projects que ligam a origem de forma visível,
       ficam como CANDIDATOS os que têm algum vínculo oculto. Esse caminho é PARTIAL, e "sem
       Projects ligados" não é uma prova.

VARIÁVEIS DE AMBIENTE
    PROJECTS_TOKEN        token clássico com `read:project` (o secret PROJECTS_READ_TOKEN).
    PROJECTS_SCOPE        um dos três escopos acima.
    PROJECTS_SCOPE_IDS    JSON com os identificadores do escopo.
    EVIDENCE_DIR          pasta de evidências (padrão: evidence). Recebe api-projects-status.json.
    PACKAGE_DIR           pasta do pacote (padrão: package). Recebe api-projects*.json.
    GITHUB_API_URL        base da API (padrão: https://api.github.com); o GraphQL fica em
                          <base>/graphql. Existe para o teste local usar um servidor simulado.

ARQUIVOS GERADOS
    package/api-projects.json             cada Project: metadados, campos e opções, views,
                                          workflows, status updates e repositórios ligados
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
            # por nome. Regra: fica o Project que liga o repositório de origem de forma VISÍVEL e,
            # também, o que tem algum vínculo OCULTO (candidato: pode ser a origem ou outro
            # repositório privado). Incluir a mais é seguro para um backup; omitir não seria.
            owner = source.split("/", 1)[0]
            matched, candidates = [], []
            for project in list_owner_projects(graph, owner):
                linked = ((project.get("repositories") or {}).get("nodes")) or []
                visible = [r for r in linked if r]
                if any(r["nameWithOwner"].lower() == source.lower() for r in visible):
                    matched.append(project)
                elif len(visible) < len(linked):
                    candidates.append(project)
            notes.append("repository not visible to the token: Projects discovered by listing the owner's Projects "
                         "and filtering by linked repository; a link to a private repository is hidden from the token, "
                         "so an empty result is not proof of absence.")
            if candidates:
                numbers = ", ".join(f"#{c['number']}" for c in candidates)
                notes.append(f"Project(s) {numbers} have linked repositories hidden from the token and were included as "
                             "candidates (they may be linked to the source or to another private repository).")
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
    projects, all_items, notes, partial = [], [], [], False
    try:
        found, notes, partial = discover(graph, scope, ids)
        for ref in found:
            details = graph.run_query(Q_DETAILS, {"id": ref["id"]})["node"]
            items, total = read_items(graph, ref["id"])
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
        status = "FAILED" if failed else ("PARTIAL" if partial else "OK")
        entry = {"status": status, "count": len(projects), "files": files, "checks": checks,
                 "extra": {"scope": scope, "items": len(all_items), "redacted_items": sum(p["items_summary"]["redacted"] for p in projects),
                           "discovery_notes": notes}}
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
