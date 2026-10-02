#!/usr/bin/env python3
"""Leitor das classes de API do BKP_REPO: labels, milestones e issues (somente leitura).

O QUE É
    Lê da API REST do GitHub o estado das classes `labels`, `milestones` e `issues` do
    repositório de origem e grava cada uma como JSON bruto da API, para entrar no pacote do
    backup (package/api-*.json). Usa só a biblioteca padrão do Python (nenhuma dependência
    nova no workflow).

ONDE SE ENCAIXA NO FLUXO
    Roda no workflow `backup-repository.yml`, depois da verificação do mirror e ANTES do
    passo "Build restorable package", que leva os arquivos para o OneDrive. Quem decide a
    disposição final de cada classe (PRESERVED..., FAILED) é `bkp_repo.py build-evidence`,
    a partir do `api-status.json` que este script grava na pasta de evidências.

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - A origem é somente leitura: só faz requisições GET. Não existe POST, PATCH, PUT ou
      DELETE neste arquivo.
    - Leitura incompleta nunca vira sucesso: cada classe é conferida (contagens e
      referências cruzadas). Qualquer divergência marca a classe como FAILED.
    - Falha de permissão nunca é tratada como "repositório vazio": é FAILED com a causa.
    - O token nunca é impresso nem gravado. Mensagens de erro mostram só o status HTTP e o
      caminho da rota.

COMO USAR (o workflow já faz isto)
    SOURCE_TOKEN=... SOURCE_REPOSITORY=dono/nome EVIDENCE_DIR=evidence PACKAGE_DIR=package \\
        python3 backup/scripts/github_api_read.py read

VARIÁVEIS DE AMBIENTE
    SOURCE_TOKEN        token de LEITURA da origem (o mesmo SOURCE_READ_TOKEN do workflow).
                        Precisa de Issues: Read para labels, milestones e issues.
    SOURCE_REPOSITORY   dono/nome do repositório de origem.
    EVIDENCE_DIR        pasta de evidências (padrão: evidence). Recebe api-status.json.
    PACKAGE_DIR         pasta do pacote (padrão: package). Recebe api-*.json.
    GITHUB_API_URL      base da API (padrão: https://api.github.com). Existe para o teste
                        local usar um servidor simulado; no Actions já vem definida.

ARQUIVOS GERADOS
    package/api-labels.json           etiquetas, como a API devolve
    package/api-milestones.json       marcos (abertos e fechados)
    package/api-issues.json           issues (SEM os pull requests; ver limitações)
    package/api-issue-comments.json   comentários das issues
    package/api-issue-events.json     eventos das issues (rotulou, fechou, atribuiu...)
    package/api-issue-reactions.json  reações detalhadas (só onde há reações)
    package/api-inventory.json        contagens, SHA-256 de cada arquivo, conferências e limitações
    evidence/api-status.json          resultado por classe (lido pelo build-evidence)

CÓDIGOS DE SAÍDA
    0   terminou (cada classe tem OK, DISABLED ou FAILED no api-status.json)
    64  uso incorreto ou variável de ambiente ausente
"""
import datetime
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Classes que este script cobre, na ordem em que são lidas. `issues` vem por último porque
# confere suas referências (etiquetas e marcos) contra as duas primeiras.
CLASSES = ("labels", "milestones", "issues")

# Itens por página da API (o máximo permitido).
PER_PAGE = 100
# Teto de páginas por listagem: evita laço infinito se a API devolver sempre "próxima página".
MAX_PAGES = 2000
# Tentativas por requisição em falha transitória (rede, 5xx, limite de taxa).
MAX_TRIES = 5
# Espera máxima, em segundos, por um limite de taxa. Acima disso a leitura falha.
MAX_RATE_WAIT = 120

# Função de espera. É uma variável para o teste poder trocá-la e não esperar de verdade.
sleep = time.sleep

# Limitações permanentes desta versão do leitor, copiadas para o inventário.
LIMITATIONS = [
    "Pull requests are not preserved by this reader: they are listed in api-inventory.json "
    "(numbers only) and remain a pending object class (pull_requests).",
    "Issue relationships are limited to the fields present in the issue JSON "
    "(for example parent_issue_url and sub_issues_summary); the dedicated sub-issue and "
    "dependency endpoints and the full timeline are not read.",
    "Issues are an equivalent representation: the original number, author and dates cannot "
    "be restored identically by the GitHub API.",
]


class ApiError(Exception):
    """Falha da API que não adianta repetir (ou que esgotou as tentativas)."""

    def __init__(self, status, route, detail=""):
        self.status = status
        self.route = route
        self.detail = detail
        super().__init__(f"HTTP {status} em {route}" + (f": {detail}" if detail else ""))


def now():
    """Instante atual em UTC, formato ISO 8601 com Z."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


class Client:
    """Cliente mínimo da API REST do GitHub: só GET, com repetição e paginação."""

    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def _open(self, url, route):
        """Faz um GET (com repetição em falha transitória) e devolve (corpo JSON, cabeçalhos)."""
        request = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "repo-backup-bkp-repo",
        })
        for attempt in range(1, MAX_TRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8")), response.headers
            except urllib.error.HTTPError as exc:
                headers = exc.headers
                # Limite de taxa: espera até o reinício, se for curto.
                if exc.code in (403, 429) and (headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0"):
                    wait = self._rate_wait(headers)
                    if wait > MAX_RATE_WAIT or attempt == MAX_TRIES:
                        raise ApiError(exc.code, route, "limite de taxa da API esgotado") from None
                    sleep(wait)
                    continue
                if exc.code >= 500 and attempt < MAX_TRIES:
                    sleep(2 * attempt)
                    continue
                # Outros 4xx (401, 403, 404, 410...) não melhoram se repetidos.
                raise ApiError(exc.code, route) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == MAX_TRIES:
                    raise ApiError(0, route, f"falha de rede: {type(exc).__name__}") from None
                sleep(2 * attempt)
        raise ApiError(0, route, "tentativas esgotadas")

    @staticmethod
    def _rate_wait(headers):
        """Segundos até o limite de taxa reiniciar (1 se o cabeçalho não existir)."""
        retry = headers.get("Retry-After")
        if retry and retry.isdigit():
            return int(retry)
        reset = headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            return max(1, int(reset) - int(time.time()) + 1)
        return 1

    def get(self, route, params=None):
        """GET de um objeto só. Devolve o JSON."""
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        body, _ = self._open(self.base + route + query, route)
        return body

    def pages(self, route, params=None):
        """GET de uma lista paginada. Segue o cabeçalho Link (rel="next") até o fim."""
        query = dict(params or {})
        query["per_page"] = PER_PAGE
        url = self.base + route + "?" + urllib.parse.urlencode(query)
        items = []
        for _ in range(MAX_PAGES):
            body, headers = self._open(url, route)
            if not isinstance(body, list):
                raise ApiError(0, route, "resposta inesperada: esperava uma lista")
            items.extend(body)
            url = self._next_link(headers.get("Link"))
            if not url:
                return items
        raise ApiError(0, route, f"mais de {MAX_PAGES} páginas")

    @staticmethod
    def _next_link(link_header):
        """Extrai a URL rel="next" do cabeçalho Link, ou None na última página."""
        for part in (link_header or "").split(","):
            segments = part.split(";")
            if len(segments) >= 2 and 'rel="next"' in segments[1]:
                return segments[0].strip().strip("<>")
        return None


def issue_number_of(url):
    """Número da issue a partir de uma issue_url ('.../issues/12'); None se não for possível."""
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def read_labels(client, repo):
    """Classe labels: todas as etiquetas, com nome, cor e descrição."""
    items = client.pages(f"/repos/{repo}/labels")
    return {"files": {"api-labels.json": items}, "count": len(items), "checks": []}


def read_milestones(client, repo):
    """Classe milestones: todos os marcos, abertos e fechados."""
    items = client.pages(f"/repos/{repo}/milestones", {"state": "all"})
    return {"files": {"api-milestones.json": items}, "count": len(items), "checks": []}


def read_issues(client, repo, known_labels=None, known_milestones=None):
    """Classe issues: issues, comentários, eventos e reações. Pull requests ficam de fora.

    A API de issues devolve também os pull requests; eles são separados pelo campo
    `pull_request` e só têm o número registrado no inventário.

    Conferências (qualquer falha vira FAILED, nunca sucesso):
      - total de abertos (issues + pull requests) igual ao `open_issues_count` do repositório;
      - por issue, o campo `comments` igual ao número de comentários lidos;
      - toda etiqueta e todo marco citados pelas issues existem nas listas lidas.
    """
    everything = client.pages(f"/repos/{repo}/issues", {"state": "all", "sort": "created", "direction": "asc"})
    issues = [i for i in everything if "pull_request" not in i]
    prs = sorted(i["number"] for i in everything if "pull_request" in i)
    numbers = {i["number"] for i in issues}
    pr_numbers = set(prs)

    # Comentários de todo o repositório; fica só o que pertence às issues (não aos PRs).
    comments = [c for c in client.pages(f"/repos/{repo}/issues/comments")
                if issue_number_of(c.get("issue_url")) in numbers]
    # Eventos de todo o repositório, só os das issues. O objeto `issue` embutido é reduzido
    # ao número (o texto completo já está em api-issues.json).
    events = []
    for ev in client.pages(f"/repos/{repo}/issues/events"):
        number = (ev.get("issue") or {}).get("number")
        if number in numbers:
            ev = dict(ev)
            ev["issue"] = {"number": number}
            events.append(ev)
    # Reações detalhadas só onde a contagem é maior que zero (evita uma chamada por item).
    reactions = []
    for issue in issues:
        if (issue.get("reactions") or {}).get("total_count"):
            reactions.append({"target": f"issue:{issue['number']}",
                              "reactions": client.pages(f"/repos/{repo}/issues/{issue['number']}/reactions")})
    for comment in comments:
        if (comment.get("reactions") or {}).get("total_count"):
            reactions.append({"target": f"comment:{comment['id']}",
                              "reactions": client.pages(f"/repos/{repo}/issues/comments/{comment['id']}/reactions")})

    checks = []
    # 1. Total de abertos contra o que o próprio repositório informa.
    expected_open = client.get(f"/repos/{repo}")["open_issues_count"]
    read_open = sum(1 for i in everything if i["state"] == "open")
    checks.append({"check": "open_issues_count", "expected": expected_open, "read": read_open,
                   "ok": expected_open == read_open})
    # 2. Comentários lidos contra o contador de cada issue.
    per_issue = {}
    for c in comments:
        per_issue[issue_number_of(c["issue_url"])] = per_issue.get(issue_number_of(c["issue_url"]), 0) + 1
    mismatched = sorted(i["number"] for i in issues if i.get("comments", 0) != per_issue.get(i["number"], 0))
    checks.append({"check": "comments_per_issue", "expected": sum(i.get("comments", 0) for i in issues),
                   "read": len(comments), "ok": not mismatched, "mismatched_issues": mismatched})
    # 3. Referências cruzadas, só quando a classe de origem foi lida.
    if known_labels is not None:
        used = {lb["name"] for i in issues for lb in i.get("labels", [])}
        missing = sorted(used - known_labels)
        checks.append({"check": "labels_exist", "ok": not missing, "missing": missing})
    if known_milestones is not None:
        used = {i["milestone"]["number"] for i in issues if i.get("milestone")}
        missing = sorted(used - known_milestones)
        checks.append({"check": "milestones_exist", "ok": not missing, "missing": missing})

    return {"files": {"api-issues.json": issues, "api-issue-comments.json": comments,
                      "api-issue-events.json": events, "api-issue-reactions.json": reactions},
            "count": len(issues), "checks": checks,
            "extra": {"comments": len(comments), "events": len(events), "reaction_targets": len(reactions),
                      "pull_requests_skipped": prs}}


def write_json(path, data):
    """Grava JSON legível e determinístico; devolve o conteúdo em bytes (para o SHA-256)."""
    raw = (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


def cmd_read():
    """Lê as três classes e grava os arquivos do pacote e o api-status.json."""
    try:
        repo = os.environ["SOURCE_REPOSITORY"]
        token = os.environ["SOURCE_TOKEN"]
    except KeyError as exc:
        print(f"::error::Variável ausente: {exc.args[0]}")
        sys.exit(64)
    evidence = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))
    package = pathlib.Path(os.environ.get("PACKAGE_DIR", "package"))
    evidence.mkdir(parents=True, exist_ok=True)
    package.mkdir(parents=True, exist_ok=True)
    client = Client(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token)

    statuses = {}
    known = {"labels": None, "milestones": None}
    for cls in CLASSES:
        try:
            if cls == "labels":
                out = read_labels(client, repo)
                known["labels"] = {lb["name"] for lb in out["files"]["api-labels.json"]}
            elif cls == "milestones":
                out = read_milestones(client, repo)
                known["milestones"] = {m["number"] for m in out["files"]["api-milestones.json"]}
            else:
                out = read_issues(client, repo, known["labels"], known["milestones"])
        except ApiError as exc:
            # 410 Gone = recurso desativado no repositório (ex.: Issues desligadas): estado
            # conhecido e vazio, não falha. Qualquer outro erro é FAILED com a causa.
            if exc.status == 410:
                statuses[cls] = {"status": "DISABLED", "count": 0, "files": [], "checks": [],
                                 "detail": f"Feature disabled in the source repository (HTTP 410 on {exc.route})."}
                print(f"API_READ {cls}: DISABLED")
            else:
                hint = " (o token precisa de Issues: Read?)" if exc.status in (401, 403, 404) else ""
                statuses[cls] = {"status": "FAILED", "count": 0, "files": [], "checks": [],
                                 "detail": f"{exc}{hint}"}
                print(f"::error::API_READ {cls}: FAILED: {exc}{hint}")
            continue
        # Gravação dos arquivos da classe e conferência final.
        files = []
        for name, data in out["files"].items():
            raw = write_json(package / name, data)
            files.append({"file": name, "items": len(data), "sha256": sha256_bytes(raw), "bytes": len(raw)})
        failed = [c for c in out["checks"] if not c["ok"]]
        statuses[cls] = {"status": "FAILED" if failed else "OK", "count": out["count"], "files": files,
                         "checks": out["checks"], **({"extra": out["extra"]} if "extra" in out else {}),
                         **({"detail": "Reconciliation check failed: " + ", ".join(c["check"] for c in failed)} if failed else {})}
        print(f"API_READ {cls}: {statuses[cls]['status']} count={out['count']}")

    inventory = {"schema": "api-inventory/1", "source_repository": repo, "captured_at": now(),
                 "api_base": client.base, "classes": statuses, "limitations": LIMITATIONS}
    write_json(package / "api-inventory.json", inventory)
    write_json(evidence / "api-status.json", inventory)


COMMANDS = {"read": cmd_read}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(64)
    COMMANDS[sys.argv[1]]()
