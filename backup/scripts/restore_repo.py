#!/usr/bin/env python3
"""Restaura um backup do BKP_REPO (OneDrive) para um repositório NOVO do GitHub (RST_REPO).

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
                    FAILED e cuja classe git foi preservada. Grava restore-work/plan.json.
    restore         confere o bundle, define o alvo (<dono>/<nome>-restaurado, ou TARGET_NAME), recusa se o
                    alvo for a origem ou já existir, cria o repositório PRIVADO, envia SÓ refs/heads/* e
                    refs/tags/* (sem --force, sem refs/pull/*), envia o LFS, define a branch padrão e confere
                    as refs do alvo contra o refs.tsv. Grava evidence/restore-result.json.
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
    - Sucesso do workflow NÃO prova restauração: a prova é o manifest validado e as refs conferidas.
"""
import base64
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

RESTORE_DIR = pathlib.Path(os.environ.get("RESTORE_DIR", "restore-work"))
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "evidence"))
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GIT_BASE = os.environ.get("GITHUB_GIT_BASE", "https://github.com").rstrip("/")

# Arquivos do pacote: os dois primeiros são obrigatórios; os outros, só se existirem no backup.
REQUIRED = ("source.bundle", "refs.tsv")
OPTIONAL = ("lfs-objects.tar", "repo-metadata.json")
# Nome do alvo: letras, números, ponto, hífen e sublinhado (regra do GitHub e do schema da linha).
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
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

def gh(method, path, body=None, token=None, retries=3):
    """Chamada à API do GitHub. Devolve (status, json). Repete em 502/503/504. Nunca imprime o token."""
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
        plan = {"source_repository": source, "backup_request_id": ev["request_id"], "backup_path": base,
                "backup_status": man["status"], "git_disposition": git_obj["disposition"], "files": files,
                "has_lfs": "lfs-objects.tar" in files, "metadata": metadata}
        (RESTORE_DIR / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        print("RESTORE_FETCH " + json.dumps({"source_repository": source, "backup_request_id": ev["request_id"],
                                             "files": sorted(files), "has_lfs": plan["has_lfs"],
                                             "has_metadata": bool(metadata)}, sort_keys=True), flush=True)
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
        status, user = gh("GET", "/user")
        if status != 200 or "login" not in user:
            raise RestoreError(43, "TOKEN_INVALID", "o token de escrita foi recusado: " + short(status, user))

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
        data = {"status": "OK", **state, "default_branch": default_branch, "heads": len(heads),
                "tags": len(to_push) - len(heads), "skipped_refs": len(skipped), "skipped_examples": skipped[:5],
                "lfs": plan["has_lfs"], "notes": notes, "backup_request_id": plan["backup_request_id"]}
        write_result(data)
        print("RESTORE_OK " + json.dumps({k: data[k] for k in ("target_repository", "html_url", "default_branch", "heads", "tags", "skipped_refs", "lfs")}, sort_keys=True), flush=True)
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
                    "description": "This stage restores only the git class (branches, tags, LFS). Labels, milestones, issues, projects and every "
                                   "other class kept in the backup package are NOT restored; see RESTAURAR.txt."}]
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
    status = "FAILED" if disposition == "FAILED" else "COMPLETE_WITH_EXCEPTIONS"
    manifest = {"schema_version": "2.0", "request_id": request_id, "capability_id": "restore_repository", "scope": scope,
                "started_at": started, "completed_at": b.now(), "status": status,
                "capability_preflight": {"status": pf["status"], "hitl_decision": pf["hitl_decision"], "accepted_restrictions": pf["accepted_restrictions"]},
                "artifacts": arts,
                "reconciliation": {"preserved": int(disposition == "PRESERVED"), "equivalent": 0, "partial": int(disposition == "PARTIALLY-PRESERVED"),
                                   "non_exportable": 0, "failed": int(disposition == "FAILED"), "not_verified": 0,
                                   "unreconciled_object_classes": [], "restrictions_reconciled": True},
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
