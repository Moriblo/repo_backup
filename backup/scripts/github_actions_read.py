#!/usr/bin/env python3
"""Leitor da classe `actions` do BKP_REPO: workflows, runs, jobs, logs e artifacts (somente leitura).

O QUE É
    Lê da API REST do GitHub o que o GitHub Actions guardou do repositório de origem e grava no pacote
    do backup: os metadados (workflows, runs, tentativas, jobs e passos), o ZIP de logs de cada
    tentativa e o conteúdo (ZIP) de cada artifact. É uma classe TEMPORAL: logs e artifacts expiram
    (retenção do repositório), então ela vem antes das classes estáveis (BACKUP_REPOSITORY.md, II.12).
    Usa só a biblioteca padrão do Python e o cliente de `github_api_read.py`.

ONDE SE ENCAIXA NO FLUXO
    Roda no workflow `backup-repository.yml`, depois do passo das classes labels/milestones/issues e
    ANTES do passo "Build restorable package". Quem decide a disposição final da classe é
    `bkp_repo.py build-evidence`, a partir do `api-actions-status.json` que este script grava.

CLASSE SEM RESTAURAÇÃO
    É REGISTRO: os arquivos preservam o que a API devolveu; não existe restauração de execuções,
    logs ou artifacts pelo GitHub.

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - A origem é somente leitura: só faz requisições GET.
    - Leitura incompleta nunca vira sucesso: cada contagem, cada ZIP e cada hash é conferido.
      Divergência = FAILED. O que já tinha expirado antes da captura NÃO é falha: é registrado como
      EXPIRED / DELETED_UNAVAILABLE (nunca tratado como "ausente") e a classe fica PARTIAL.
    - O token nunca é impresso nem gravado, e NÃO é enviado ao domínio de armazenamento para o qual a
      API redireciona o download de logs e artifacts (a URL assinada basta).
    - O LOG PÚBLICO do executor traz só contagens e tamanhos. Nunca nomes de workflow, job, passo,
      branch de run ou título de commit, nem conteúdo de log. As rotas aparecem sem ids.
    - Tamanho: no máximo ACTIONS_MAX_BYTES (padrão 5 GiB) de logs e artifacts. Os mais antigos vêm
      primeiro (vencem primeiro); o que não couber fica listado como SKIPPED_OVER_CAP (PARTIAL).

COMO USAR (o workflow já faz isto)
    SOURCE_TOKEN=... SOURCE_REPOSITORY=dono/nome EVIDENCE_DIR=evidence PACKAGE_DIR=package \\
        python3 backup/scripts/github_actions_read.py read

VARIÁVEIS DE AMBIENTE
    SOURCE_TOKEN        token de LEITURA da origem (o mesmo SOURCE_READ_TOKEN). Precisa de
                        Actions: Read (além de Contents, Metadata e Issues já usados).
    SOURCE_REPOSITORY   dono/nome do repositório de origem.
    EVIDENCE_DIR        pasta de evidências (padrão: evidence). Recebe api-actions-status.json.
    PACKAGE_DIR         pasta do pacote (padrão: package). Recebe api-actions-*.json e os .tar.
    ACTIONS_MAX_BYTES   teto, em bytes, de logs + artifacts baixados (padrão: 5 GiB).
    GITHUB_API_URL      base da API (padrão: https://api.github.com), para o teste local.

ARQUIVOS GERADOS
    package/api-actions-workflows.json   workflows (id, nome, caminho, estado)
    package/api-actions-runs.json        runs, como a API devolve
    package/api-actions-attempts.json    cada tentativa de cada run, com jobs e passos
    package/api-actions-artifacts.json   lista de artifacts (com expired/expires_at)
    package/actions-logs.tar             ZIP de logs por tentativa (só se houve algum disponível)
    package/actions-artifacts.tar        ZIP de cada artifact disponível (idem)
    package/api-actions-inventory.json   contagens, estados temporais, SHA-256 de tudo, conferências
    evidence/api-actions-status.json     resultado da classe (lido pelo build-evidence)

CÓDIGOS DE SAÍDA
    0   terminou (a classe tem OK, PARTIAL ou FAILED no api-actions-status.json)
    64  uso incorreto ou variável de ambiente ausente
"""
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import github_api_read as g
from github_api_read import ApiError, now, sha256_bytes, write_json

# Teto padrão de logs + artifacts baixados.
DEFAULT_MAX_BYTES = 5 * 1024 ** 3
# Bloco de leitura ao gravar um download em disco.
BLOCK = 1 << 20
# Tentativas e espera: as mesmas do leitor de issues.
MAX_TRIES = g.MAX_TRIES
MAX_PAGES = g.MAX_PAGES
PER_PAGE = g.PER_PAGE

# Espera como variável, para o teste trocá-la.
sleep = time.sleep

# Estados de um item (log ou artifact). Os quatro primeiros seguem o vocabulário temporal do registro.
AVAILABLE = "AVAILABLE"
EXPIRED = "EXPIRED"
DELETED_UNAVAILABLE = "DELETED_UNAVAILABLE"
NOT_COMPLETED = "NOT_COMPLETED"          # run ainda em andamento: o log ainda não está completo
SKIPPED_OVER_CAP = "SKIPPED_OVER_CAP"    # não coube no teto de tamanho
FAILED_ITEM = "FAILED"                   # disponível, mas a leitura falhou (a classe vira FAILED)
GAP_STATES = (EXPIRED, DELETED_UNAVAILABLE, NOT_COMPLETED, SKIPPED_OVER_CAP)

LIMITATIONS = [
    "Actions is preserved as a record (API JSON, run log ZIPs and artifact ZIPs): runs, logs and artifacts cannot be restored to GitHub.",
    "Logs and artifacts that had already expired (or were deleted) before the capture are recorded as EXPIRED / DELETED_UNAVAILABLE; their content cannot be recovered.",
    "Commit statuses belong to the `checks` object class and are not read by this reader.",
    "Retention is reported as OBSERVED (artifact expires_at, or a manual check of the repository settings); the setting itself is not read through the API.",
    "At most ACTIONS_MAX_BYTES of logs and artifacts are downloaded; the oldest are taken first, the rest is listed as SKIPPED_OVER_CAP.",
]


def mask(route):
    """Rota sem ids numéricos, para mensagens e log públicos ('/actions/runs/123/logs' -> '/actions/runs/{id}/logs')."""
    return re.sub(r"/\d+", "/{id}", route)


class OverCap(Exception):
    """O download passaria do teto de tamanho."""


class Client(g.Client):
    """Cliente do leitor de issues, com rotas mascaradas, listas com total_count e download em disco."""

    def _open(self, url, route):
        try:
            return super()._open(url, route)
        except ApiError as exc:
            raise ApiError(exc.status, mask(exc.route), exc.detail) from None

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "repo-backup-bkp-repo"}

    def pages_obj(self, route, key, params=None):
        """Lista paginada cujo corpo é um objeto {total_count, <key>: [...]}. Devolve (itens, total_count)."""
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

    def _call(self, route, fn):
        """Executa fn() com a mesma política de repetição do leitor de issues."""
        for attempt in range(1, MAX_TRIES + 1):
            try:
                return fn()
            except urllib.error.HTTPError as exc:
                headers = exc.headers
                if exc.code in (403, 429) and (headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0"):
                    wait = self._rate_wait(headers)
                    if wait > g.MAX_RATE_WAIT or attempt == MAX_TRIES:
                        raise ApiError(exc.code, mask(route), "API rate limit exhausted") from None
                    sleep(wait)
                    continue
                if exc.code >= 500 and attempt < MAX_TRIES:
                    sleep(2 * attempt)
                    continue
                raise ApiError(exc.code, mask(route)) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == MAX_TRIES:
                    raise ApiError(0, mask(route), f"network failure: {type(exc).__name__}") from None
                sleep(2 * attempt)
        raise ApiError(0, mask(route), "attempts exhausted")

    def download(self, route, dest, limit):
        """Baixa um recurso binário (log ou artifact) para `dest`. Devolve (bytes, sha256).

        A API responde 302 com a URL assinada do armazenamento: o token NÃO é enviado a esse domínio.
        `limit` = máximo de bytes aceito; passou disso, apaga o arquivo e levanta OverCap."""
        def first():
            request = urllib.request.Request(self.base + route, method="GET", headers=self._headers())
            try:
                return _NO_REDIRECT.open(request, timeout=60)
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                    return exc.headers["Location"]
                raise

        target = self._call(route, first)

        def second():
            if isinstance(target, str):
                # URL assinada: sem cabeçalho de autorização.
                response = urllib.request.urlopen(urllib.request.Request(target, method="GET",
                                                  headers={"User-Agent": "repo-backup-bkp-repo"}), timeout=120)
            else:
                response = target                 # servidor que responde direto (teste local)
            digest, size = hashlib.sha256(), 0
            try:
                with response, open(dest, "wb") as out:
                    for block in iter(lambda: response.read(BLOCK), b""):
                        size += len(block)
                        if size > limit:
                            raise OverCap()
                        digest.update(block)
                        out.write(block)
            except OverCap:
                pathlib.Path(dest).unlink(missing_ok=True)
                raise
            return size, digest.hexdigest()

        # `target` de resposta direta só pode ser lido uma vez: sem repetição nesse caso.
        if isinstance(target, str):
            return self._call(route, second)
        return second()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Não segue redirecionamentos: o redirecionamento do download é tratado à mão (sem o token)."""

    def redirect_request(self, *args, **kwargs):
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def parse_time(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def check(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), **({"detail": detail} if detail else {})}


def zip_ok(path):
    """True se o arquivo é um ZIP íntegro."""
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None
    except (zipfile.BadZipFile, OSError):
        return False


def classify_gone(status):
    """Estado temporal de um recurso que a API não entrega: 410 expirou; 404 apagado/indisponível."""
    return EXPIRED if status == 410 else DELETED_UNAVAILABLE


class Budget:
    """Controla o teto de bytes baixados."""

    def __init__(self, max_bytes):
        self.max_bytes = max_bytes
        self.used = 0

    def left(self):
        return self.max_bytes - self.used


def read_actions(client, repo, work, max_bytes):
    """Lê a classe e devolve {files (dados JSON), tars, checks, count, extra}. ApiError 401/403 sobe (fatal)."""
    budget = Budget(max_bytes)
    checks = []
    workflows, wf_total = client.pages_obj(f"/repos/{repo}/actions/workflows", "workflows")
    checks.append(check("workflows_read_equal_total_count", wf_total is None or len(workflows) == wf_total,
                        f"read {len(workflows)} of {wf_total}"))
    runs, run_total = client.pages_obj(f"/repos/{repo}/actions/runs", "workflow_runs")
    ids = [r["id"] for r in runs]
    checks.append(check("runs_ids_unique", len(ids) == len(set(ids)), "duplicate run ids (the list changed while it was being read?)"))
    checks.append(check("runs_read_equal_total_count", run_total is None or len(runs) == run_total,
                        f"read {len(runs)} of {run_total}"))
    # Os mais antigos primeiro: vencem primeiro, e o teto de tamanho corta os mais novos.
    runs.sort(key=lambda r: (r.get("created_at", ""), r["id"]))

    logs_tar = tarfile.open(work / "actions-logs.tar", "w")
    arts_tar = tarfile.open(work / "actions-artifacts.tar", "w")
    attempts_out, log_items, artifacts_out, art_items = [], [], [], []
    jobs_total = 0
    attempts_total = 0
    log_members = art_members = 0
    jobs_ok = attempts_ok = per_run_arts_ok = True
    per_run_art_count = 0
    scratch = work / "scratch.bin"
    try:
        for run in runs:
            rid, latest = run["id"], int(run.get("run_attempt", 1) or 1)
            base = f"/repos/{repo}/actions/runs/{rid}"
            # --- tentativas, jobs e passos ---
            for n in range(1, latest + 1):
                try:
                    att = client.get(f"{base}/attempts/{n}")
                    jobs, jtotal = client.pages_obj(f"{base}/attempts/{n}/jobs", "jobs")
                except ApiError as exc:
                    if exc.status in (401, 403):
                        raise
                    attempts_ok = False
                    continue
                attempts_total += 1
                jobs_total += len(jobs)
                if jtotal is not None and len(jobs) != jtotal:
                    jobs_ok = False
                attempts_out.append({"run_id": rid, "run_attempt": n, "run": att, "jobs": jobs})
                # --- log da tentativa ---
                item = {"run_id": rid, "run_attempt": n, "run_created_at": run.get("created_at")}
                if att.get("status") != "completed":
                    item["state"] = NOT_COMPLETED
                else:
                    try:
                        size, digest = client.download(f"{base}/attempts/{n}/logs", scratch, budget.left())
                    except OverCap:
                        item["state"] = SKIPPED_OVER_CAP
                    except ApiError as exc:
                        if exc.status in (401, 403):
                            raise
                        item["state"] = classify_gone(exc.status) if exc.status in (404, 410) else FAILED_ITEM
                        if item["state"] == FAILED_ITEM:
                            item["status"] = exc.status
                    else:
                        if not zip_ok(scratch):
                            item.update(state=FAILED_ITEM, reason="log archive is not a valid ZIP")
                            scratch.unlink(missing_ok=True)
                        else:
                            member = f"logs/run-{rid}-attempt-{n}.zip"
                            logs_tar.add(scratch, arcname=member)
                            scratch.unlink(missing_ok=True)
                            budget.used += size
                            log_members += 1
                            item.update(state=AVAILABLE, bytes=size, sha256=digest, member=member)
                log_items.append(item)
            # --- artifacts do run ---
            try:
                arts, atotal = client.pages_obj(f"{base}/artifacts", "artifacts")
            except ApiError as exc:
                if exc.status in (401, 403):
                    raise
                per_run_arts_ok = False
                continue
            if atotal is not None and len(arts) != atotal:
                per_run_arts_ok = False
            per_run_art_count += len(arts)
            for art in arts:
                artifacts_out.append(art)
                item = {"artifact_id": art["id"], "run_id": rid, "name": art.get("name"), "created_at": art.get("created_at"),
                        "expires_at": art.get("expires_at"), "size_in_bytes": art.get("size_in_bytes")}
                if art.get("expired"):
                    item["state"] = EXPIRED
                elif (art.get("size_in_bytes") or 0) > budget.left():
                    item["state"] = SKIPPED_OVER_CAP
                else:
                    try:
                        size, digest = client.download(f"/repos/{repo}/actions/artifacts/{art['id']}/zip", scratch, budget.left())
                    except OverCap:
                        item["state"] = SKIPPED_OVER_CAP
                    except ApiError as exc:
                        if exc.status in (401, 403):
                            raise
                        item["state"] = classify_gone(exc.status) if exc.status in (404, 410) else FAILED_ITEM
                        if item["state"] == FAILED_ITEM:
                            item["status"] = exc.status
                    else:
                        problems = []
                        if size != art.get("size_in_bytes"):
                            problems.append("downloaded size differs from size_in_bytes")
                        if not zip_ok(scratch):
                            problems.append("artifact archive is not a valid ZIP")
                        declared = str(art.get("digest") or "")
                        if declared.startswith("sha256:") and declared[7:] != digest:
                            problems.append("sha256 differs from the digest reported by the API")
                        if problems:
                            item.update(state=FAILED_ITEM, reason="; ".join(problems))
                            scratch.unlink(missing_ok=True)
                        else:
                            member = f"artifacts/{art['id']}.zip"
                            arts_tar.add(scratch, arcname=member)
                            scratch.unlink(missing_ok=True)
                            budget.used += size
                            art_members += 1
                            item.update(state=AVAILABLE, bytes=size, sha256=digest, member=member,
                                        digest_verified=declared.startswith("sha256:"))
                art_items.append(item)
    finally:
        logs_tar.close()
        arts_tar.close()
        scratch.unlink(missing_ok=True)

    # --- conferências globais ---
    checks.append(check("attempts_all_read", attempts_ok, "an attempt could not be read"))
    checks.append(check("jobs_read_equal_total_count_per_attempt", jobs_ok, "job count differs from total_count in some attempt"))
    checks.append(check("artifacts_read_equal_total_count_per_run", per_run_arts_ok, "artifact count differs from total_count in some run"))
    try:
        repo_arts, repo_atotal = client.pages_obj(f"/repos/{repo}/actions/artifacts", "artifacts")
        checks.append(check("artifacts_per_run_equal_repository_list",
                            len(repo_arts) == per_run_art_count and (repo_atotal is None or repo_atotal == per_run_art_count),
                            f"per run {per_run_art_count}, repository list {len(repo_arts)} (total_count {repo_atotal})"))
    except ApiError as exc:
        if exc.status in (401, 403):
            raise
        checks.append(check("artifacts_per_run_equal_repository_list", False, f"repository artifact list unavailable (HTTP {exc.status})"))
    failed_items = [i for i in log_items + art_items if i["state"] == FAILED_ITEM]
    checks.append(check("available_items_all_saved", not failed_items, f"{len(failed_items)} item(s) could not be saved"))

    # --- resumo temporal ---
    counts = {}
    for i in log_items + art_items:
        counts[i["state"]] = counts.get(i["state"], 0) + 1
    avail_times = [i["run_created_at"] for i in log_items if i["state"] == AVAILABLE and i.get("run_created_at")] + \
                  [i["created_at"] for i in art_items if i["state"] == AVAILABLE and i.get("created_at")]
    retention_days = set()
    for art in artifacts_out:
        try:
            delta = parse_time(art["expires_at"]) - parse_time(art["created_at"])
            retention_days.add(round(delta.total_seconds() / 86400))
        except (KeyError, TypeError, ValueError):
            pass
    extra = {
        "runs": len(runs), "attempts": attempts_total, "jobs": jobs_total,
        "logs": {"total": len(log_items), "saved": log_members},
        "artifacts": {"total": len(art_items), "saved": art_members},
        "bytes_downloaded": budget.used, "max_bytes": max_bytes,
        "temporal_counts": counts,
        "observed_artifact_retention_days": sorted(retention_days),
        "oldest_available_created_at": min(avail_times) if avail_times else None,
    }
    inventory_items = {"logs": log_items, "artifacts": art_items}
    files = {"api-actions-workflows.json": workflows, "api-actions-runs.json": runs,
             "api-actions-attempts.json": attempts_out, "api-actions-artifacts.json": artifacts_out}
    tars = {}
    if log_members:
        tars["actions-logs.tar"] = log_members
    if art_members:
        tars["actions-artifacts.tar"] = art_members
    return {"files": files, "tars": tars, "checks": checks, "count": len(runs), "extra": extra, "items": inventory_items}


def summary_line(status, extra):
    """Linha do log público: só contagens e tamanhos."""
    c = extra["temporal_counts"]
    gaps = ",".join(f"{k}={v}" for k, v in sorted(c.items()) if k != AVAILABLE) or "none"
    return (f"API_READ actions: {status} runs={extra['runs']} attempts={extra['attempts']} jobs={extra['jobs']} "
            f"logs={extra['logs']['saved']}/{extra['logs']['total']} artifacts={extra['artifacts']['saved']}/{extra['artifacts']['total']} "
            f"bytes={extra['bytes_downloaded']} gaps={gaps}")


def cmd_read():
    """Lê a classe e grava os arquivos do pacote, o inventário e o api-actions-status.json."""
    try:
        repo = os.environ["SOURCE_REPOSITORY"]
        token = os.environ["SOURCE_TOKEN"]
    except KeyError as exc:
        print(f"::error::Variável ausente: {exc.args[0]}")
        sys.exit(64)
    try:
        max_bytes = int(os.environ.get("ACTIONS_MAX_BYTES", DEFAULT_MAX_BYTES))
    except ValueError:
        print("::error::ACTIONS_MAX_BYTES inválida")
        sys.exit(64)
    evidence = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))
    package = pathlib.Path(os.environ.get("PACKAGE_DIR", "package"))
    evidence.mkdir(parents=True, exist_ok=True)
    package.mkdir(parents=True, exist_ok=True)
    client = Client(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token)

    work = pathlib.Path(tempfile.mkdtemp(prefix="actions-read-"))
    try:
        try:
            out = read_actions(client, repo, work, max_bytes)
        except ApiError as exc:
            hint = " (o token precisa de Actions: Read?)" if exc.status in (401, 403, 404) else ""
            status = {"status": "FAILED", "count": 0, "files": [], "checks": [], "detail": f"{exc}{hint}"}
            print(f"::error::API_READ actions: FAILED: {exc}{hint}")
            out = None
        except Exception as exc:  # noqa: BLE001 - rede de segurança: a causa vai só como tipo, sem conteúdo
            status = {"status": "FAILED", "count": 0, "files": [], "checks": [],
                      "detail": f"Unexpected error in the Actions reader: {type(exc).__name__}"}
            print(f"::error::API_READ actions: FAILED: {type(exc).__name__}")
            out = None
        inventory = {"schema": "api-actions-inventory/1", "source_repository": repo, "captured_at": now(),
                     "api_base": client.base, "limitations": LIMITATIONS}
        if out is not None:
            files = []
            for name, data in out["files"].items():
                raw = write_json(package / name, data)
                files.append({"file": name, "items": len(data), "sha256": sha256_bytes(raw), "bytes": len(raw)})
            for name, members in out["tars"].items():
                dest = package / name
                shutil.move(str(work / name), dest)
                files.append({"file": name, "items": members, "sha256": file_sha256(dest), "bytes": dest.stat().st_size})
            failed = [c for c in out["checks"] if not c["ok"]]
            gaps = {k: v for k, v in out["extra"]["temporal_counts"].items() if k in GAP_STATES}
            if failed:
                st, detail = "FAILED", "Reconciliation check failed: " + ", ".join(c["check"] for c in failed)
            elif gaps:
                st = "PARTIAL"
                detail = "Some logs or artifacts are not in the package: " + ", ".join(f"{v} {k}" for k, v in sorted(gaps.items())) + "."
            else:
                st, detail = "OK", ""
            status = {"status": st, "count": out["count"], "files": files, "checks": out["checks"], "extra": out["extra"],
                      **({"detail": detail} if detail else {})}
            inventory["items"] = out["items"]
            print(summary_line(st, out["extra"]))
        inventory["classes"] = {"actions": status}
        write_json(package / "api-actions-inventory.json", inventory)
        write_json(evidence / "api-actions-status.json", inventory)
    finally:
        shutil.rmtree(work, ignore_errors=True)


COMMANDS = {"read": cmd_read}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(64)
    COMMANDS[sys.argv[1]]()
