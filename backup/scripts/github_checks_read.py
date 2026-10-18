#!/usr/bin/env python3
"""Leitor da classe `checks` do BKP_REPO: check suites, check runs, anotações e commit statuses (somente leitura).

O QUE É
    Lê da API REST do GitHub os resultados de verificação ligados a COMMITS do repositório de origem:
    check suites, check runs (todos, não só os últimos), as anotações de cada check run e os commit
    statuses (lista e status combinado). É uma classe TEMPORAL: checks e statuses expiram pela retenção do
    repositório (BACKUP_REPOSITORY.md, II.12). Usa só a biblioteca padrão do Python e o cliente de
    `github_actions_read.py`.

POR QUE COMMITS CANDIDATOS
    A API não lista "todos os checks do repositório": eles ficam presos a commits. O leitor lê um conjunto
    CANDIDATO, sem varrer o histórico:
      - a ponta de cada branch e de cada tag (de evidence/refs.tsv, gerado pelo passo do mirror; tag
        anotada é resolvida pelo nome);
      - a ponta de cada refs/pull/*/head (do mesmo arquivo);
      - o head_sha de cada run lido pela classe `actions` (package/api-actions-runs.json).
    No máximo CHECKS_MAX_COMMITS (padrão 500) commits e CHECKS_MAX_BYTES (padrão 256 MiB) de JSON; as
    pontas de branches e tags e os runs vêm antes dos PRs. O que não couber fica SKIPPED_OVER_CAP.
    Commits fora do conjunto candidato NÃO são descobertos (limitação declarada, nunca "ausente").

COMMIT SEM DADOS
    Um commit sem nenhum check nem status fica NO_DATA_FOUND: a API não distingue "nunca teve" de
    "expirou pela retenção". Não é falha, e nunca é tratado como ausente nem como expirado.

CLASSE SEM RESTAURAÇÃO
    É REGISTRO: os arquivos preservam o que a API devolveu.

PERMISSÕES E ESCOLHA DO TOKEN (SOURCE_TOKEN = SOURCE_READ_TOKEN; CHECKS_APP_TOKEN = token do App Reader)
    - Commit statuses: Read-only (fine-grained). Sem ela, a classe é FAILED com a dica. Statuses, commits e o
      resto SEMPRE usam o SOURCE_TOKEN.
    - Check suites, check runs e anotações (as TRÊS rotas de checks): o token fine-grained NÃO tem permissão de
      Checks (só GitHub App). Se CHECKS_APP_TOKEN existir (token de instalação do GitHub App "Repository
      Preservation Reader", só com Checks: Read e só para o repositório de origem), essas três rotas usam ELE.
      Sem ele, usam o SOURCE_TOKEN: em repositório PÚBLICO a API libera as rotas sem a permissão; em
      repositório PRIVADO, 403/404 NÃO derrubam a classe: o commit vira CHECKS_DENIED, a classe fica PARTIAL e o
      cabeçalho X-Accepted-GitHub-Permissions das respostas negadas vai para o status de evidência.
    - SEM cascata: negado com um token, o commit vira CHECKS_DENIED; o outro token não é tentado.
    - CHECKS_APP_STATUS (resultado do passo que emite o token: success, failure, cancelled ou skipped) permite
      distinguir "App não configurado" (NOT_CONFIGURED) de "App falhou" (FAILED); a evidência registra o estado
      do token (ISSUED, NOT_CONFIGURED ou FAILED) e quantas requisições de checks usaram cada token.

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - A origem é somente leitura: só faz requisições GET.
    - Leitura incompleta nunca vira sucesso: contagens, referências e o status combinado são conferidos;
      divergência = FAILED.
    - O token nunca é impresso nem gravado.
    - O LOG PÚBLICO traz só contagens e tamanhos. Nenhum nome de check, contexto, app, branch, tag, título ou
      URL; as rotas saem sem ids, SHAs nem nomes de ref. Os dados (que podem trazer mensagens, caminhos e
      URLs de CI de terceiros com token) vão só ao pacote e à evidência (OneDrive).

COMO USAR (o workflow já faz isto)
    SOURCE_TOKEN=... SOURCE_REPOSITORY=dono/nome EVIDENCE_DIR=evidence PACKAGE_DIR=package \\
        python3 backup/scripts/github_checks_read.py read

VARIÁVEIS DE AMBIENTE
    SOURCE_TOKEN, SOURCE_REPOSITORY, EVIDENCE_DIR (padrão evidence), PACKAGE_DIR (padrão package),
    CHECKS_APP_TOKEN (opcional), CHECKS_APP_STATUS (opcional), CHECKS_MAX_COMMITS (padrão 500), CHECKS_MAX_BYTES (padrão 256 MiB), GITHUB_API_URL (teste local).

ARQUIVOS GERADOS
    package/api-checks-commits.json       commits candidatos: origem, estado e contagens
    package/api-checks-suites.json        check suites por commit
    package/api-checks-runs.json          check runs por commit
    package/api-checks-annotations.json   anotações por check run
    package/api-commit-statuses.json      statuses (lista) e status combinado por commit
    package/api-checks-inventory.json     contagens, SHA-256 de cada arquivo, conferências, limitações
    evidence/api-checks-status.json       resultado da classe (lido pelo build-evidence)

CÓDIGOS DE SAÍDA
    0   terminou (a classe tem OK, PARTIAL ou FAILED no api-checks-status.json)
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

DEFAULT_MAX_COMMITS = 500
DEFAULT_MAX_BYTES = 256 * 1024 ** 2
PER_PAGE = g.PER_PAGE
MAX_PAGES = g.MAX_PAGES

# Estados de um commit candidato.
AVAILABLE = "AVAILABLE"                      # há check ou status capturado
NO_DATA_FOUND = "NO_DATA_FOUND"              # nenhum dado: "nunca teve" e "expirou" não se distinguem
SKIPPED_OVER_CAP = "SKIPPED_OVER_CAP"        # não coube nos tetos
COMMIT_NOT_RESOLVABLE = "COMMIT_NOT_RESOLVABLE"  # a API não conhece o SHA (404/422)
CHECKS_DENIED = "CHECKS_DENIED"              # checks negados ao token (repositório privado)
FAILED_ITEM = "FAILED"                       # leitura falhou (a classe vira FAILED)
GAP_STATES = (SKIPPED_OVER_CAP, COMMIT_NOT_RESOLVABLE, CHECKS_DENIED)

# As três rotas de checks (suites, runs e anotações; as anotações ficam sob /check-runs/{id}/annotations).
CHECK_ROUTE = re.compile(r"/check-(?:suites|runs)(?:[/?]|$)")
# Estado do token do App Reader neste run.
APP_ISSUED, APP_NOT_CONFIGURED, APP_FAILED = "ISSUED", "NOT_CONFIGURED", "FAILED"

LIMITATIONS = [
    "Checks and commit statuses are preserved as a record (API JSON); they cannot be restored to GitHub.",
    "Only a candidate set of commits is read: branch and tag tips, refs/pull/*/head and the head commits of the workflow runs read by the actions class (at most CHECKS_MAX_COMMITS). Checks of any other commit are not discovered, and the merge commits of pull requests (refs/pull/*/merge) are not read.",
    "A commit with no check or status data is reported NO_DATA_FOUND: the API cannot tell a commit that never had any from one whose data expired under the repository retention. It is neither reported absent nor expired.",
    "Check suites, check runs and annotations of a private repository need a GitHub App with Checks: Read (the Repository Preservation Reader): they are read with its installation token when one is issued for the run, otherwise with the SOURCE_READ_TOKEN, which has no Checks permission (fine-grained tokens cannot have it) and reads them only for a public repository. Statuses and commits are always read with the SOURCE_READ_TOKEN (Commit statuses: Read-only).",
    "Retention is reported as OBSERVED (a manual check of the repository settings); the setting itself is not read through the API.",
]


def mask(route):
    """Rota sem ids, SHAs nem nomes de ref: '/commits/<ref>/check-runs' -> '/commits/{ref}/check-runs'."""
    route = re.sub(r"(/commits/)[^/?]+", r"\1{ref}", route)
    return re.sub(r"/\d+", "/{id}", route)


class ChecksClient(ar.Client):
    """Cliente do leitor de Actions com rotas mascaradas (inclusive SHAs) e captura do cabeçalho de permissão."""

    def __init__(self, base, token, app_token=None, app_state=None):
        super().__init__(base, token)
        self.denied_headers = set()
        self._last_headers = None
        self.app_token = app_token or None
        self.app_state = app_state or (APP_ISSUED if self.app_token else APP_NOT_CONFIGURED)
        # Só nas rotas de checks: quantas requisições (e quantas negações) usaram cada token.
        self.check_requests = {"app": 0, "source": 0}
        self.check_denied = {"app": 0, "source": 0}
        self._last_kind = "source"

    def _kind(self, route):
        """Token da rota: o do App nas três rotas de checks, se houver; o SOURCE_READ_TOKEN em todo o resto."""
        return "app" if (self.app_token and CHECK_ROUTE.search(route)) else "source"

    def _open(self, url, route):
        kind = self._kind(route)
        self._last_kind = kind
        if CHECK_ROUTE.search(route):
            self.check_requests[kind] += 1

        def fn():
            headers = self._headers()
            if kind == "app":
                headers["Authorization"] = f"Bearer {self.app_token}"
            request = urllib.request.Request(url, method="GET", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8")), response.headers
            except urllib.error.HTTPError as exc:
                self._last_headers = exc.headers
                raise
        return self._call(mask(route), fn)

    def note_denied(self):
        """Guarda o X-Accepted-GitHub-Permissions da última resposta negada (documenta a permissão exigida)."""
        value = self._last_headers.get("X-Accepted-GitHub-Permissions") if self._last_headers is not None else None
        self.denied_headers.add(value if value else "(header not present)")
        self.check_denied[self._last_kind] += 1

    def token_info(self):
        """Estado do token do App e uso de cada token nas rotas de checks (só contagens)."""
        used = [k for k, v in self.check_requests.items() if v]
        route_token = "none" if not used else (used[0] if len(used) == 1 else "mixed")
        return {"app_token": self.app_state, "route_token": route_token,
                "check_requests": dict(self.check_requests), "check_denied": dict(self.check_denied)}

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

    def pages_obj(self, route, key, params=None):
        query = dict(params or {})
        query["per_page"] = PER_PAGE
        url = self.base + route + "?" + urllib.parse.urlencode(query)
        items, total = [], None
        for _ in range(MAX_PAGES):
            body, headers = self._open(url, route)
            if not isinstance(body, dict) or not isinstance(body.get(key), list):
                raise ApiError(0, mask(route), "unexpected response: expected an object with a list")
            items.extend(body[key])
            total = body.get("total_count", total)
            url = self._next_link(headers.get("Link"))
            if not url:
                return items, total
        raise ApiError(0, mask(route), f"more than {MAX_PAGES} pages")

    def combined_status(self, route):
        """Status combinado de um commit: (state, total_count, statuses). Segue a paginação das statuses."""
        query = urllib.parse.urlencode({"per_page": PER_PAGE})
        url = self.base + route + "?" + query
        state = total = None
        statuses = []
        for _ in range(MAX_PAGES):
            body, headers = self._open(url, route)
            if not isinstance(body, dict) or not isinstance(body.get("statuses"), list):
                raise ApiError(0, mask(route), "unexpected response: expected a combined status object")
            state, total = body.get("state"), body.get("total_count", total)
            statuses.extend(body["statuses"])
            url = self._next_link(headers.get("Link"))
            if not url:
                return state, total, statuses
        raise ApiError(0, mask(route), f"more than {MAX_PAGES} pages")


def combined_state(statuses):
    """Estado combinado esperado a partir da lista de statuses (o mais novo de cada contexto vem primeiro)."""
    latest = {}
    for s in statuses:
        latest.setdefault(s.get("context"), s.get("state"))
    states = set(latest.values())
    if not latest:
        return "pending", 0
    if states & {"failure", "error"}:
        return "failure", len(latest)
    if "pending" in states:
        return "pending", len(latest)
    return "success", len(latest)


def candidates_from_files(evidence, package):
    """Commits candidatos (sem resolver tags anotadas). Devolve (lista, notas)."""
    notes, found = [], []
    refs = evidence / "refs.tsv"
    if refs.exists():
        for line in refs.read_text(errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            ref, sha = parts[0], parts[1]
            kind_type = parts[2] if len(parts) > 2 else "commit"
            if ref.startswith("refs/heads/"):
                found.append({"sha": sha, "kind": "branch", "type": kind_type, "name": ref[len("refs/heads/"):]})
            elif ref.startswith("refs/tags/"):
                found.append({"sha": sha, "kind": "tag", "type": kind_type, "name": ref[len("refs/tags/"):]})
            elif re.fullmatch(r"refs/pull/\d+/head", ref):
                found.append({"sha": sha, "kind": "pull", "type": kind_type, "name": ref})
    else:
        notes.append("evidence/refs.tsv not found: refs were not used as candidates")
    runs_file = package / "api-actions-runs.json"
    if runs_file.exists():
        try:
            for run in json.loads(runs_file.read_text()):
                if run.get("head_sha"):
                    found.append({"sha": run["head_sha"], "kind": "run", "type": "commit", "name": None})
        except (ValueError, TypeError):
            notes.append("package/api-actions-runs.json could not be parsed: run commits were not used as candidates")
    else:
        notes.append("package/api-actions-runs.json not found (actions class not read): run commits were not used as candidates")
    return found, notes


def resolve(client, repo, found):
    """Resolve tags anotadas para o SHA do commit e junta as origens de cada SHA. Devolve dict sha -> registro."""
    out, unresolved = {}, []
    for c in found:
        sha = c["sha"]
        if c["type"] == "tag":                       # objeto de tag: o SHA do commit sai da rota do commit pelo nome
            try:
                sha = client.get(f"/repos/{repo}/commits/{urllib.parse.quote(c['name'], safe='')}")["sha"]
            except ApiError as exc:
                if exc.status in (401, 403):
                    raise
                unresolved.append(c)
                continue
        elif c["type"] != "commit":
            continue
        rec = out.setdefault(sha, {"sha": sha, "sources": set()})
        rec["sources"].add(c["kind"])
    return out, unresolved


def earliest(*times):
    times = [t for t in times if t]
    return min(times) if times else None


def read_commit(client, repo, sha):
    """Lê um commit. Devolve (estado, dados, conferências, detalhes). ApiError 401/403 de statuses sobe (fatal)."""
    checks = []
    data = {"sha": sha}
    # 1) statuses: validam o SHA e usam a permissão Commit statuses.
    try:
        statuses = client.pages_list(f"/repos/{repo}/commits/{sha}/statuses")
        cstate, ctotal, cstatuses = client.combined_status(f"/repos/{repo}/commits/{sha}/status")
    except ApiError as exc:
        if exc.status in (401, 403):
            raise
        if exc.status in (404, 422):
            return COMMIT_NOT_RESOLVABLE, None, [], {"status": exc.status}
        return FAILED_ITEM, None, [], {"status": exc.status}
    expected, distinct = combined_state(statuses)
    ids = [s.get("id") for s in statuses]
    checks.append(("statuses_ids_unique", len(ids) == len(set(ids))))
    checks.append(("combined_status_matches_statuses",
                   (not statuses and ctotal in (0, None)) or (cstate == expected and ctotal == distinct)))
    data["statuses"] = {"statuses": statuses, "combined": {"state": cstate, "total_count": ctotal, "statuses": cstatuses}}
    # 2) check suites, check runs e anotações.
    suites = runs = None
    annotations = []
    denied = False
    try:
        suites, stotal = client.pages_obj(f"/repos/{repo}/commits/{sha}/check-suites", "check_suites")
        runs, rtotal = client.pages_obj(f"/repos/{repo}/commits/{sha}/check-runs", "check_runs", {"filter": "all"})
        checks.append(("check_suites_read_equal_total_count", stotal is None or len(suites) == stotal))
        checks.append(("check_runs_read_equal_total_count", rtotal is None or len(runs) == rtotal))
        rids = [r.get("id") for r in runs]
        checks.append(("check_runs_ids_unique", len(rids) == len(set(rids))))
        suite_ids = {s.get("id") for s in suites}
        checks.append(("check_runs_belong_to_listed_suites", all((r.get("check_suite") or {}).get("id") in suite_ids for r in runs)))
        for run in runs:
            if (run.get("output") or {}).get("annotations_count", run.get("annotations_count", 0)):
                got = client.pages_list(f"/repos/{repo}/check-runs/{run['id']}/annotations")
                expected_n = (run.get("output") or {}).get("annotations_count", 0)
                checks.append(("annotations_read_equal_annotations_count", len(got) == expected_n))
                annotations.append({"sha": sha, "check_run_id": run["id"], "annotations": got})
    except ApiError as exc:
        if exc.status == 401:
            raise
        if exc.status in (403, 404):
            denied = True
            client.note_denied()
            suites = runs = None
            annotations = []
        else:
            return FAILED_ITEM, None, [], {"status": exc.status}
    data["suites"] = {"sha": sha, "check_suites": suites or []}
    data["runs"] = {"sha": sha, "check_runs": runs or []}
    data["annotations"] = annotations
    counts = {"check_suites": len(suites or []), "check_runs": len(runs or []), "annotations": sum(len(a["annotations"]) for a in annotations),
              "statuses": len(statuses)}
    created = earliest(*[s.get("created_at") for s in suites or []], *[r.get("started_at") for r in runs or []],
                       *[s.get("created_at") for s in statuses])
    if denied:
        state = CHECKS_DENIED
    elif counts["check_suites"] or counts["check_runs"] or counts["statuses"]:
        state = AVAILABLE
    else:
        state = NO_DATA_FOUND
    return state, data, checks, {"counts": counts, "oldest_created_at": created}


def read_checks(client, repo, evidence, package, max_commits, max_bytes):
    found, notes = candidates_from_files(evidence, package)
    cands, unresolved = resolve(client, repo, found)
    priority = lambda rec: (0 if rec["sources"] - {"pull"} else 1, rec["sha"])
    ordered = sorted(cands.values(), key=priority)
    all_checks, records = {}, []
    out = {"suites": [], "runs": [], "annotations": [], "statuses": []}
    used = 0
    totals = {"check_suites": 0, "check_runs": 0, "annotations": 0, "statuses": 0}
    times, github_actions_runs = [], []
    for i, rec in enumerate(ordered):
        item = {"sha": rec["sha"], "sources": sorted(rec["sources"])}
        if i >= max_commits or used >= max_bytes:
            item["state"] = SKIPPED_OVER_CAP
            records.append(item)
            continue
        state, data, checks, detail = read_commit(client, repo, rec["sha"])
        item["state"] = state
        item.update({k: v for k, v in detail.items() if k in ("status", "counts")})
        for name, ok in checks:
            all_checks.setdefault(name, True)
            all_checks[name] = all_checks[name] and ok
        if data is not None:
            out["suites"].append(data["suites"])
            out["runs"].append(data["runs"])
            out["annotations"].extend(data["annotations"])
            out["statuses"].append({"sha": rec["sha"], **data["statuses"]})
            used += len(json.dumps(data))
            for k, v in detail["counts"].items():
                totals[k] += v
            if detail.get("oldest_created_at") and state == AVAILABLE:
                times.append(detail["oldest_created_at"])
            github_actions_runs += [r["id"] for r in data["runs"]["check_runs"] if (r.get("app") or {}).get("slug") == "github-actions"]
        records.append(item)
    for c in unresolved:
        records.append({"sha": None, "sources": [c["kind"]], "state": COMMIT_NOT_RESOLVABLE})
    checks = [ar.check(name, ok, "" if ok else "see the inventory") for name, ok in sorted(all_checks.items())]
    failed_items = [r for r in records if r["state"] == FAILED_ITEM]
    checks.append(ar.check("candidate_commits_all_read", not failed_items, f"{len(failed_items)} commit(s) could not be read"))
    # Cruzamento informativo com a classe actions (não reprova): o id do check run de um job do Actions é o id do job.
    cross = {"available": False}
    attempts = package / "api-actions-attempts.json"
    if attempts.exists():
        try:
            job_ids = {j["id"] for a in json.loads(attempts.read_text()) for j in a.get("jobs", [])}
            cross = {"available": True, "github_actions_check_runs": len(github_actions_runs),
                     "found_in_actions_jobs": sum(1 for rid in github_actions_runs if rid in job_ids)}
        except (ValueError, TypeError, KeyError):
            pass
    counts = {}
    for r in records:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    extra = {
        "commits": {"candidates": len(records), "read": sum(1 for r in records if r["state"] in (AVAILABLE, NO_DATA_FOUND, CHECKS_DENIED))},
        **totals, "bytes_read": used, "max_commits": max_commits, "max_bytes": max_bytes,
        "temporal_counts": counts, "oldest_available_created_at": min(times) if times else None,
        "denied_permission_headers": sorted(client.denied_headers), "checks_token": client.token_info(),
        "actions_crosscheck": cross, "notes": notes,
    }
    files = {"api-checks-commits.json": records, "api-checks-suites.json": out["suites"], "api-checks-runs.json": out["runs"],
             "api-checks-annotations.json": out["annotations"], "api-commit-statuses.json": out["statuses"]}
    return {"files": files, "checks": checks, "count": len(records), "extra": extra, "items": records}


def summary_line(status, extra):
    """Linha do log público: só contagens e tamanhos."""
    c = extra["temporal_counts"]
    gaps = ",".join(f"{k}={v}" for k, v in sorted(c.items()) if k in GAP_STATES) or "none"
    return (f"API_READ checks: {status} commits={extra['commits']['read']}/{extra['commits']['candidates']} "
            f"suites={extra['check_suites']} runs={extra['check_runs']} annotations={extra['annotations']} "
            f"statuses={extra['statuses']} no_data={c.get(NO_DATA_FOUND, 0)} bytes={extra['bytes_read']} gaps={gaps} "
            f"token={extra['checks_token']['route_token']}")


def denied_hint(info):
    """Texto do que fazer quando commits ficaram CHECKS_DENIED, conforme o token que as rotas de checks usaram."""
    if info["check_denied"]["app"]:
        return (" The GitHub App token was denied on the check routes: confirm that the Repository Preservation Reader "
                "is installed on this repository and has Checks: Read.")
    if info["app_token"] == APP_FAILED:
        return (" Checks of a private repository need a GitHub App with Checks: Read; the Repository Preservation Reader "
                "token could not be issued for this run (step 'Issue checks-reader token').")
    return (" Checks of a private repository need a GitHub App with Checks: Read; the Repository Preservation Reader token "
            "is not configured (variable REPOSITORY_PRESERVATION_READER_CLIENT_ID).")


def cmd_read():
    try:
        repo = os.environ["SOURCE_REPOSITORY"]
        token = os.environ["SOURCE_TOKEN"]
    except KeyError as exc:
        print(f"::error::Variável ausente: {exc.args[0]}")
        sys.exit(64)
    try:
        max_commits = int(os.environ.get("CHECKS_MAX_COMMITS", DEFAULT_MAX_COMMITS))
        max_bytes = int(os.environ.get("CHECKS_MAX_BYTES", DEFAULT_MAX_BYTES))
    except ValueError:
        print("::error::CHECKS_MAX_COMMITS ou CHECKS_MAX_BYTES inválida")
        sys.exit(64)
    evidence = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))
    package = pathlib.Path(os.environ.get("PACKAGE_DIR", "package"))
    evidence.mkdir(parents=True, exist_ok=True)
    package.mkdir(parents=True, exist_ok=True)
    app_token = os.environ.get("CHECKS_APP_TOKEN", "").strip()
    app_status = os.environ.get("CHECKS_APP_STATUS", "").strip().lower()
    if app_token:
        app_state = APP_ISSUED
    elif app_status in ("success", "failure", "cancelled"):
        app_state = APP_FAILED          # o passo rodou e não entregou token
    else:
        app_state = APP_NOT_CONFIGURED  # passo pulado (variável ausente) ou fora do workflow
    client = ChecksClient(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token, app_token, app_state)

    inventory = {"schema": "api-checks-inventory/1", "source_repository": repo, "captured_at": now(),
                 "api_base": client.base, "limitations": LIMITATIONS}
    out = None
    try:
        out = read_checks(client, repo, evidence, package, max_commits, max_bytes)
    except ApiError as exc:
        hint = " (o token precisa de Commit statuses: Read-only?)" if exc.status in (401, 403) else ""
        if exc.status == 401 and client._last_kind == "app":
            hint = " (the GitHub App token was rejected on a check route)"
        status = {"status": "FAILED", "count": 0, "files": [], "checks": [], "detail": f"{exc}{hint}"}
        print(f"::error::API_READ checks: FAILED: {exc}{hint}")
    except Exception as exc:  # noqa: BLE001 - rede de segurança: a causa vai só como tipo, sem conteúdo
        status = {"status": "FAILED", "count": 0, "files": [], "checks": [],
                  "detail": f"Unexpected error in the checks reader: {type(exc).__name__}"}
        print(f"::error::API_READ checks: FAILED: {type(exc).__name__}")
    if out is not None:
        files = []
        for name, data in out["files"].items():
            raw = write_json(package / name, data)
            files.append({"file": name, "items": len(data), "sha256": sha256_bytes(raw), "bytes": len(raw)})
        failed = [c for c in out["checks"] if not c["ok"]]
        gaps = {k: v for k, v in out["extra"]["temporal_counts"].items() if k in GAP_STATES}
        if failed:
            st, detail = "FAILED", "Reconciliation check failed: " + ", ".join(c["check"] for c in failed)
        elif gaps:
            st = "PARTIAL"
            detail = "Some commits are not fully in the package: " + ", ".join(f"{v} {k}" for k, v in sorted(gaps.items())) + "."
            if gaps.get(CHECKS_DENIED):
                detail += denied_hint(out["extra"]["checks_token"])
        else:
            st, detail = "OK", ""
        status = {"status": st, "count": out["count"], "files": files, "checks": out["checks"], "extra": out["extra"],
                  **({"detail": detail} if detail else {})}
        inventory["items"] = out["items"]
        print(summary_line(st, out["extra"]))
    inventory["classes"] = {"checks": status}
    write_json(package / "api-checks-inventory.json", inventory)
    write_json(evidence / "api-checks-status.json", inventory)


COMMANDS = {"read": cmd_read}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(64)
    COMMANDS[sys.argv[1]]()
