#!/usr/bin/env python3
"""Leitor da classe `pull_requests` do BKP_REPO: pull requests e tudo que os rodeia (somente leitura).

O QUE É
    Lê da API do GitHub, para cada pull request do repositório de origem (todos os estados): o PR, os commits, os
    arquivos alterados (com o patch), os reviews, os comentários de review, as threads de review (resolvida,
    desatualizada), os comentários de conversa, os eventos, os rótulos e as relações (base e head, referências de
    fechamento, referências cruzadas e conexões). REST para quase tudo; GraphQL (só consultas) para as threads e
    para as referências de fechamento. Usa só a biblioteca padrão do Python e o cliente de `github_actions_read.py`.

CLASSE SÓ DE REGISTRO
    Os arquivos preservam o que a API devolveu. NÃO existe restauração de PRs nesta etapa (as refs refs/pull/* não
    voltam ao GitHub, e criar um PR exige as branches de base e de head): a cópia equivalente é uma etapa futura e
    separada. A parte Git dos PRs (refs/pull/*/head) já vai no bundle da classe `git`.

LIMITES DECLARADOS (nunca viram "sucesso" nem "ausente")
    - A API lista no máximo 250 commits e 3000 arquivos por PR: acima disso o PR fica LIMITED (a classe é PARTIAL),
      sem contornar. Os commits continuam no bundle Git.
    - Arquivo sem `patch` (binário ou grande demais) é limitação REGISTRADA (contada no inventário), não falha.
    - Reações (em PRs, comentários e reviews) estão fora do escopo desta versão.
    - A diferença entre o total de comentários das threads (GraphQL) e o dos comentários de review (REST) NÃO é
      falha: o PR fica LIMITED, a classe PARTIAL, e as duas contagens vão para a evidência.
    - Teto de PULLS_MAX PRs (padrão 2000) e PULLS_MAX_BYTES (padrão 256 MiB): do menor número para o maior; o
      excedente fica SKIPPED_OVER_CAP.

CONFERÊNCIAS (divergência = FAILED)
    ids únicos; commits, arquivos, comentários de review e comentários de conversa lidos contra os contadores do
    PR; merged coerente com merged_at; rótulos do PR existentes em api-labels.json; e a lista de PRs contra os
    números que o leitor de issues separou (pull_requests_skipped), SÓ se a classe issues foi lida completa (OK)
    no mesmo run; senão a comparação é apenas informativa. O head.sha de cada PR contra evidence/refs.tsv também é
    informativo (um push no meio da leitura é legítimo).

PERMISSÕES (SOURCE_TOKEN = SOURCE_READ_TOKEN)
    Pull requests: Read-only (fine-grained). Sem ela, a classe é FAILED com a dica. As threads usam a GraphQL com
    o mesmo token; se ela falhar, os PRs ficam LIMITED (PARTIAL), nunca FAILED.

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - A origem é somente leitura: só GET e consultas GraphQL (uma mutation ou subscription é recusada antes de
      enviar; o documento precisa começar por `query`).
    - O token nunca é impresso nem gravado.
    - O LOG PÚBLICO traz só contagens e tamanhos. Nenhum título, descrição, branch, nome de arquivo, usuário,
      mensagem de commit ou de review; as rotas saem sem números nem nomes. Os dados vão só ao pacote e à
      evidência (OneDrive).

COMO USAR (o workflow já faz isto)
    SOURCE_TOKEN=... SOURCE_REPOSITORY=dono/nome EVIDENCE_DIR=evidence PACKAGE_DIR=package \\
        python3 backup/scripts/github_pulls_read.py read

VARIÁVEIS DE AMBIENTE
    SOURCE_TOKEN, SOURCE_REPOSITORY, EVIDENCE_DIR (padrão evidence), PACKAGE_DIR (padrão package),
    PULLS_MAX (padrão 2000), PULLS_MAX_BYTES (padrão 256 MiB), GITHUB_API_URL (teste local).

ARQUIVOS GERADOS
    package/api-pulls.json                 o objeto de cada PR, como a API devolve
    package/api-pull-commits.json          commits por PR
    package/api-pull-files.json            arquivos alterados por PR (com patch quando a API o envia)
    package/api-pull-reviews.json          reviews por PR
    package/api-pull-review-comments.json  comentários de review por PR
    package/api-pull-comments.json         comentários de conversa por PR
    package/api-pull-events.json           eventos por PR
    package/api-pull-threads.json          threads de review por PR (GraphQL)
    package/api-pull-relations.json        base e head, fechamento, referências cruzadas e conexões por PR
    package/api-pulls-inventory.json       contagens, SHA-256 de cada arquivo, conferências, limitações
    evidence/api-pulls-status.json         resultado da classe (lido pelo build-evidence)

CÓDIGOS DE SAÍDA
    0   terminou (a classe tem OK, PARTIAL ou FAILED no api-pulls-status.json)
    64  uso incorreto ou variável de ambiente ausente
"""
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import github_actions_read as ar
import github_api_read as g
from github_api_read import ApiError, now, sha256_bytes, write_json

CLASS = "pull_requests"
DEFAULT_MAX_PULLS = 2000
DEFAULT_MAX_BYTES = 256 * 1024 ** 2
PER_PAGE = g.PER_PAGE
MAX_PAGES = g.MAX_PAGES
GRAPHQL_PAGE = 50
# Limites da própria API do GitHub.
COMMITS_API_LIMIT = 250
FILES_API_LIMIT = 3000

AVAILABLE = "AVAILABLE"
LIMITED = "LIMITED"                      # lido, com limite da API, threads ausentes ou divergência de totais
SKIPPED_OVER_CAP = "SKIPPED_OVER_CAP"
FAILED_ITEM = "FAILED"
GAP_STATES = (LIMITED, SKIPPED_OVER_CAP)

LIMITATIONS = [
    "Pull requests are preserved as a record only (API JSON): they cannot be restored to GitHub in this version; an equivalent copy is a future, separate step. The refs/pull/*/head refs are part of the git class (bundle).",
    "The GitHub API lists at most 250 commits and 3000 files per pull request; a pull request above either limit is reported LIMITED (the class is PARTIALLY-PRESERVED) and the commits remain in the git bundle.",
    "A changed file without a patch (binary or too large) is a registered limitation, not a failure: the file is listed with its metadata but without the diff text.",
    "Reactions (on pull requests, review comments, reviews and conversation comments) are out of scope in this version.",
    "Review threads come from the GraphQL API with the same token; when the thread comment totals differ from the REST review comment totals, or the GraphQL API is unavailable, the pull request is reported LIMITED with both counts.",
    "Relations are limited to base and head (repository and ref), closing issue references (GraphQL) and cross-references and connections found in the issue timeline; the full timeline is not stored.",
    "At most PULLS_MAX pull requests and PULLS_MAX_BYTES of JSON are read, from the lowest number to the highest; the rest is listed as SKIPPED_OVER_CAP.",
]


def mask(route):
    """Rota sem números nem nomes: '/pulls/7/files' -> '/pulls/{id}/files'."""
    return re.sub(r"/\d+", "/{id}", route)


THREADS_QUERY = """query PullThreads($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: %d, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated isCollapsed path line startLine diffSide
          resolvedBy { login }
          comments(first: %d) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes { databaseId createdAt updatedAt body path diffHunk outdated author { login } replyTo { databaseId } }
          }
        }
      }
    }
  }
}""" % (GRAPHQL_PAGE, GRAPHQL_PAGE)

THREAD_COMMENTS_QUERY = """query ThreadComments($id: ID!, $after: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: %d, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { databaseId createdAt updatedAt body path diffHunk outdated author { login } replyTo { databaseId } }
      }
    }
  }
}""" % GRAPHQL_PAGE

CLOSING_QUERY = """query PullClosing($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: %d, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { number repository { nameWithOwner } }
      }
    }
  }
}""" % GRAPHQL_PAGE


class GraphQLError(Exception):
    """A GraphQL respondeu com erro (sem o texto, que pode trazer dados)."""


def assert_query_only(document):
    """Recusa qualquer documento que não seja uma consulta (nada de mutation nem subscription)."""
    head = document.lstrip()
    if not head.startswith("query") or re.search(r"\b(mutation|subscription)\b", document):
        raise ValueError("only GraphQL queries are allowed")


class PullsClient(ar.Client):
    """Cliente do leitor de Actions com rotas mascaradas, listas simples e GraphQL só de consulta."""

    def _open(self, url, route):
        def fn():
            request = urllib.request.Request(url, method="GET", headers=self._headers())
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8")), response.headers
        return self._call(mask(route), fn)

    def get(self, route, params=None):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        body, _ = self._open(self.base + route + query, route)
        return body

    def pages_list(self, route, params=None):
        query = dict(params or {})
        query["per_page"] = PER_PAGE
        url = self.base + route + "?" + urllib.parse.urlencode(query)
        items = []
        for _ in range(MAX_PAGES):
            body, headers = self._open(url, route)
            if not isinstance(body, list):
                raise ApiError(0, mask(route), "unexpected response: expected a list")
            items.extend(body)
            url = self._next_link(headers.get("Link"))
            if not url:
                return items
        raise ApiError(0, mask(route), f"more than {MAX_PAGES} pages")

    def graphql(self, document, variables):
        """Consulta GraphQL (POST). Devolve `data`; erros viram GraphQLError sem o texto da mensagem."""
        assert_query_only(document)
        payload = json.dumps({"query": document, "variables": variables}).encode("utf-8")

        def fn():
            request = urllib.request.Request(self.base + "/graphql", data=payload, method="POST",
                                             headers=dict(self._headers(), **{"Content-Type": "application/json"}))
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        body = self._call("/graphql", fn)
        if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
            raise GraphQLError("GraphQL error")
        return body["data"]


def split_repo(repo):
    owner, name = repo.split("/", 1)
    return owner, name


def read_threads(client, repo, number):
    """Threads de review do PR, com os comentários de cada thread paginados. Devolve a lista de threads."""
    owner, name = split_repo(repo)
    threads, after = [], None
    for _ in range(MAX_PAGES):
        data = client.graphql(THREADS_QUERY, {"owner": owner, "name": name, "number": number, "after": after})
        pr = (data.get("repository") or {}).get("pullRequest")
        if pr is None:
            raise GraphQLError("pull request not found")
        block = pr["reviewThreads"]
        for node in block["nodes"]:
            comments = node["comments"]
            items = list(comments["nodes"])
            page = comments["pageInfo"]
            while page.get("hasNextPage"):
                more = client.graphql(THREAD_COMMENTS_QUERY, {"id": node["id"], "after": page["endCursor"]})
                chunk = (more.get("node") or {}).get("comments")
                if not chunk:
                    raise GraphQLError("thread comments not found")
                items.extend(chunk["nodes"])
                page = chunk["pageInfo"]
            thread = {k: v for k, v in node.items() if k != "comments"}
            thread["comments"] = items
            thread["comments_total_count"] = comments["totalCount"]
            threads.append(thread)
        if not block["pageInfo"].get("hasNextPage"):
            return threads
        after = block["pageInfo"]["endCursor"]
    raise GraphQLError("too many pages")


def read_closing(client, repo, number):
    owner, name = split_repo(repo)
    refs, after = [], None
    for _ in range(MAX_PAGES):
        data = client.graphql(CLOSING_QUERY, {"owner": owner, "name": name, "number": number, "after": after})
        pr = (data.get("repository") or {}).get("pullRequest")
        if pr is None:
            raise GraphQLError("pull request not found")
        block = pr["closingIssuesReferences"]
        refs.extend({"number": n["number"], "repository": (n.get("repository") or {}).get("nameWithOwner")} for n in block["nodes"])
        if not block["pageInfo"].get("hasNextPage"):
            return refs
        after = block["pageInfo"]["endCursor"]
    raise GraphQLError("too many pages")


def relations_from_timeline(timeline):
    """Referências cruzadas e conexões da linha do tempo (a linha do tempo inteira não é guardada)."""
    cross, connections = [], []
    for ev in timeline:
        kind = ev.get("event")
        if kind == "cross-referenced":
            issue = (ev.get("source") or {}).get("issue") or {}
            cross.append({"number": issue.get("number"), "repository": (issue.get("repository") or {}).get("full_name"),
                          "is_pull_request": "pull_request" in issue, "created_at": ev.get("created_at")})
        elif kind in ("connected", "disconnected"):
            connections.append({"event": kind, "id": ev.get("id"), "created_at": ev.get("created_at")})
    return cross, connections


def side(ref):
    """Base ou head do PR, sem o conteúdo do repositório (que pode ser nulo se o fork foi apagado)."""
    repo = ref.get("repo") or {}
    return {"repository": repo.get("full_name"), "ref": ref.get("ref"), "sha": ref.get("sha")}


def read_pull(client, repo, number, known_labels):
    """Lê um PR. Devolve (estado, dados, conferências, detalhes). ApiError 401/403 sobe (fatal)."""
    base = f"/repos/{repo}"
    checks, limits, detail = [], [], {}
    pr = client.get(f"{base}/pulls/{number}")
    commits = client.pages_list(f"{base}/pulls/{number}/commits")
    files = client.pages_list(f"{base}/pulls/{number}/files")
    reviews = client.pages_list(f"{base}/pulls/{number}/reviews")
    review_comments = client.pages_list(f"{base}/pulls/{number}/comments")
    comments = client.pages_list(f"{base}/issues/{number}/comments")
    events = client.pages_list(f"{base}/issues/{number}/events")
    timeline = client.pages_list(f"{base}/issues/{number}/timeline")

    # --- conferências (divergência = FAILED) ---
    if pr.get("commits", 0) > COMMITS_API_LIMIT:
        limits.append("COMMITS_ABOVE_API_LIMIT")
    else:
        checks.append(("commits_read_equal_commits", len(commits) == pr.get("commits")))
    if pr.get("changed_files", 0) > FILES_API_LIMIT:
        limits.append("FILES_ABOVE_API_LIMIT")
    else:
        checks.append(("files_read_equal_changed_files", len(files) == pr.get("changed_files")))
    checks.append(("review_comments_read_equal_count", len(review_comments) == pr.get("review_comments")))
    checks.append(("conversation_comments_read_equal_count", len(comments) == pr.get("comments")))
    for name, items in (("reviews", reviews), ("review_comments", review_comments), ("comments", comments), ("commits", commits)):
        key = "sha" if name == "commits" else "id"
        ids = [i.get(key) for i in items]
        checks.append((f"{name}_ids_unique", len(ids) == len(set(ids))))
    checks.append(("merged_consistent_with_merged_at", bool(pr.get("merged")) == bool(pr.get("merged_at"))))
    if known_labels is not None:
        checks.append(("labels_exist_in_labels_class", all(lb.get("name") in known_labels for lb in pr.get("labels", []))))

    # --- threads e fechamento (GraphQL): falha aqui limita o PR, não o derruba ---
    threads, closing = None, None
    try:
        threads = read_threads(client, repo, number)
    except (GraphQLError, ApiError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, ApiError) and exc.status in (401,):
            raise
        limits.append("THREADS_UNAVAILABLE")
    try:
        closing = read_closing(client, repo, number)
    except (GraphQLError, ApiError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, ApiError) and exc.status in (401,):
            raise
        limits.append("CLOSING_REFERENCES_UNAVAILABLE")
    thread_total = None
    if threads is not None:
        thread_total = sum(len(t["comments"]) for t in threads)
        detail["thread_comments_graphql"], detail["review_comments_rest"] = thread_total, len(review_comments)
        if thread_total != len(review_comments):
            limits.append("THREAD_TOTALS_DIFFER_FROM_REST")
    cross, connections = relations_from_timeline(timeline)
    relations = {"number": number, "base": side(pr.get("base") or {}), "head": side(pr.get("head") or {}),
                 "closing_issues": closing, "cross_references": cross, "connections": connections}
    detail["files_without_patch"] = sum(1 for f in files if "patch" not in f)
    detail["counts"] = {"commits": len(commits), "files": len(files), "reviews": len(reviews), "review_comments": len(review_comments),
                        "comments": len(comments), "events": len(events), "threads": len(threads or [])}
    detail["limits"] = limits
    data = {"pull": pr, "commits": {"number": number, "commits": commits}, "files": {"number": number, "files": files},
            "reviews": {"number": number, "reviews": reviews}, "review_comments": {"number": number, "review_comments": review_comments},
            "comments": {"number": number, "comments": comments}, "events": {"number": number, "events": events},
            "threads": {"number": number, "threads": threads, "available": threads is not None}, "relations": relations}
    return (LIMITED if limits else AVAILABLE), data, checks, detail


def read_pulls(client, repo, evidence, package, max_pulls, max_bytes):
    listed = client.pages_list(f"/repos/{repo}/pulls", {"state": "all", "sort": "created", "direction": "asc"})
    numbers = [p["number"] for p in listed]
    all_checks = {}

    def note(name, ok):
        all_checks[name] = all_checks.get(name, True) and ok

    note("pulls_numbers_unique", len(numbers) == len(set(numbers)))
    ordered = sorted(set(numbers))

    # Rótulos conhecidos (classe labels, já lida pelo passo anterior), para conferir os rótulos dos PRs.
    known_labels = None
    labels_file = package / "api-labels.json"
    if labels_file.exists():
        try:
            known_labels = {lb["name"] for lb in json.loads(labels_file.read_text())}
        except (ValueError, KeyError, TypeError):
            known_labels = None

    # Cruzamento com o leitor de issues: estrito só se a classe issues foi lida completa (OK) neste run.
    cross = {"mode": "informative", "reason": "issues status not found"}
    status_file = evidence / "api-status.json"
    if status_file.exists():
        try:
            issues = json.loads(status_file.read_text())["classes"]["issues"]
            skipped = sorted(issues.get("extra", {}).get("pull_requests_skipped", []))
            only_pulls = sorted(set(ordered) - set(skipped))
            only_issues = sorted(set(skipped) - set(ordered))
            if issues.get("status") == "OK":
                cross = {"mode": "strict", "only_in_pulls": only_pulls, "only_in_issues": only_issues}
                note("pulls_list_equals_issues_pull_requests", not only_pulls and not only_issues)
            else:
                cross = {"mode": "informative", "reason": f"issues class status {issues.get('status')}",
                         "only_in_pulls": only_pulls, "only_in_issues": only_issues}
        except (ValueError, KeyError, TypeError):
            cross = {"mode": "informative", "reason": "issues status unreadable"}

    refs = {}
    refs_file = evidence / "refs.tsv"
    if refs_file.exists():
        for line in refs_file.read_text(errors="replace").splitlines():
            parts = line.split("\t")
            m = re.fullmatch(r"refs/pull/(\d+)/head", parts[0]) if len(parts) >= 2 else None
            if m:
                refs[int(m.group(1))] = parts[1]

    out = {k: [] for k in ("pulls", "commits", "files", "reviews", "review_comments", "comments", "events", "threads", "relations")}
    records, used = [], 0
    totals = {k: 0 for k in ("commits", "files", "reviews", "review_comments", "comments", "events", "threads")}
    no_patch = 0
    ref_compare = {"available": bool(refs), "compared": 0, "equal": 0, "differ": 0}
    thread_diffs = []
    for i, number in enumerate(ordered):
        item = {"number": number}
        if i >= max_pulls or used >= max_bytes:
            item["state"] = SKIPPED_OVER_CAP
            records.append(item)
            continue
        try:
            state, data, checks, detail = read_pull(client, repo, number, known_labels)
        except ApiError as exc:
            if exc.status in (401, 403):
                raise
            item.update(state=FAILED_ITEM, status=exc.status)
            records.append(item)
            continue
        for name, ok in checks:
            note(name, ok)
        item["state"] = state
        item["limits"] = detail["limits"]
        item["counts"] = detail["counts"]
        if "thread_comments_graphql" in detail:
            item["thread_comments_graphql"], item["review_comments_rest"] = detail["thread_comments_graphql"], detail["review_comments_rest"]
            if detail["thread_comments_graphql"] != detail["review_comments_rest"]:
                thread_diffs.append({"graphql": detail["thread_comments_graphql"], "rest": detail["review_comments_rest"]})
        for key in totals:
            totals[key] += detail["counts"][key]
        no_patch += detail["files_without_patch"]
        out["pulls"].append(data["pull"])
        for key in ("commits", "files", "reviews", "review_comments", "comments", "events", "threads", "relations"):
            out[key].append(data[key])
        used += len(json.dumps(data))
        head_sha = (data["pull"].get("head") or {}).get("sha")
        if number in refs and head_sha:
            ref_compare["compared"] += 1
            ref_compare["equal" if refs[number] == head_sha else "differ"] += 1
        records.append(item)
    failed_items = [r for r in records if r["state"] == FAILED_ITEM]
    note("pull_requests_all_read", not failed_items)
    checks = [ar.check(name, ok, "" if ok else "see the inventory") for name, ok in sorted(all_checks.items())]
    counts = {}
    for r in records:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    limit_counts = {}
    for r in records:
        for lim in r.get("limits", []):
            limit_counts[lim] = limit_counts.get(lim, 0) + 1
    extra = {"pulls": {"listed": len(ordered), "read": sum(1 for r in records if r["state"] in (AVAILABLE, LIMITED))}, **totals,
             "files_without_patch": no_patch, "bytes_read": used, "max_pulls": max_pulls, "max_bytes": max_bytes,
             "state_counts": counts, "limit_counts": limit_counts, "thread_total_differences": thread_diffs,
             "issues_crosscheck": cross, "refs_crosscheck": ref_compare}
    files = {"api-pulls.json": out["pulls"], "api-pull-commits.json": out["commits"], "api-pull-files.json": out["files"],
             "api-pull-reviews.json": out["reviews"], "api-pull-review-comments.json": out["review_comments"],
             "api-pull-comments.json": out["comments"], "api-pull-events.json": out["events"],
             "api-pull-threads.json": out["threads"], "api-pull-relations.json": out["relations"]}
    return {"files": files, "checks": checks, "count": len(records), "extra": extra, "items": records}


def summary_line(status, extra):
    """Linha do log público: só contagens e tamanhos."""
    c = extra["state_counts"]
    gaps = ",".join(f"{k}={v}" for k, v in sorted(c.items()) if k in GAP_STATES) or "none"
    return (f"API_READ pull_requests: {status} prs={extra['pulls']['read']}/{extra['pulls']['listed']} commits={extra['commits']} "
            f"files={extra['files']} reviews={extra['reviews']} review_comments={extra['review_comments']} comments={extra['comments']} "
            f"events={extra['events']} threads={extra['threads']} bytes={extra['bytes_read']} gaps={gaps}")


def cmd_read():
    try:
        repo = os.environ["SOURCE_REPOSITORY"]
        token = os.environ["SOURCE_TOKEN"]
    except KeyError as exc:
        print(f"::error::Variável ausente: {exc.args[0]}")
        sys.exit(64)
    try:
        max_pulls = int(os.environ.get("PULLS_MAX", DEFAULT_MAX_PULLS))
        max_bytes = int(os.environ.get("PULLS_MAX_BYTES", DEFAULT_MAX_BYTES))
    except ValueError:
        print("::error::PULLS_MAX ou PULLS_MAX_BYTES inválida")
        sys.exit(64)
    evidence = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))
    package = pathlib.Path(os.environ.get("PACKAGE_DIR", "package"))
    evidence.mkdir(parents=True, exist_ok=True)
    package.mkdir(parents=True, exist_ok=True)
    client = PullsClient(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token)

    inventory = {"schema": "api-pulls-inventory/1", "source_repository": repo, "captured_at": now(),
                 "api_base": client.base, "limitations": LIMITATIONS}
    out = None
    try:
        out = read_pulls(client, repo, evidence, package, max_pulls, max_bytes)
    except ApiError as exc:
        hint = " (o token precisa de Pull requests: Read-only?)" if exc.status in (401, 403, 404) else ""
        status = {"status": "FAILED", "count": 0, "files": [], "checks": [], "detail": f"{exc}{hint}"}
        print(f"::error::API_READ pull_requests: FAILED: {exc}{hint}")
    except Exception as exc:  # noqa: BLE001 - rede de segurança: a causa vai só como tipo, sem conteúdo
        status = {"status": "FAILED", "count": 0, "files": [], "checks": [],
                  "detail": f"Unexpected error in the pull requests reader: {type(exc).__name__}"}
        print(f"::error::API_READ pull_requests: FAILED: {type(exc).__name__}")
    if out is not None:
        files = []
        for name, data in out["files"].items():
            raw = write_json(package / name, data)
            files.append({"file": name, "items": len(data), "sha256": sha256_bytes(raw), "bytes": len(raw)})
        failed = [c for c in out["checks"] if not c["ok"]]
        gaps = {k: v for k, v in out["extra"]["state_counts"].items() if k in GAP_STATES}
        if failed:
            st, detail = "FAILED", "Reconciliation check failed: " + ", ".join(c["check"] for c in failed)
        elif gaps:
            st = "PARTIAL"
            lims = ", ".join(f"{v} {k}" for k, v in sorted(out["extra"]["limit_counts"].items()))
            detail = ("Some pull requests are not fully in the package: " + ", ".join(f"{v} {k}" for k, v in sorted(gaps.items())) + "."
                      + (f" Limits: {lims}." if lims else ""))
        else:
            st, detail = "OK", ""
        status = {"status": st, "count": out["count"], "files": files, "checks": out["checks"], "extra": out["extra"],
                  **({"detail": detail} if detail else {})}
        inventory["items"] = out["items"]
        print(summary_line(st, out["extra"]))
    inventory["classes"] = {CLASS: status}
    write_json(package / "api-pulls-inventory.json", inventory)
    write_json(evidence / "api-pulls-status.json", inventory)


COMMANDS = {"read": cmd_read}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(64)
    COMMANDS[sys.argv[1]]()
