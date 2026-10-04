#!/usr/bin/env python3
"""Restaura um backup do BKP_REPO (OneDrive) para um repositório NOVO do GitHub (RST_REPO): git, labels, milestones e issues.

O QUE É
    Script do workflow restore-repository.yml, com três subcomandos. É a primeira escrita no GitHub do
    sistema: por isso é fail-closed, só escreve no repositório que ele mesmo cria, nunca na origem e nunca
    num repositório que já existia. Repete, de forma automática e conferida, o roteiro do RESTAURAR.txt.

ONDE SE ENCAIXA NO FLUXO
    Mnemonic RST_REPO: linha no commands.log (GO do HITL) -> dispatcher -> restore-repository.yml ->
    revalidar -> token de escrita -> preflight -> gate do destino -> fetch -> restore -> evidência.

SUBCOMANDOS
    fetch           baixa evidence.json, manifest.json e onedrive-package.json de <BACKUP_PATH>/evidence/ e
                    os arquivos do pacote de <BACKUP_PATH>/package/; confere o SHA-256 de cada um contra o
                    manifest e o onedrive-package.json; só aceita um backup do BKP_REPO que não terminou
                    FAILED e cuja classe git foi preservada. Baixa também api-labels.json e api-milestones.json,
                    quando a evidência do backup diz que a classe foi preservada; senão a classe fica NOT-VERIFIED
                    (não é restaurada, nem tratada como vazia). Para as issues baixa api-issues.json e
                    api-issue-comments.json. Grava restore-work/plan.json.
    restore         confere o bundle, define o alvo (<dono>/<nome>-restaurado, ou TARGET_NAME), recusa se o
                    alvo for a origem ou já existir, cria o repositório PRIVADO, envia SÓ refs/heads/* e
                    refs/tags/* (sem --force, sem refs/pull/*), envia o LFS, define a branch padrão e confere
                    as refs do alvo contra o refs.tsv. Só depois disso restaura as labels e os milestones (API REST,
                    sem DELETE) e confere o que ficou no alvo campo a campo. Grava evidence/restore-result.json e
                    evidence/restore-map.json (milestone e issue antigos -> novos).
                    Depois de labels e milestones, recria as issues e os comentários como CÓPIA EQUIVALENTE: cabeçalho
                    com número, autor e datas originais, @menções e referências cruzadas neutralizadas (entre crases),
                    sem assignees; só roda se labels e milestones voltaram OK e se o total de escritas cabe no teto
                    (RESTORE_MAX_WRITES, padrão 450); confere o alvo lendo de volta.
    build-evidence  monta evidence.json e manifest.json (schemas 2.0) da restauração.
                    (saída 21 = destino não validado; 1 = FAILED)

CÓDIGOS DE SAÍDA
    0   ok
    1   build-evidence com objeto FAILED
    21  build-evidence sem destino validado
    22  falha do OneDrive no download ou no hash (ver onedrive.py)
    23  configuração, autenticação ou rotação do OneDrive
    40  backup não restaurável (evidência inválida, não é do BKP_REPO, FAILED, sem branches ou sem bundle)
    41  integridade: SHA-256 divergente, bundle inválido ou refs do bundle diferentes do refs.tsv
    42  alvo recusado: é a origem, já existe, ou nome inválido
    43  GitHub: token inválido, repositório não criado ou resposta inesperada
    44  falha no envio de branches e tags
    45  as refs do alvo não conferem com o refs.tsv
    46  falha no envio dos objetos LFS
    48  git restaurado e conferido, mas labels, milestones ou issues não voltaram como no backup (o alvo NÃO é apagado)
    64  uso incorreto ou variável ausente

VARIÁVEIS DE AMBIENTE
    REQUEST_ID, BACKUP_PATH   inputs autorizados (backup_path = <destination>/<request_id do backup>)
    TARGET_NAME               (opcional) nome do repositório alvo, sem o dono
    RESTORE_TOKEN             token de escrita (secret RESTORE_WRITE_TOKEN); nunca é impresso
    EVIDENCE_DIR              pasta de evidências (padrão: evidence)
    RESTORE_DIR               pasta de trabalho (padrão: restore-work)
    GITHUB_API_URL            (opcional, testes) base da API; padrão https://api.github.com
    GITHUB_GIT_BASE           (opcional, testes) base das URLs Git; padrão https://github.com
    ONEDRIVE_*                ver onedrive.py (Session.from_env)
    PREFLIGHT_DECISION, DESTINATION_VALIDATED, STARTED_AT   como no bkp_repo.py

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - O backup (OneDrive) é lido, nunca alterado. A origem do backup nunca é tocada.
    - Só escreve no repositório que criou nesta execução, privado, e nunca com --force.
    - Nunca imprime o token. A URL de envio não leva o token: ele vai por cabeçalho HTTP (GIT_CONFIG_*).
    - O alvo parcial (falha no meio) NÃO é apagado: o resultado diz qual é, e o HITL decide.
    - Nada é apagado no GitHub: as labels padrão que o GitHub cria e que não existem na origem ficam no alvo e são
      só listadas no resultado.
    - Nunca toca a origem, nem indiretamente: nenhum texto restaurado cita `dono/repo#N` nem URL de issue sem
      estar entre crases (a referência cruzada criaria um evento na issue original); nenhuma @menção é ativa.
    - Sucesso do workflow NÃO prova restauração: a prova é o manifest validado e as refs conferidas.
"""
import base64
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import jsonschema

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bkp_repo as b  # noqa: E402  (reaproveita load, now, sha256, result e o preflight)
import onedrive  # noqa: E402
import restore_projects as rpj  # noqa: E402

RESTORE_DIR = pathlib.Path(os.environ.get("RESTORE_DIR", "restore-work"))
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GIT_BASE = os.environ.get("GITHUB_GIT_BASE", "https://github.com").rstrip("/")

# Arquivos do pacote: os dois primeiros são obrigatórios; os outros, só se existirem no backup.
REQUIRED = ("source.bundle", "refs.tsv")
OPTIONAL = ("lfs-objects.tar", "repo-metadata.json", "api-labels.json", "api-milestones.json", "api-issues.json", "api-issue-comments.json",
            "api-issue-sub-issues.json", "api-issue-dependencies.json",
            "api-projects.json", "api-project-items.json")
# Classes de API restauradas e o arquivo principal do pacote de cada uma (as issues também usam os comentários).
DATA_CLASSES = {"labels": "api-labels.json", "milestones": "api-milestones.json", "issues": "api-issues.json",
                "relations": "api-issue-sub-issues.json", "projects": "api-projects.json"}
EXTRA_FILES = {"issues": ("api-issue-comments.json",), "relations": ("api-issue-dependencies.json",), "projects": ("api-project-items.json",)}
# Sub-issues e dependências vêm no pacote da classe issues do backup: a disposição que conta é a dela.
EVIDENCE_CLASS = {"relations": "issues"}
# Teto de escritas de conteúdo (issues, fechamentos e comentários) por execução: o GitHub limita cerca de 500 por hora.
# Acima do teto a restauração de issues RECUSA antes de escrever qualquer coisa (não restaura pela metade).
MAX_WRITES = int(os.environ.get("RESTORE_MAX_WRITES", "450"))
# Limite do GitHub para o corpo de uma issue ou comentário é 65536 caracteres; sobra margem.
MAX_BODY = 65000
# Disposições do backup que dizem que a classe foi lida e preservada (o resto não é restaurado).
DATA_READY = ("PRESERVED", "PRESERVED-AS-EQUIVALENT-REPRESENTATION")
# O GitHub limita escritas em sequência (limite secundário): pausa entre escritas e espera quando ele pede.
WRITE_DELAY = float(os.environ.get("RESTORE_WRITE_DELAY", "0.8"))
RATE_WAIT = float(os.environ.get("RESTORE_RATE_WAIT", "60"))
# Conferência das issues: o GitHub pode demorar alguns segundos para mostrar o que acabou de criar, por isso
# cada issue é lida de volta pelo número (leitura direta) e, se algo ainda não bate, a conferência se repete.
VERIFY_TRIES = int(os.environ.get("RESTORE_VERIFY_TRIES", "4"))
VERIFY_DELAY = float(os.environ.get("RESTORE_VERIFY_DELAY", "3"))
# Nome do alvo: letras, números, ponto, hífen e sublinhado (regra do GitHub e do schema da linha).
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Limitação padrão de cada classe de API restaurada com sucesso (a disposição é equivalente, nunca idêntica).
RESTORE_LIMITATIONS = {
    "labels": "Restored through the API: name, color and description match the backup; the internal label id is not preserved.",
    "milestones": "Restored through the API: title, state, description and due date match the backup; creation and closing dates and the "
                  "creator cannot be set, and milestone numbers may change when the source had gaps.",
    "issues": "Restored through the API as an equivalent copy (issues and comments): the original number, author and dates are kept only in a "
              "header at the top of each body; @mentions, cross-repository references and issue URLs are wrapped in backticks so nobody is notified "
              "and the source is never touched; no assignees are set; reactions, events, edit history, lock state and issue types are not "
              "restored; issue numbers may change when the source had gaps or pull requests.",
    "projects": rpj.LIMITATIONS,
    "relations": "Restored through the API between the restored issues of this repository (sub-issues and blocked-by/blocking dependencies); "
                 "relations whose other end is in another repository, or is not a restored issue, are reported and not restored.",
}
# Só estas refs voltam ao GitHub; refs/pull/* e outras são gerenciadas por ele e não aceitam envio.
PUSHABLE = ("refs/heads/", "refs/tags/")


class RestoreError(Exception):
    """Falha com código de saída e motivo estáveis. A mensagem é segura para log (sem token)."""

    def __init__(self, code, reason, message, **extra):
        super().__init__(message)
        self.code, self.reason, self.message, self.extra = code, reason, message, extra


def write_result(data):
    """Grava evidence/restore-result.json (lido pelo build-evidence)."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "restore-result.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def fail(err, state):
    """Registra a falha: resultado em arquivo, BKP_RESULT FAILED e saída com o código do erro."""
    print(f"::error::{err.reason}: {err.message}")
    data = {"status": "FAILED", "reason": err.reason, "message": err.message, **state, **err.extra}
    write_result(data)
    b.result("FAILED", reason=err.reason, **{k: v for k, v in state.items() if k in ("target_repository", "created")}, **err.extra)
    sys.exit(err.code)


# --- GitHub (API REST e Git) --------------------------------------------------------------

def gh(method, path, body=None, token=None, retries=3, headers_out=None):
    """Chamada à API do GitHub. Devolve (status, json). Repete em 502/503/504. Nunca imprime o token.
    `headers_out`, se dado, recebe os cabeçalhos da resposta (nomes em minúsculas)."""
    token = token or os.environ.get("RESTORE_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    last = (0, {"message": "sem resposta"})
    for attempt in range(retries):
        req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if headers_out is not None:
                    headers_out.update({k.lower(): v for k, v in resp.headers.items()})
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"message": raw[:120].decode(errors="replace")}
            if exc.code in (502, 503, 504) and attempt < retries - 1:
                time.sleep(float(os.environ.get("RESTORE_RETRY_DELAY", "2")) * (attempt + 1))
                last = (exc.code, payload)
                continue
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = (0, {"message": str(exc)[:120]})
            time.sleep(float(os.environ.get("RESTORE_RETRY_DELAY", "2")) * (attempt + 1))
    return last


def gh_write(method, path, body):
    """Escrita no alvo, com pausa entre chamadas e espera quando o GitHub pede (403/429 de limite de taxa)."""
    for attempt in range(3):
        time.sleep(WRITE_DELAY)
        status, payload = gh(method, path, body)
        limited = status in (403, 429) and "rate limit" in str((payload or {}).get("message", "")).lower()
        if not limited or attempt == 2:
            return status, payload
        time.sleep(RATE_WAIT)
    return status, payload


def gh_list(path):
    """Lista paginada (100 por página) de uma rota GET do alvo. Devolve (status, itens)."""
    items = []
    for page in range(1, 201):
        sep = "&" if "?" in path else "?"
        status, payload = gh("GET", f"{path}{sep}per_page=100&page={page}")
        if status != 200 or not isinstance(payload, list):
            return status, payload
        items.extend(payload)
        if len(payload) < 100:
            return 200, items
    return 200, items


def short(status, payload):
    """Resumo seguro de um erro da API: status e mensagem do serviço."""
    return f"HTTP {status}: {str((payload or {}).get('message', ''))[:160]}"


def git_env():
    """Ambiente do git: o token vai por cabeçalho HTTP só neste processo (não na URL, no log nem na config)."""
    env = dict(os.environ)
    token = os.environ.get("RESTORE_TOKEN", "")
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.{GIT_BASE}/.extraheader",
               GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode(),
               GIT_TERMINAL_PROMPT="0")
    return env


def git(*args, cwd=None):
    """Executa git e devolve (código, saída). A saída pode ter mensagens do servidor, nunca o token."""
    proc = subprocess.run(["git", *args], cwd=cwd, env=git_env(), capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr)


def parse_refs(text):
    """{refname: sha} de linhas 'refname<TAB>sha[<TAB>tipo]'."""
    out = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].startswith("refs/"):
            out[parts[0]] = parts[1]
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- fetch --------------------------------------------------------------------------------

def data_classes_plan(ev, files):
    """O que fazer com labels e milestones: READY (restaurar) ou NOT_VERIFIED (com o motivo), nunca "vazio" por engano."""
    out = {}
    for cls, fname in DATA_CLASSES.items():
        ev_cls = EVIDENCE_CLASS.get(cls, cls)
        obj = next((o for o in ev["objects"] if o["object_class"] == ev_cls), None)
        if obj is None:
            out[cls] = {"status": "NOT_VERIFIED", "reason": f"a evidência do backup não traz a classe {ev_cls}"}
        elif obj["disposition"] not in DATA_READY + (("PARTIALLY-PRESERVED",) if cls == "projects" else ()):
            out[cls] = {"status": "NOT_VERIFIED", "reason": f"a classe {ev_cls} ficou {obj['disposition']} no backup"}
        elif any(n not in files for n in (fname,) + EXTRA_FILES.get(cls, ())):
            missing = [n for n in (fname,) + EXTRA_FILES.get(cls, ()) if n not in files]
            out[cls] = {"status": "NOT_VERIFIED", "reason": f"{', '.join(missing)} não está no pacote do backup"}
        else:
            try:
                for name in (fname,) + EXTRA_FILES.get(cls, ()):
                    items = json.loads((RESTORE_DIR / "package" / name).read_text(encoding="utf-8"))
                    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                        raise ValueError("não é uma lista de objetos")
            except ValueError as exc:
                out[cls] = {"status": "NOT_VERIFIED", "reason": f"{name} não pôde ser lido: {exc}"}
                continue
            out[cls] = {"status": "READY", "file": fname, "count": len(json.loads((RESTORE_DIR / "package" / fname).read_text(encoding="utf-8")))}
    return out


def cmd_fetch():
    """Baixa e confere o backup. Não escreve nada no GitHub."""
    base = os.environ["BACKUP_PATH"].strip("/")
    state = {}
    try:
        try:
            session = onedrive.Session.from_env()
            for name in ("evidence.json", "manifest.json", "onedrive-package.json"):
                session.download_file(f"{base}/evidence/{name}", RESTORE_DIR / "evidence" / name)
        except onedrive.OneDriveError as exc:
            raise RestoreError(23 if exc.code in onedrive.AUTH_CODES else 22, "ONEDRIVE_DOWNLOAD_FAILED", f"{exc.code}: {exc.message}")
        load = lambda n: json.loads((RESTORE_DIR / "evidence" / n).read_text())
        ev, man, pkg = load("evidence.json"), load("manifest.json"), load("onedrive-package.json")
        try:
            jsonschema.validate(ev, b.load("backup/schemas/evidence.schema.yaml"))
            jsonschema.validate(man, b.load("backup/schemas/backup-manifest.schema.yaml"))
        except jsonschema.ValidationError as exc:
            raise RestoreError(40, "BACKUP_EVIDENCE_INVALID", f"evidência ou manifest do backup fora do schema: {exc.message[:120]}")
        if ev.get("capability_id") != "backup_repository" or man.get("capability_id") != "backup_repository":
            raise RestoreError(40, "NOT_A_REPOSITORY_BACKUP", "só backups do BKP_REPO (backup_repository) podem ser restaurados por esta capability")
        if man["status"] not in ("COMPLETE", "COMPLETE_WITH_EXCEPTIONS"):
            raise RestoreError(40, "BACKUP_NOT_COMPLETE", f"o backup terminou {man['status']}; só COMPLETE ou COMPLETE_WITH_EXCEPTIONS é restaurável")
        git_obj = next((o for o in ev["objects"] if o["object_class"] == "git"), None)
        if not git_obj or git_obj["disposition"] not in ("PRESERVED", "PARTIALLY-PRESERVED"):
            raise RestoreError(40, "GIT_NOT_PRESERVED", "a classe git do backup não foi preservada")
        source = ev["source_scope"].get("source_repository", "")
        if not SOURCE_RE.match(source):
            raise RestoreError(40, "BACKUP_EVIDENCE_INVALID", "a evidência do backup não traz um source_repository válido")
        # A evidência baixada precisa bater com o SHA-256 que o manifest registrou para ela.
        by_path = {a["path"]: a for a in man["artifacts"]}
        ev_art = by_path.get("evidence.json")
        if not ev_art or ev_art["sha256"] != sha256_file(RESTORE_DIR / "evidence" / "evidence.json"):
            raise RestoreError(41, "HASH_MISMATCH", "o SHA-256 do evidence.json difere do registrado no manifest")
        # Arquivos do pacote: todos precisam estar dentro de <BACKUP_PATH>/package/ e bater nos dois registros.
        entries = {pathlib.PurePosixPath(e["remote_path"]).name: e for e in pkg}
        missing = [n for n in REQUIRED if n not in entries]
        if missing:
            raise RestoreError(40, "BACKUP_PACKAGE_INCOMPLETE", "faltam no pacote do backup: " + ", ".join(missing))
        files = {}
        for name in REQUIRED + OPTIONAL:
            entry = entries.get(name)
            if entry is None:
                continue
            if entry["remote_path"] != f"{base}/package/{name}":
                raise RestoreError(40, "BACKUP_PATH_MISMATCH", f"{name} está registrado fora de {base}/package/")
            art = by_path.get(entry["remote_path"])
            if not art or art["sha256"] != entry["sha256"]:
                raise RestoreError(41, "HASH_MISMATCH", f"o SHA-256 de {name} difere entre o manifest e o onedrive-package.json")
            try:
                session.download_file(entry["remote_path"], RESTORE_DIR / "package" / name)
            except onedrive.OneDriveError as exc:
                raise RestoreError(23 if exc.code in onedrive.AUTH_CODES else 22, "ONEDRIVE_DOWNLOAD_FAILED", f"{exc.code}: {exc.message}")
            if sha256_file(RESTORE_DIR / "package" / name) != entry["sha256"]:
                (RESTORE_DIR / "package" / name).unlink()
                raise RestoreError(41, "HASH_MISMATCH", f"o SHA-256 de {name} baixado difere do registrado")
            files[name] = {"sha256": entry["sha256"], "bytes": entry["bytes"]}
        metadata = {}
        if "repo-metadata.json" in files:
            metadata = json.loads((RESTORE_DIR / "package" / "repo-metadata.json").read_text())
        classes = data_classes_plan(ev, files)
        plan = {"source_repository": source, "backup_request_id": ev["request_id"], "backup_path": base,
                "backup_status": man["status"], "git_disposition": git_obj["disposition"], "files": files,
                "has_lfs": "lfs-objects.tar" in files, "metadata": metadata, "classes": classes}
        (RESTORE_DIR / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        print("RESTORE_FETCH " + json.dumps({"source_repository": source, "backup_request_id": ev["request_id"],
                                             "files": sorted(files), "has_lfs": plan["has_lfs"],
                                             "has_metadata": bool(metadata),
                                             "classes": {c: v["status"] for c, v in classes.items()}}, sort_keys=True), flush=True)
    except RestoreError as err:
        fail(err, state)


# --- restore ------------------------------------------------------------------------------

def choose_default_branch(plan, heads, mirror):
    """Branch padrão do alvo: a do metadado salvo, senão o HEAD do bundle, senão a única branch."""
    wanted = (plan.get("metadata") or {}).get("default_branch")
    if wanted and f"refs/heads/{wanted}" in heads:
        return wanted
    code, out = git("symbolic-ref", "--short", "HEAD", cwd=mirror)
    head = out.strip()
    if code == 0 and f"refs/heads/{head}" in heads:
        return head
    only = [r[len("refs/heads/"):] for r in heads]
    return only[0] if len(only) == 1 else None


def restore_labels(repo, labels):
    """Recria as labels do backup no alvo e confere. Devolve o resultado da classe (status OK ou FAILED)."""
    status, existing = gh_list(f"/repos/{repo}/labels")
    if status != 200:
        return {"status": "FAILED", "reason": "não foi possível listar as labels do alvo: " + short(status, existing)}
    by_name = {l["name"].lower(): l["name"] for l in existing}
    created = updated = 0
    errors = []
    for lb in labels:
        name = lb.get("name")
        body = {"color": str(lb.get("color") or "ededed").lstrip("#"), "description": lb.get("description") or ""}
        if not name:
            errors.append("label sem nome no backup")
            continue
        current = by_name.get(name.lower())
        if current is None:
            status, payload = gh_write("POST", f"/repos/{repo}/labels", dict(body, name=name))
            ok, bucket = status == 201, "created"
        else:
            status, payload = gh_write("PATCH", f"/repos/{repo}/labels/{urllib.parse.quote(current, safe='')}", dict(body, new_name=name))
            ok, bucket = status == 200, "updated"
        if ok:
            created += bucket == "created"
            updated += bucket == "updated"
        else:
            errors.append(f"{name}: " + short(status, payload))
    # Conferência: lê de volta e compara nome, cor e descrição de cada label do backup.
    status, after = gh_list(f"/repos/{repo}/labels")
    if status != 200:
        return {"status": "FAILED", "reason": "não foi possível conferir as labels do alvo: " + short(status, after)}
    got = {l["name"]: l for l in after}
    mismatches = []
    for lb in labels:
        name = lb.get("name")
        item = got.get(name)
        want = (str(lb.get("color") or "ededed").lstrip("#").lower(), lb.get("description") or "")
        if item is None or ((item.get("color") or "").lower(), item.get("description") or "") != want:
            mismatches.append(name)
    wanted = {(lb.get("name") or "").lower() for lb in labels}
    extras = sorted(n for n in got if n.lower() not in wanted)
    out = {"status": "OK", "expected": len(labels), "created": created, "updated": updated, "extras": extras}
    if errors or mismatches:
        out.update(status="FAILED", errors=errors[:10], mismatches=sorted(m for m in mismatches if m)[:10],
                   reason=f"{len(errors)} erro(s) de escrita e {len(mismatches)} label(s) que não conferem")
    return out


def due_date(value):
    """Data (UTC) de um due_on ISO 8601, ou None."""
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(datetime.timezone.utc).date().isoformat()
    except (AttributeError, ValueError):
        return None


def restore_milestones(repo, milestones):
    """Recria os milestones em ordem de número, guarda o mapa antigo -> novo e confere. Devolve (resultado, mapa)."""
    ordered = sorted(milestones, key=lambda m: m.get("number", 0))
    mapping, errors, notes = {}, [], []
    for m in ordered:
        want_closed = m.get("state") == "closed"
        body = {"title": m.get("title"), "description": m.get("description") or "", "state": "closed" if want_closed else "open"}
        if m.get("due_on"):
            body["due_on"] = m["due_on"]
        status, created = gh_write("POST", f"/repos/{repo}/milestones", body)
        if status != 201 or "number" not in created:
            errors.append(f"{m.get('title')}: " + short(status, created))
            continue
        mapping[m.get("number")] = created["number"]
        if want_closed and created.get("state") != "closed":
            status, payload = gh_write("PATCH", f"/repos/{repo}/milestones/{created['number']}", {"state": "closed"})
            if status != 200:
                errors.append(f"{m.get('title')}: não foi possível fechar: " + short(status, payload))
    status, after = gh_list(f"/repos/{repo}/milestones?state=all")
    if status != 200:
        return {"status": "FAILED", "reason": "não foi possível conferir os milestones do alvo: " + short(status, after)}, mapping
    got = {m["number"]: m for m in after}
    mismatches = []
    for m in ordered:
        new = mapping.get(m.get("number"))
        item = got.get(new)
        if item is None:
            mismatches.append(m.get("title"))
            continue
        same = (item.get("title"), item.get("state"), item.get("description") or "") == (m.get("title"), m.get("state"), m.get("description") or "")
        if m.get("due_on") and item.get("due_on") != m["due_on"]:
            if due_date(item.get("due_on")) == due_date(m["due_on"]):
                notes.append(f"o GitHub normalizou o horário do prazo de '{m.get('title')}' (a data é a mesma)")
            else:
                same = False
        if not m.get("due_on") and item.get("due_on"):
            same = False
        if not same:
            mismatches.append(m.get("title"))
    shifted = sorted(old for old, new in mapping.items() if old != new)
    out = {"status": "OK", "expected": len(ordered), "created": len(mapping), "numbers_changed": shifted, "notes": notes}
    if errors or mismatches:
        out.update(status="FAILED", errors=errors[:10], mismatches=mismatches[:10],
                   reason=f"{len(errors)} erro(s) de escrita e {len(mismatches)} milestone(s) que não conferem")
    return out, mapping


# --- issues e comentários (etapa 3) -------------------------------------------------------

# Um só padrão, para cada trecho ser embrulhado uma única vez: URL de issue ou PR, referência dono/repo#N e @menção.
_NEUTRAL = re.compile(
    r"(?P<url>https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/\d+(?:#[\w-]+)?)"
    r"|(?<![\w`/.-])(?P<ref>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+)"
    r"|(?<![\w`/.@+-])(?P<mention>@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:/[A-Za-z0-9_-]+)?)")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def neutralize(text):
    """Põe entre crases @menções, referências dono/repo#N e URLs de issues/PRs, fora de código.

    Não mexe em blocos de código (cercas ``` ou ~~~), em trechos já entre crases nem em e-mails. Assim o texto
    restaurado não notifica ninguém e não cria referência cruzada (evento) na issue original.
    """
    out, fence = [], None
    for line in (text or "").split("\n"):
        m = _FENCE.match(line)
        if fence:
            out.append(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            continue
        parts, pos = [], 0
        for span in _CODE_SPAN.finditer(line):
            parts.append(_NEUTRAL.sub(lambda g: "`" + g.group(0) + "`", line[pos:span.start()]))
            parts.append(span.group(0))
            pos = span.end()
        parts.append(_NEUTRAL.sub(lambda g: "`" + g.group(0) + "`", line[pos:]))
        out.append("".join(parts))
    return "\n".join(out)


def issue_header(source, issue):
    """Cabeçalho do corpo da issue restaurada: número, autor e datas originais (tudo entre crases: nada ativo)."""
    login = (issue.get("user") or {}).get("login") or "desconhecido"
    bits = [f"original: `{source}#{issue.get('number')}`", f"autor: `@{login}`", f"criada em {issue.get('created_at')}"]
    if issue.get("state") == "closed":
        bits.append(f"fechada em {issue.get('closed_at')} ({issue.get('state_reason') or 'completed'})")
    assignees = [a.get("login") for a in issue.get("assignees") or [] if a.get("login")]
    if assignees:
        bits.append("atribuída originalmente a " + ", ".join(f"`@{a}`" for a in assignees))
    return ("> **Cópia restaurada de um backup** · " + " · ".join(bits) + "\n"
            "> Número, autor e datas originais não podem ser restaurados de forma idêntica; esta é uma cópia equivalente.\n\n")


def comment_header(comment):
    login = (comment.get("user") or {}).get("login") or "desconhecido"
    return (f"> **Comentário restaurado** · autor: `@{login}` · em {comment.get('created_at')}"
            f" · id original `{comment.get('id')}`\n\n")


def issue_number_of(url):
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def plan_issue_writes(source, issues, comments, label_names, milestone_map):
    """Monta, SEM escrever nada, o que será enviado. Devolve (plano, motivo): motivo != None recusa a restauração."""
    ordered = sorted(issues, key=lambda i: i.get("number", 0))
    numbers = {i["number"] for i in ordered}
    by_issue = {}
    for c in sorted(comments, key=lambda c: (c.get("created_at") or "", c.get("id") or 0)):
        n = issue_number_of(c.get("issue_url"))
        if n not in numbers:
            return None, f"o comentário {c.get('id')} pertence à issue {n}, que não está no backup"
        by_issue.setdefault(n, []).append(c)
    plan = []
    for i in ordered:
        labels = [lb["name"] for lb in i.get("labels") or []]
        unknown = [n for n in labels if n not in label_names]
        if unknown:
            return None, f"a issue {i['number']} cita labels que o backup não traz: {', '.join(unknown[:3])}"
        ms = (i.get("milestone") or {}).get("number")
        if ms is not None and ms not in milestone_map:
            return None, f"a issue {i['number']} cita o milestone {ms}, que não foi restaurado"
        body = issue_header(source, i) + neutralize(i.get("body") or "")
        cbodies = [comment_header(c) + neutralize(c.get("body") or "") for c in by_issue.get(i["number"], [])]
        if len(body) > MAX_BODY or any(len(b) > MAX_BODY for b in cbodies):
            return None, f"a issue {i['number']} ou um comentário dela passa do limite de {MAX_BODY} caracteres do GitHub"
        closed = i.get("state") == "closed"
        reason = i.get("state_reason") if i.get("state_reason") in ("completed", "not_planned", "duplicate") else "completed"
        plan.append({"old": i["number"], "title": i.get("title") or "(sem título)", "body": body, "labels": labels,
                     "milestone": milestone_map.get(ms) if ms is not None else None, "closed": closed, "reason": reason,
                     "comments": cbodies})
    writes = sum(1 + int(p["closed"]) + len(p["comments"]) for p in plan)
    if writes > MAX_WRITES:
        return None, (f"a restauração exigiria {writes} escritas (issues, fechamentos e comentários), acima do teto de {MAX_WRITES} "
                      "por execução (o GitHub limita cerca de 500 por hora); nada foi escrito")
    return plan, None


def restore_issues(repo, plan):
    """Cria as issues e os comentários do plano, em ordem, e confere lendo de volta.
    Devolve (resultado, mapa antigo -> novo número, mapa antigo -> id novo, mapa antigo -> node_id novo;
    o id é o que as relações usam e o node_id, o que os Projects usam)."""
    mapping, ids, node_ids, errors, writes = {}, {}, {}, [], 0
    closed = 0
    for p in plan:
        body = {"title": p["title"], "body": p["body"]}
        if p["labels"]:
            body["labels"] = p["labels"]
        if p["milestone"] is not None:
            body["milestone"] = p["milestone"]
        status, created = gh_write("POST", f"/repos/{repo}/issues", body)
        writes += 1
        if status != 201 or "number" not in created:
            errors.append(f"issue {p['old']}: " + short(status, created))
            break
        new = created["number"]
        mapping[p["old"]] = new
        ids[p["old"]] = created.get("id")
        node_ids[p["old"]] = created.get("node_id")
        if p["closed"]:
            status, payload = gh_write("PATCH", f"/repos/{repo}/issues/{new}", {"state": "closed", "state_reason": p["reason"]})
            writes += 1
            if status != 200:
                errors.append(f"issue {p['old']}: não foi possível fechar: " + short(status, payload))
                break
            closed += 1
        for n, cbody in enumerate(p["comments"], 1):
            status, payload = gh_write("POST", f"/repos/{repo}/issues/{new}/comments", {"body": cbody})
            writes += 1
            if status != 201:
                errors.append(f"issue {p['old']}, comentário {n}: " + short(status, payload))
                break
        if errors:
            break
    out = {"status": "OK", "expected": len(plan), "created": len(mapping), "closed": closed, "writes": writes,
           "comments": sum(len(p["comments"]) for p in plan),
           "numbers_changed": sorted(old for old, new in mapping.items() if old != new)}
    if errors:
        # Interrompe na primeira falha: continuar deslocaria a numeração das próximas. Nada é apagado.
        out.update(status="FAILED", errors=errors[:10], reason=f"{len(errors)} erro(s) de escrita; a restauração das issues foi interrompida")
        return out, mapping, ids, node_ids
    # Conferência: lê de volta cada issue pelo número (não por listagem, que pode atrasar) e compara com o que foi enviado.
    attempts = 0
    for attempts in range(1, VERIFY_TRIES + 1):
        mismatches = issue_mismatches(repo, plan, mapping)
        if not mismatches:
            break
        if attempts < VERIFY_TRIES:
            time.sleep(VERIFY_DELAY)
    out["verify_attempts"] = attempts
    if mismatches:
        out.update(status="FAILED", mismatches=mismatches[:10], reason=f"{len(mismatches)} divergência(s) na leitura de volta")
    return out, mapping, ids, node_ids


def issue_mismatches(repo, plan, mapping):
    """Lê de volta cada issue (GET direto) e seus comentários. Devolve a lista de divergências (números antigos ou textos)."""
    bad = []
    for p in plan:
        new = mapping[p["old"]]
        status, item = gh("GET", f"/repos/{repo}/issues/{new}")
        if status != 200:
            bad.append(p["old"])
            continue
        status, comments = gh_list(f"/repos/{repo}/issues/{new}/comments")
        if status != 200:
            bad.append(p["old"])
            continue
        bodies = [c.get("body") for c in sorted(comments, key=lambda c: c.get("id", 0))]
        same = (item.get("title") == p["title"] and item.get("body") == p["body"]
                and item.get("state") == ("closed" if p["closed"] else "open")
                and (not p["closed"] or item.get("state_reason") in (None, p["reason"]))
                and sorted(l["name"] for l in item.get("labels") or []) == sorted(p["labels"])
                and ((item.get("milestone") or {}).get("number") == p["milestone"])
                and not (item.get("assignees") or [])
                and bodies == p["comments"])
        if not same:
            bad.append(p["old"])
    # Não pode haver nenhuma issue além da última restaurada (o alvo é novo).
    if mapping:
        status, _ = gh("GET", f"/repos/{repo}/issues/{max(mapping.values()) + 1}")
        if status != 404:
            bad.append(f"existe uma issue além da última restaurada ({max(mapping.values()) + 1}); resposta HTTP {status}")
    return bad


def plan_relation_writes(source, sub_issues, dependencies, issue_map, ids, budget):
    """Monta, SEM escrever nada, as relações a gravar entre issues restauradas deste repositório.
    Devolve (subs, deps, pulados, motivo): subs = [(pai, filha)], deps = [(bloqueada, bloqueadora)] em números ANTIGOS.
    Relação com ponta em outro repositório ou fora do mapa é só reportada (pulados). motivo != None recusa a classe."""
    lower = source.lower()

    def usable(ref, kind, label):
        if (ref.get("repository") or "").lower() != lower:
            return f"{kind} {label}: a outra ponta está em {ref.get('repository')}, outro repositório"
        if ref.get("number") not in issue_map or ids.get(ref.get("number")) is None:
            return f"{kind} {label}: a issue {ref.get('number')} não foi restaurada"
        return None

    subs, deps, skipped = set(), set(), []
    for e in sub_issues:
        parent = e.get("parent")
        for c in e.get("children") or []:
            label = f"{parent} -> {c.get('number')}"
            why = None if parent in issue_map and ids.get(parent) is not None else f"sub-issue {label}: a issue pai {parent} não foi restaurada"
            why = why or usable(c, "sub-issue", label)
            if why:
                skipped.append(why)
            else:
                subs.add((parent, c["number"]))
    # A dependência é simétrica: "A bloqueada por B" é o mesmo que "B bloqueia A". A união dos dois lados cobre um lado ausente.
    for e in dependencies:
        issue = e.get("issue")
        for r in e.get("blocked_by") or []:
            label = f"{issue} bloqueada por {r.get('number')}"
            why = None if issue in issue_map and ids.get(issue) is not None else f"dependência {label}: a issue {issue} não foi restaurada"
            why = why or usable(r, "dependência", label)
            if why:
                skipped.append(why)
            else:
                deps.add((issue, r["number"]))
        for r in e.get("blocking") or []:
            label = f"{issue} bloqueia {r.get('number')}"
            why = None if issue in issue_map and ids.get(issue) is not None else f"dependência {label}: a issue {issue} não foi restaurada"
            why = why or usable(r, "dependência", label)
            if why:
                skipped.append(why)
            else:
                deps.add((r["number"], issue))
    subs, deps = sorted(subs), sorted(deps)
    if len(subs) + len(deps) > budget:
        return None, None, skipped, (f"as relações exigiriam {len(subs) + len(deps)} escritas, acima do que resta do teto de {MAX_WRITES} por "
                                     f"execução ({max(budget, 0)}); nenhuma relação foi escrita")
    return subs, deps, sorted(set(skipped)), None


def restore_relations(repo, subs, deps, issue_map, ids, skipped):
    """Grava sub-issues e dependências (pelo id novo de cada issue) e confere lendo de volta. Para na primeira falha."""
    errors, writes = [], 0
    for parent, child in subs:
        status, payload = gh_write("POST", f"/repos/{repo}/issues/{issue_map[parent]}/sub_issues", {"sub_issue_id": ids[child]})
        writes += 1
        if status != 201:
            errors.append(f"sub-issue {parent} -> {child}: " + short(status, payload))
            break
    if not errors:
        for blocked, blocker in deps:
            status, payload = gh_write("POST", f"/repos/{repo}/issues/{issue_map[blocked]}/dependencies/blocked_by", {"issue_id": ids[blocker]})
            writes += 1
            if status != 201:
                errors.append(f"dependência {blocked} bloqueada por {blocker}: " + short(status, payload))
                break
    out = {"status": "OK", "sub_issues": len(subs), "dependencies": len(deps), "writes": writes, "skipped": len(skipped)}
    if skipped:
        out.update(status="PARTIAL", skipped_examples=skipped[:10])
    if errors:
        out.update(status="FAILED", errors=errors[:10], reason=f"{len(errors)} erro(s) de escrita; a restauração das relações foi interrompida")
        return out
    attempts = 0
    for attempts in range(1, VERIFY_TRIES + 1):
        mismatches = relation_mismatches(repo, subs, deps, issue_map)
        if not mismatches:
            break
        if attempts < VERIFY_TRIES:
            time.sleep(VERIFY_DELAY)
    out["verify_attempts"] = attempts
    if mismatches:
        out.update(status="FAILED", mismatches=mismatches[:10], reason=f"{len(mismatches)} divergência(s) na leitura de volta das relações")
    return out


def relation_mismatches(repo, subs, deps, issue_map):
    """Lê de volta TODAS as issues restauradas (GET direto) e compara relações esperadas com as do alvo, nos dois sentidos:
    nada a menos e nada a mais. Devolve a lista de divergências (textos)."""
    new_of = issue_map
    exp_children, exp_parent, exp_blocked_by, exp_blocking = {}, {}, {}, {}
    for parent, child in subs:
        exp_children.setdefault(new_of[parent], set()).add(new_of[child])
        exp_parent[new_of[child]] = new_of[parent]
    for blocked, blocker in deps:
        exp_blocked_by.setdefault(new_of[blocked], set()).add(new_of[blocker])
        exp_blocking.setdefault(new_of[blocker], set()).add(new_of[blocked])
    bad = []
    for old, new in sorted(issue_map.items()):
        status, item = gh("GET", f"/repos/{repo}/issues/{new}")
        if status != 200:
            bad.append(f"issue {old}: leitura HTTP {status}")
            continue
        sub_total = (item.get("sub_issues_summary") or {}).get("total", 0)
        dep = item.get("issue_dependencies_summary") or {}
        want_children = exp_children.get(new, set())
        want_by, want_ing = exp_blocked_by.get(new, set()), exp_blocking.get(new, set())
        if sub_total != len(want_children):
            bad.append(f"issue {old}: {sub_total} sub-issue(s), esperava {len(want_children)}")
        if dep.get("total_blocked_by", 0) != len(want_by) or dep.get("total_blocking", 0) != len(want_ing):
            bad.append(f"issue {old}: dependências {dep.get('total_blocked_by', 0)}/{dep.get('total_blocking', 0)}, esperava {len(want_by)}/{len(want_ing)}")
        if issue_number_of(item.get("parent_issue_url")) != exp_parent.get(new):
            bad.append(f"issue {old}: o pai no alvo é {issue_number_of(item.get('parent_issue_url'))}, esperava {exp_parent.get(new)}")
        for route, want in (("sub_issues", want_children), ("dependencies/blocked_by", want_by), ("dependencies/blocking", want_ing)):
            if not want:
                continue
            status, listed = gh_list(f"/repos/{repo}/issues/{new}/{route}")
            if status != 200 or {x.get("number") for x in listed} != want:
                bad.append(f"issue {old}: a lista {route} não confere (HTTP {status})")
    return bad


def restore_relations_class(plan, repo, issue_result, issue_map, ids):
    """Decide e executa a classe relations. Só roda se as issues voltaram OK (política 6A) e o plano cabe no teto."""
    info = plan.get("classes", {}).get("relations", {"status": "NOT_VERIFIED", "reason": "o plano não traz a classe relations"})
    if info["status"] != "READY":
        return {"status": "NOT_VERIFIED", "reason": info["reason"]}
    if issue_result.get("status") != "OK":
        return {"status": "NOT_VERIFIED", "reason": "as relações dependem das issues, que não foram restauradas com sucesso"}
    pkg = RESTORE_DIR / "package"
    sub_issues = json.loads((pkg / "api-issue-sub-issues.json").read_text(encoding="utf-8"))
    dependencies = json.loads((pkg / "api-issue-dependencies.json").read_text(encoding="utf-8"))
    budget = MAX_WRITES - int(issue_result.get("writes", 0))
    subs, deps, skipped, reason = plan_relation_writes(plan["source_repository"], sub_issues, dependencies, issue_map, ids, budget)
    if reason:
        return {"status": "NOT_VERIFIED", "reason": reason, "skipped": len(skipped), "skipped_examples": skipped[:10]}
    return restore_relations(repo, subs, deps, issue_map, ids, skipped)


def restore_projects_class(plan, repo, results, issue_map, node_ids, ctx):
    """Classe projects: só depois das issues OK (os itens apontam para elas) e se o plano cabe no teto de escritas."""
    info = plan.get("classes", {}).get("projects", {"status": "NOT_VERIFIED", "reason": "o plano não traz a classe projects"})
    if info["status"] != "READY":
        return {"status": "NOT_VERIFIED", "reason": info["reason"]}
    if results.get("issues", {}).get("status") != "OK":
        return {"status": "NOT_VERIFIED", "reason": "os Projects dependem das issues, que não foram restauradas com sucesso"}
    pkg = RESTORE_DIR / "package"
    load = lambda name: json.loads((pkg / name).read_text(encoding="utf-8"))
    issues = load("api-issues.json")
    used = int(results["issues"].get("writes", 0)) + int(results.get("relations", {}).get("writes", 0))
    return rpj.restore_projects(
        source=plan["source_repository"], projects=load("api-projects.json"), items=load("api-project-items.json"), issues=issues,
        issue_map=issue_map, node_ids=node_ids, issue_states={i["number"]: ("closed" if i.get("state") == "closed" else "open") for i in issues},
        repo_node_id=ctx.get("repo_node_id"), owner_id=ctx.get("owner_id"), api_base=API, token=os.environ.get("RESTORE_TOKEN", ""),
        token_scopes=ctx.get("token_scopes"), budget=MAX_WRITES - used, rest_get=lambda n: gh("GET", f"/repos/{repo}/issues/{n}"),
        evidence_dir=EVIDENCE, write_delay=WRITE_DELAY, rate_wait=RATE_WAIT, settle=float(os.environ.get("RESTORE_PROJECT_SETTLE", "5")),
        tries=VERIFY_TRIES, delay=VERIFY_DELAY)


def restore_data_classes(plan, repo, ctx=None):
    """Restaura labels, milestones, issues, relações e Projects do plano. Devolve ({classe: resultado}, mapa de milestones, mapa de issues)."""
    results, mapping, issue_map, ids, node_ids = {}, {}, {}, {}, {}
    for cls, fname in DATA_CLASSES.items():
        if cls in ("issues", "relations", "projects"):
            continue
        info = plan.get("classes", {}).get(cls, {"status": "NOT_VERIFIED", "reason": "o plano não traz a classe"})
        if info["status"] != "READY":
            results[cls] = {"status": "NOT_VERIFIED", "reason": info["reason"]}
            continue
        items = json.loads((RESTORE_DIR / "package" / fname).read_text(encoding="utf-8"))
        if cls == "labels":
            results[cls] = restore_labels(repo, items)
        else:
            results[cls], mapping = restore_milestones(repo, items)
    # Issues: só se labels e milestones voltaram OK (as issues dependem deles) e se o plano cabe no teto.
    info = plan.get("classes", {}).get("issues", {"status": "NOT_VERIFIED", "reason": "o plano não traz a classe issues"})
    not_ok = [c for c in ("labels", "milestones") if results.get(c, {}).get("status") != "OK"]
    if info["status"] != "READY":
        results["issues"] = {"status": "NOT_VERIFIED", "reason": info["reason"]}
    elif not_ok:
        results["issues"] = {"status": "NOT_VERIFIED", "reason": "as issues dependem de " + " e ".join(not_ok) + ", que não foram restauradas com sucesso"}
    else:
        pkg = RESTORE_DIR / "package"
        issues = json.loads((pkg / "api-issues.json").read_text(encoding="utf-8"))
        comments = json.loads((pkg / "api-issue-comments.json").read_text(encoding="utf-8"))
        labels = {lb.get("name") for lb in json.loads((pkg / "api-labels.json").read_text(encoding="utf-8"))}
        wplan, reason = plan_issue_writes(plan["source_repository"], issues, comments, labels, mapping)
        if reason:
            results["issues"] = {"status": "NOT_VERIFIED", "reason": reason}
        else:
            results["issues"], issue_map, ids, node_ids = restore_issues(repo, wplan)
    results["relations"] = restore_relations_class(plan, repo, results["issues"], issue_map, ids)
    results["projects"] = restore_projects_class(plan, repo, results, issue_map, node_ids, ctx or {})
    return results, mapping, issue_map


def cmd_restore():
    """Cria o alvo e restaura branches, tags e LFS, com conferência final."""
    plan = json.loads((RESTORE_DIR / "plan.json").read_text())
    source = plan["source_repository"]
    state = {"source_repository": source, "backup_path": plan["backup_path"], "target_repository": None, "created": False}
    try:
        owner, name = source.split("/", 1)
        tname = os.environ.get("TARGET_NAME") or f"{name}-restaurado"
        if not NAME_RE.match(tname) or set(tname) == {"."} or tname.endswith(".git"):
            raise RestoreError(42, "TARGET_NAME_INVALID", "nome do repositório alvo inválido")
        target = f"{owner}/{tname}"
        state["target_repository"] = target
        if target.lower() == source.lower():
            raise RestoreError(42, "TARGET_IS_SOURCE", "o alvo não pode ser o repositório de origem")

        # 1. Integridade do bundle, como no RESTAURAR.txt: clone espelho, bundle verify e refs contra o refs.tsv.
        pkg = RESTORE_DIR / "package"
        mirror = RESTORE_DIR / "mirror.git"
        code, out = git("clone", "--mirror", str((pkg / "source.bundle").resolve()), str(mirror.resolve()))
        if code:
            raise RestoreError(41, "BUNDLE_INVALID", "não foi possível clonar o bundle: " + out.strip()[-160:])
        code, out = git("bundle", "verify", str((pkg / "source.bundle").resolve()), cwd=mirror)
        if code:
            raise RestoreError(41, "BUNDLE_INVALID", "git bundle verify falhou: " + out.strip()[-160:])
        expected = parse_refs((pkg / "refs.tsv").read_text())
        code, out = git("for-each-ref", "--format=%(refname)\t%(objectname)", cwd=mirror)
        actual = parse_refs(out)
        diff = sorted(r for r, sha in expected.items() if actual.get(r) != sha)
        if code or diff:
            raise RestoreError(41, "REFS_DIFFER_FROM_BUNDLE", f"{len(diff)} ref(s) do bundle diferem do refs.tsv (ex.: {', '.join(diff[:3])})")
        to_push = {r: sha for r, sha in expected.items() if r.startswith(PUSHABLE)}
        skipped = sorted(r for r in expected if not r.startswith(PUSHABLE))
        heads = [r for r in to_push if r.startswith("refs/heads/")]
        if not heads:
            raise RestoreError(40, "NO_BRANCHES", "o backup não tem nenhuma branch para restaurar")
        default_branch = choose_default_branch(plan, heads, mirror)

        # 2. Alvo: tem de NÃO existir (404). Qualquer outra resposta recusa, sem adivinhar.
        status, payload = gh("GET", f"/repos/{target}")
        if status == 200:
            raise RestoreError(42, "TARGET_EXISTS", f"o repositório {target} já existe; a restauração só cria repositórios novos")
        if status != 404:
            raise RestoreError(43, "TARGET_CHECK_FAILED", "não foi possível confirmar que o alvo não existe: " + short(status, payload))
        user_headers = {}
        status, user = gh("GET", "/user", headers_out=user_headers)
        if status != 200 or "login" not in user:
            raise RestoreError(43, "TOKEN_INVALID", "o token de escrita foi recusado: " + short(status, user))
        # Escopos do token (só os nomes): o GitHub os devolve num cabeçalho para token clássico; token fine-grained não tem o cabeçalho.
        raw_scopes = user_headers.get("x-oauth-scopes")
        state["token_scopes"] = sorted(x.strip() for x in raw_scopes.split(",") if x.strip()) if raw_scopes is not None else None
        print("RESTORE_TOKEN " + json.dumps({"scopes": state["token_scopes"]}), flush=True)

        # 3. Criação: sempre PRIVADO. Descrição e página vêm do metadado salvo (se houver).
        meta = plan.get("metadata") or {}
        body = {"name": tname, "private": True, "has_issues": True, "auto_init": False}
        for key in ("description", "homepage"):
            if meta.get(key):
                body[key] = meta[key]
        route = "/user/repos" if user["login"].lower() == owner.lower() else f"/orgs/{owner}/repos"
        status, created = gh("POST", route, body)
        if status == 422:
            raise RestoreError(42, "TARGET_EXISTS", f"o GitHub recusou a criação de {target} (nome já em uso ou inválido): " + short(status, created))
        if status == 201:
            # Criado: a partir daqui o alvo existe e, se algo falhar, ele fica registrado (e não é apagado).
            state["created"] = True
            state["target_repository"] = created.get("full_name", target)
            state["html_url"] = created.get("html_url")
        if status != 201 or str(created.get("full_name", "")).lower() != target.lower() or created.get("private") is not True:
            raise RestoreError(43, "REPO_CREATE_FAILED", "criação do repositório recusada ou inesperada: " + short(status, created))
        url = f"{GIT_BASE}/{created['full_name']}.git"

        # 4. Envio: só branches e tags, sem --force. refs/pull/* e outras ficam de fora.
        code, out = git("push", url, "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*", cwd=mirror)
        if code:
            raise RestoreError(44, "PUSH_FAILED", "envio de branches e tags falhou: " + out.strip()[-200:])
        if plan["has_lfs"]:
            tar = subprocess.run(["tar", "-xf", str((pkg / "lfs-objects.tar").resolve()), "-C", str(mirror.resolve())], capture_output=True, text=True)
            if tar.returncode:
                raise RestoreError(46, "LFS_FAILED", "não foi possível extrair o lfs-objects.tar: " + tar.stderr.strip()[-160:])
            code, out = git("lfs", "push", "--all", url, cwd=mirror)
            if code:
                raise RestoreError(46, "LFS_FAILED", "envio dos objetos LFS falhou: " + out.strip()[-200:])

        # 5. Ajustes que não derrubam a restauração: branch padrão e tópicos (falha vira nota).
        notes = []
        if default_branch:
            status, payload = gh("PATCH", f"/repos/{created['full_name']}", {"default_branch": default_branch})
            if status != 200:
                notes.append("não foi possível definir a branch padrão: " + short(status, payload))
        else:
            notes.append("a branch padrão não pôde ser determinada; ficou a que o GitHub escolheu")
        if meta.get("topics"):
            status, payload = gh("PUT", f"/repos/{created['full_name']}/topics", {"names": meta["topics"]})
            if status != 200:
                notes.append("não foi possível definir os tópicos: " + short(status, payload))
        if not meta:
            notes.append("o backup não traz repo-metadata.json: descrição, tópicos e branch padrão não foram restaurados")
        if (meta.get("visibility") or "private") != "private" or meta.get("private") is False:
            notes.append(f"a origem era {meta.get('visibility', 'pública')}; o alvo foi criado PRIVADO")

        # 6. Conferência: as refs do alvo precisam ser exatamente as enviadas (branches e tags do refs.tsv).
        code, out = git("ls-remote", url)
        remote = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith(PUSHABLE) and not ref.endswith("^{}"):
                remote[ref] = sha
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / "refs-expected.tsv").write_text("".join(f"{r}\t{s}\n" for r, s in sorted(to_push.items())))
        (EVIDENCE / "refs-restored.tsv").write_text("".join(f"{r}\t{s}\n" for r, s in sorted(remote.items())))
        if code or remote != to_push:
            bad = sorted(set(remote) ^ set(to_push) | {r for r in remote if r in to_push and remote[r] != to_push[r]})
            raise RestoreError(45, "REFS_DO_NOT_MATCH", f"{len(bad)} ref(s) do alvo não conferem com o refs.tsv (ex.: {', '.join(bad[:3])})")
        status, repo = gh("GET", f"/repos/{created['full_name']}")
        if default_branch and status == 200 and repo.get("default_branch") != default_branch:
            notes.append(f"a branch padrão do alvo é {repo.get('default_branch')}, não {default_branch}")
        # 7. Labels, milestones e issues, só depois de o git estar restaurado e conferido. Uma falha aqui não apaga nada:
        # o git fica como está, o resultado diz o que não voltou e o job termina com o código 48.
        # Ids do GraphQL que os Projects pedem: o do dono (usuário ou organização) e o do repositório criado.
        owner_id = user.get("node_id") if route == "/user/repos" else gh("GET", f"/orgs/{owner}")[1].get("node_id")
        ctx = {"repo_node_id": created.get("node_id"), "owner_id": owner_id, "token_scopes": state.get("token_scopes")}
        classes, mapping, issue_map = restore_data_classes(plan, created["full_name"], ctx)
        project_map = {str(r["source_number"]): {"number": r.get("target_number"), "url": r.get("url")}
                       for r in classes.get("projects", {}).get("projects", []) if r.get("target_number") is not None}
        (EVIDENCE / "restore-map.json").write_text(json.dumps(
            {"schema": "restore-map/1", "source_repository": source, "target_repository": created["full_name"],
             "milestones": {str(old): new for old, new in sorted(mapping.items())},
             "issues": {str(old): new for old, new in sorted(issue_map.items())}, "projects": project_map}, indent=2, sort_keys=True) + "\n")
        data = {"status": "OK", **state, "default_branch": default_branch, "heads": len(heads),
                "tags": len(to_push) - len(heads), "skipped_refs": len(skipped), "skipped_examples": skipped[:5],
                "lfs": plan["has_lfs"], "notes": notes, "backup_request_id": plan["backup_request_id"], "classes": classes}
        write_result(data)
        print("RESTORE_OK " + json.dumps({k: data[k] for k in ("target_repository", "html_url", "default_branch", "heads", "tags", "skipped_refs", "lfs")}, sort_keys=True), flush=True)
        print("RESTORE_CLASSES " + json.dumps({c: v["status"] for c, v in classes.items()}, sort_keys=True), flush=True)
        failed = sorted(c for c, v in classes.items() if v["status"] == "FAILED")
        if failed:
            print(f"::error::DATA_RESTORE_FAILED: {', '.join(failed)} não voltaram como no backup; o alvo foi mantido.")
            b.result("FAILED", reason="DATA_RESTORE_FAILED", target_repository=state["target_repository"], created=True, failed_classes=failed)
            sys.exit(48)
    except RestoreError as err:
        fail(err, state)


# --- build-evidence -----------------------------------------------------------------------

def cmd_build_evidence():
    """Monta evidence.json e manifest.json (schemas 2.0) da restauração e valida ambos."""
    if os.environ.get("DESTINATION_VALIDATED") != "true":
        print("::error::Refusing to build evidence: destination was not validated.")
        sys.exit(21)
    request_id = os.environ["REQUEST_ID"]
    started = os.environ.get("STARTED_AT", b.now())
    pf = json.loads((EVIDENCE / "preflight.json").read_text())
    rfile = EVIDENCE / "restore-result.json"
    res = json.loads(rfile.read_text()) if rfile.exists() else {
        "status": "FAILED", "reason": "NO_RESULT", "message": "o passo de restauração não produziu resultado"}
    plan_file = RESTORE_DIR / "plan.json"
    plan = json.loads(plan_file.read_text()) if plan_file.exists() else {}
    source = res.get("source_repository") or plan.get("source_repository") or "unknown"
    target = res.get("target_repository") or "not-created"
    ok = res.get("status") == "OK"
    skipped = int(res.get("skipped_refs", 0)) if ok else 0

    if not ok:
        disposition, limitation = "FAILED", f"{res.get('reason')}: {res.get('message')}"
    elif skipped:
        disposition = "PARTIALLY-PRESERVED"
        limitation = (f"{skipped} ref(s) fora de refs/heads e refs/tags (por exemplo {', '.join(res.get('skipped_examples', [])[:2])}) "
                      "não podem ser enviadas de volta ao GitHub; branches e tags foram restauradas e conferidas.")
    else:
        disposition, limitation = "PRESERVED", None
    evidence_files = [n for n in ("refs-expected.tsv", "refs-restored.tsv", "restore-result.json", "preflight.json") if (EVIDENCE / n).exists()]
    objects = [{"object_class": "git", "object_id": f"{target}", "disposition": disposition, "evidence": evidence_files,
                "limitation": limitation}]
    limitations = [{"limitation_id": "LIM-restore-scope", "restriction_id": None, "object_class": None,
                    "description": "This stage restores git (branches, tags, LFS), labels, milestones, issues (with comments) and the sub-issue and dependency relations between "
                                   "restored issues, and recreates the projects as new private projects (without workflow rules). Every other class kept in the backup package is NOT restored; see RESTAURAR.txt."}]
    # Labels e milestones: o resultado de cada classe vem do restore; sem ele (git não restaurado), NOT-VERIFIED.
    planned = plan.get("classes", {})
    results = res.get("classes", {}) if ok else {}
    data_files = evidence_files + [n for n in ("restore-map.json", "RESTAURAR-PROJETO.txt") if (EVIDENCE / n).exists()]
    unreconciled = []
    for cls in DATA_CLASSES:
        r = results.get(cls)
        if r is None:
            reason = (planned.get(cls) or {}).get("reason") if planned.get(cls, {}).get("status") == "NOT_VERIFIED" else None
            r = {"status": "NOT_VERIFIED", "reason": reason or "a restauração não chegou a esta classe"}
        object_id = f"{target}#{cls}"
        if r["status"] in ("OK", "PARTIAL"):
            # Projects: as regras dos workflows e o agrupamento das views não voltam, então nunca é "equivalente".
            disp = "PRESERVED-AS-EQUIVALENT-REPRESENTATION" if r["status"] == "OK" and cls != "projects" else "PARTIALLY-PRESERVED"
            lim = RESTORE_LIMITATIONS[cls]
            if cls == "projects":
                skipped = [f"{p.get('source_number')}: {x['title']} ({x['reason']})" for p in r.get("projects", []) for x in p.get("items", {}).get("skipped", [])]
                if skipped:
                    lim += " Items not linked: " + "; ".join(skipped[:5]) + "."
            if r["status"] == "PARTIAL":
                lim += f" {r.get('skipped')} relation(s) were not restored: " + "; ".join(r.get("skipped_examples", [])[:5]) + "."
            if cls == "labels" and r.get("extras"):
                lim += f" Default GitHub labels that do not exist in the source remain in the target: {', '.join(r['extras'][:12])}."
            if cls in ("milestones", "issues") and r.get("numbers_changed"):
                lim += f" {cls[:-1]} numbers that changed (source: {', '.join(str(n) for n in r['numbers_changed'][:12])}); the map is in restore-map.json."
            if r.get("notes"):
                lim += " " + "; ".join(r["notes"][:3]) + "."
            limitations.append({"limitation_id": f"LIM-{cls}-restore", "description": lim, "restriction_id": None, "object_class": cls})
        elif r["status"] == "FAILED":
            disp, lim = "FAILED", f"{r.get('reason')}"
        else:
            disp, lim = "NOT-VERIFIED", f"Not restored: {r['reason']}."
            unreconciled.append(cls)
            limitations.append({"limitation_id": f"LIM-{cls}-not-restored", "description": lim, "restriction_id": None, "object_class": cls})
        objects.append({"object_class": cls, "object_id": object_id, "disposition": disp, "evidence": data_files, "limitation": lim})
    if limitation and disposition != "FAILED":
        limitations.append({"limitation_id": "LIM-git-refs", "description": limitation, "restriction_id": None, "object_class": "git"})
    for i, note in enumerate(res.get("notes", []), 1):
        limitations.append({"limitation_id": f"LIM-restore-note-{i}", "description": note, "restriction_id": None, "object_class": "git"})

    pf_ev = {"status": pf["status"],
             "gaps": [{k: g[k] for k in ("object_class", "gap_classification", "limitation", "preservation_impact", "resulting_restriction")} for g in pf["gaps"]],
             "factual_inventory_pending": [{"object_class": p["object_class"], "pending_fact": p["pending_fact"]} for p in pf["factual_inventory_pending"]],
             "hitl_decision": pf["hitl_decision"], "accepted_restrictions": pf["accepted_restrictions"]}
    scope = {"backup_path": os.environ["BACKUP_PATH"], "source_repository": source, "target_repository": target,
             "destination": os.environ["DESTINATION"]}
    evidence = {"schema_version": "2.0", "request_id": request_id, "capability_id": "restore_repository", "source_scope": scope,
                "captured_at": b.now(), "capability_preflight": pf_ev, "objects": objects,
                "execution": {"source_repository_mode": "READ_ONLY", "destination_validated": True, "authorization_request_id": request_id,
                              "restrictions_reconciled": True,
                              "temporal_preservation": {"mrbi": None, "crr": None, "notes": ["Temporal classes are not restored by this stage."]}}}
    jsonschema.validate(evidence, b.load("backup/schemas/evidence.schema.yaml"))
    (EVIDENCE / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")

    arts = [{"path": p.name, "sha256": b.sha256(p), "object_class": "evidence", "bytes": p.stat().st_size}
            for p in sorted(EVIDENCE.iterdir()) if p.is_file() and p.name != "manifest.json"]
    dispositions = [o["disposition"] for o in objects]
    status = "FAILED" if "FAILED" in dispositions else "COMPLETE_WITH_EXCEPTIONS"
    manifest = {"schema_version": "2.0", "request_id": request_id, "capability_id": "restore_repository", "scope": scope,
                "started_at": started, "completed_at": b.now(), "status": status,
                "capability_preflight": {"status": pf["status"], "hitl_decision": pf["hitl_decision"], "accepted_restrictions": pf["accepted_restrictions"]},
                "artifacts": arts,
                "reconciliation": {"preserved": dispositions.count("PRESERVED"), "equivalent": dispositions.count("PRESERVED-AS-EQUIVALENT-REPRESENTATION"),
                                   "partial": dispositions.count("PARTIALLY-PRESERVED"), "non_exportable": 0, "failed": dispositions.count("FAILED"),
                                   "not_verified": dispositions.count("NOT-VERIFIED"),
                                   "unreconciled_object_classes": unreconciled, "restrictions_reconciled": True},
                "limitations": limitations}
    jsonschema.validate(manifest, b.load("backup/schemas/backup-manifest.schema.yaml"))
    (EVIDENCE / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    b.result(status, evidence_sha256=b.sha256(EVIDENCE / "evidence.json"), manifest_sha256=b.sha256(EVIDENCE / "manifest.json"),
             reconciliation=manifest["reconciliation"], target_repository=target, source_repository=source)
    if status == "FAILED":
        sys.exit(1)


COMMANDS = {"fetch": cmd_fetch, "restore": cmd_restore, "build-evidence": cmd_build_evidence}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(64)
    for needed in ("REQUEST_ID", "BACKUP_PATH"):
        if not os.environ.get(needed):
            print(f"::error::Variável ausente: {needed}")
            sys.exit(64)
    COMMANDS[sys.argv[1]]()
