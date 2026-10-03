#!/usr/bin/env python3
"""Teste local ponta a ponta da restauração (RST_REPO): restore_repo.py, onedrive.py (download) e bkp_repo.py.

O QUE PROVA
    Roda os subcomandos como o workflow roda, contra um OneDrive SIMULADO (OAuth, Graph com redirecionamento de
    download e hash), uma API do GitHub SIMULADA e repositórios Git bare locais como "GitHub":
    - fetch: baixa e confere o backup; recusa hash adulterado, SHA-256 divergente entre manifest e pacote, backup
      FAILED, backup que não é do BKP_REPO, pacote incompleto e caminho fora de <backup_path>/package/;
    - restore: cria o alvo PRIVADO, envia só branches e tags (não refs/pull/*), define a branch padrão e os tópicos,
      confere as refs; usa target_name; recusa alvo existente, alvo igual à origem e nome inválido SEM criar nada;
      recusa token inválido; registra o alvo parcial (sem apagar) em falha de envio, de LFS e de conferência;
    - labels e milestones (etapa 2): recriados pela API depois do git, conferidos campo a campo; labels padrão do GitHub
      que não existem na origem ficam e são só listadas; milestones em ordem de número com mapa antigo -> novo; milestone
      fechado; classe NOT-VERIFIED quando o backup não a preservou (sem escrever nada); falha vira saída 48 sem apagar o alvo;
      paginação, limite de taxa, divergência na leitura de volta; nunca DELETE;
    - evidência e manifest 2.0 da restauração (capability_id restore_repository), inclusive o caso FAILED;
    - preflight de três classes (git, labels, milestones) sem o RESTORE_WRITE_TOKEN: BLOCKED com TRÊS restrições;
    - o token nunca aparece na saída nem nos arquivos de evidência.
    NÃO prova nada contra o GitHub ou o OneDrive reais, nem o envio LFS que funciona: isso só um run real prova.

COMO RODAR
    python3 backup/tests/test_restore_repo.py        (saída 0 = tudo certo; precisa de git)
"""
import hashlib
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "backup" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import onedrive  # noqa: E402

TOKEN = "ghp_SEGREDO_RESTAURACAO_789"
LOGIN = "dono"
SOURCE = "dono/teste"
BASE = "backups/teste/req-20260101-001"


def run(cmd, cwd=None, env=None):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (cmd, proc.stdout, proc.stderr)
    return proc.stdout.strip()


def qx(data):
    """quickXorHash de uns bytes (o hash que o OneDrive pessoal devolve)."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
    try:
        return onedrive.quickxor_hash(f.name)
    finally:
        os.unlink(f.name)


class FakeOneDrive(http.server.BaseHTTPRequestHandler):
    """OAuth + Graph (item, conteúdo com redirecionamento) + blob pré-autenticado."""
    files = {}        # caminho relativo ao AppFolder -> bytes (o que o item informa)
    served = {}       # caminho -> bytes servidos no blob, se diferentes (adulteração)
    blob_auth = []    # cabeçalho Authorization recebido em cada requisição ao blob (deve ser sempre None)

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=None, ctype="application/json"):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(200, json.dumps({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}).encode())

    def do_GET(self):  # noqa: N802
        path = urllib.parse.unquote(self.path)
        cls = FakeOneDrive
        if path.startswith("/blob/"):
            rel = path[len("/blob/"):]
            cls.blob_auth.append(self.headers.get("Authorization"))        # o blob NÃO pode receber Authorization
            return self._send(200, cls.served.get(rel, cls.files.get(rel, b"")), ctype="application/octet-stream")
        marker = "/me/drive/special/approot:/"
        if not path.startswith(marker):
            return self._send(404, b'{"error": {"code": "itemNotFound"}}')
        rel = path[len(marker):]
        content = rel.endswith(":/content")
        rel = rel[:-len(":/content")] if content else rel
        if rel not in cls.files:
            return self._send(404, b'{"error": {"code": "itemNotFound", "message": "x"}}')
        if content:
            host = f"http://127.0.0.1:{self.server.server_address[1]}"
            return self._send(302, b"", {"Location": f"{host}/blob/{urllib.parse.quote(rel)}"})
        data = cls.files[rel]
        self._send(200, json.dumps({"id": rel, "size": len(data), "file": {"hashes": {"quickXorHash": qx(data)}}}).encode())


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    """API do GitHub mínima para a restauração. Cria repositórios bare em `git_root`."""
    git_root = None
    existing = set()
    created = []
    bodies = []
    calls = []
    topics = {}
    patched = {}
    bad_token = False
    target_status = None         # força o status da consulta ao alvo (GET /repos/<alvo>)
    user_status = None           # força o status de GET /user
    create_private = True        # o que a criação devolve em `private`
    hide_tag = None              # esconde uma tag do ls-remote (simula divergência)
    default_labels = ["bug", "documentation", "enhancement"]    # o que todo repositório novo já traz
    labels = {}                  # repo -> lista de labels
    milestones = {}              # repo -> lista de milestones
    label_fail = {}              # nome da label -> status de erro na escrita
    milestone_fail = {}          # título -> status de erro na escrita
    ignore_color = False         # o servidor ignora a cor ao atualizar uma label (a conferência tem de pegar)
    ignore_description = False   # o servidor ignora a descrição ao atualizar uma label
    ignore_state = False         # o servidor cria milestone sempre aberto (a restauração tem de fechar depois)
    due_shift = None             # muda o due_on devolvido (ex.: "T07:00:00Z" mesma data, ou "+1" outro dia)
    rate_limited = 0             # quantas escritas respondem 403 de limite de taxa antes de funcionar
    order = []                   # títulos de milestones na ordem de criação

    def log_message(self, *args):
        pass

    def _send(self, code, body=None):
        raw = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _auth(self):
        FakeGitHub.calls.append((self.command, self.path))
        if FakeGitHub.bad_token or self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"message": "Bad credentials"})
            return False
        return True

    def _paged(self, items):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        per, page = int(q.get("per_page", ["30"])[0]), int(q.get("page", ["1"])[0])
        self._send(200, items[(page - 1) * per: page * per])

    def _data_route(self):
        """('labels'|'milestones', repo, resto) se o caminho é de labels ou milestones de um repositório existente."""
        path = urllib.parse.urlparse(self.path).path
        parts = path.split("/")
        if len(parts) >= 5 and parts[1] == "repos" and parts[4] in ("labels", "milestones"):
            return parts[4], f"{parts[2]}/{parts[3]}", [urllib.parse.unquote(x) for x in parts[5:]]
        return None

    def _data_write(self, kind, repo, rest, body):
        cls = FakeGitHub
        if cls.rate_limited > 0:
            cls.rate_limited -= 1
            return self._send(403, {"message": "API rate limit exceeded"})
        if kind == "labels":
            items = cls.labels.setdefault(repo, [])
            name = body.get("name") or (rest[0] if rest else "")
            if self.command == "POST":
                if cls.label_fail.get(name):
                    return self._send(cls.label_fail[name], {"message": "boom"})
                if any(l["name"].lower() == name.lower() for l in items):
                    return self._send(422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]})
                items.append({"name": name, "color": body["color"], "description": body.get("description") or None})
                return self._send(201, items[-1])
            item = next((l for l in items if l["name"] == rest[0]), None)
            if item is None:
                return self._send(404, {"message": "Not Found"})
            if cls.label_fail.get(body.get("new_name")):
                return self._send(cls.label_fail[body["new_name"]], {"message": "boom"})
            item["name"] = body.get("new_name", item["name"])
            if not cls.ignore_color:
                item["color"] = body["color"]
            if not cls.ignore_description:
                item["description"] = body.get("description") or None
            return self._send(200, item)
        items = cls.milestones.setdefault(repo, [])
        if self.command == "POST":
            if cls.milestone_fail.get(body.get("title")):
                return self._send(cls.milestone_fail[body["title"]], {"message": "boom"})
            due = body.get("due_on")
            if due and cls.due_shift == "same-day":
                due = due[:10] + "T07:00:00Z"
            elif due and cls.due_shift == "other-day":
                due = "2099-01-01T07:00:00Z"
            elif not due and cls.due_shift == "add-due":
                due = "2099-01-01T07:00:00Z"
            m = {"number": len(items) + 1, "title": body["title"], "description": body.get("description") or None,
                 "state": "open" if cls.ignore_state else body.get("state", "open"), "due_on": due}
            items.append(m)
            cls.order.append(body["title"])
            return self._send(201, m)
        item = next((m for m in items if str(m["number"]) == rest[0]), None)
        if item is None:
            return self._send(404, {"message": "Not Found"})
        if cls.ignore_state == "always":
            body = {k: v for k, v in body.items() if k != "state"}
        item.update({k: v for k, v in body.items() if k in ("state", "title", "description", "due_on")})
        return self._send(200, item)

    def do_GET(self):  # noqa: N802
        if not self._auth():
            return
        cls = FakeGitHub
        route = self._data_route()
        if route:
            kind, repo, _ = route
            items = cls.labels.get(repo, []) if kind == "labels" else cls.milestones.get(repo, [])
            return self._paged(items)
        if self.path == "/user":
            return self._send(cls.user_status or 200, {"login": LOGIN} if not cls.user_status else {"message": "x"})
        name = self.path[len("/repos/"):]
        if cls.target_status and self.path != f"/repos/{SOURCE}":
            return self._send(cls.target_status, {"message": "x"})
        if self.path.startswith("/repos/") and name.lower() in cls.existing:
            return self._send(200, {"full_name": name, "private": True, "default_branch": cls.patched.get(name, "main")})
        self._send(404, {"message": "Not Found"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return
        body = self._body()
        cls = FakeGitHub
        route = self._data_route()
        if route:
            return self._data_write(route[0], route[1], route[2], body)
        owner = LOGIN if self.path == "/user/repos" else self.path.split("/")[2]
        full = f"{owner}/{body['name']}"
        cls.bodies.append(body)
        if full.lower() in cls.existing:
            return self._send(422, {"message": "name already exists on this account"})
        bare = pathlib.Path(cls.git_root) / f"{full}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--bare", "-q", str(bare)])
        if cls.hide_tag:
            run(["git", "config", "uploadpack.hideRefs", f"refs/tags/{cls.hide_tag}"], cwd=bare)
        cls.existing.add(full.lower())
        cls.created.append(full)
        cls.labels[full] = [{"name": n, "color": "ededed", "description": None} for n in cls.default_labels]
        cls.milestones[full] = []
        self._send(201, {"full_name": full, "private": cls.create_private, "html_url": f"https://example.invalid/{full}"})

    def do_PATCH(self):  # noqa: N802
        if not self._auth():
            return
        body = self._body()
        route = self._data_route()
        if route:
            return self._data_write(route[0], route[1], route[2], body)
        name = self.path[len("/repos/"):]
        FakeGitHub.patched[name] = body["default_branch"]
        bare = pathlib.Path(FakeGitHub.git_root) / f"{name}.git"
        run(["git", "symbolic-ref", "HEAD", f"refs/heads/{body['default_branch']}"], cwd=bare)
        self._send(200, {"full_name": name})

    def do_PUT(self):  # noqa: N802
        if not self._auth():
            return
        FakeGitHub.topics[self.path.split("/")[2] + "/" + self.path.split("/")[3]] = self._body()["names"]
        self._send(200, {"names": FakeGitHub.topics})


def start(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.od, cls.gh = start(FakeOneDrive), start(FakeGitHub)
        cls.pack = tempfile.TemporaryDirectory()
        cls.build_source(pathlib.Path(cls.pack.name))

    @classmethod
    def tearDownClass(cls):
        cls.od.shutdown()
        cls.gh.shutdown()
        cls.pack.cleanup()

    @classmethod
    def build_source(cls, root):
        """Repositório de origem com 2 branches, 1 tag e 1 ref de pull request; bundle e refs.tsv como no backup."""
        src, mirror = root / "src", root / "mirror.git"
        run(["git", "init", "-q", "-b", "main", str(src)])
        git = lambda *a: run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=src)
        (src / "a.txt").write_text("um\n")
        git("add", "."), git("commit", "-q", "-m", "um")
        git("tag", "v1")
        git("checkout", "-q", "-b", "feature"), (src / "b.txt").write_text("dois\n"), git("add", "."), git("commit", "-q", "-m", "dois")
        git("checkout", "-q", "main")
        git("update-ref", "refs/pull/1/head", git("rev-parse", "feature"))
        run(["git", "clone", "-q", "--mirror", str(src), str(mirror)])
        run(["git", "bundle", "create", str(root / "source.bundle"), "--all"], cwd=mirror)
        refs = run(["git", "for-each-ref", "--format=%(refname)\t%(objectname)\t%(objecttype)"], cwd=mirror)
        cls.refs_tsv = "\n".join(sorted(refs.splitlines())) + "\n"
        cls.bundle = (root / "source.bundle").read_bytes()
        cls.lfs = cls.build_lfs_source(root)

    @classmethod
    def build_lfs_source(cls, root):
        """Origem com um arquivo no Git LFS (ponteiro no commit, objeto em .git/lfs). None se o git-lfs não existe."""
        if subprocess.run(["git", "lfs", "version"], capture_output=True).returncode:
            return None
        src, mirror = root / "lsrc", root / "lmirror.git"
        run(["git", "init", "-q", "-b", "main", str(src)])
        run(["git", "lfs", "install", "--local"], cwd=src)
        run(["git", "lfs", "track", "*.bin"], cwd=src)
        content = b"conteudo grande no lfs\n" * 50
        (src / "grande.bin").write_bytes(content)
        git = lambda *a: run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=src)
        git("add", "."), git("commit", "-q", "-m", "lfs")
        run(["git", "clone", "-q", "--mirror", str(src), str(mirror)])
        run(["git", "bundle", "create", str(root / "lfs.bundle"), "--all"], cwd=mirror)
        refs = run(["git", "for-each-ref", "--format=%(refname)\t%(objectname)\t%(objecttype)"], cwd=mirror)
        # Como o backup: o tar leva lfs/objects (o diretório do mirror se chama "lfs"). Aqui, o do repositório de origem.
        run(["tar", "-C", str(src / ".git"), "-cf", str(root / "lfs-objects.tar"), "lfs/objects"])
        (root / "empty" / "lfs" / "objects").mkdir(parents=True)
        run(["tar", "-C", str(root / "empty"), "-cf", str(root / "empty.tar"), "lfs/objects"])
        return {"bundle": (root / "lfs.bundle").read_bytes(), "refs": "\n".join(sorted(refs.splitlines())) + "\n",
                "tar": (root / "lfs-objects.tar").read_bytes(), "empty_tar": (root / "empty.tar").read_bytes(),
                "oid": hashlib.sha256(content).hexdigest()}

    # --- o backup no OneDrive ---
    EQUIV = "PRESERVED-AS-EQUIVALENT-REPRESENTATION"
    LABELS = [{"name": "Bug", "color": "d73a4a", "description": "Algo não funciona"},
              {"name": "teste-backup", "color": "0e8a16", "description": None},
              {"name": "prioridade ✔ alta", "color": "B60205", "description": "acentuação e símbolo"}]
    MILESTONES = [{"number": 1, "title": "Marco de teste", "state": "open", "description": "primeiro", "due_on": "2026-12-31T08:00:00Z"},
                  {"number": 3, "title": "Marco antigo", "state": "closed", "description": None, "due_on": None}]

    def make_backup(self, *, metadata=True, lfs_tar=None, bundle=None, refs_tsv=None, status="COMPLETE_WITH_EXCEPTIONS",
                    capability="backup_repository", git_disposition="PRESERVED", drop=(), bad_manifest_hash=None,
                    data=True, labels=None, milestones=None, label_disposition=None, milestone_disposition=None, raw=None):
        package = {"source.bundle": bundle or self.bundle, "refs.tsv": (refs_tsv or self.refs_tsv).encode()}
        if data:
            package["api-labels.json"] = json.dumps(self.LABELS if labels is None else labels).encode()
            package["api-milestones.json"] = json.dumps(self.MILESTONES if milestones is None else milestones).encode()
        for name, content in (raw or {}).items():
            package[name] = content
        if metadata:
            package["repo-metadata.json"] = json.dumps({
                "schema": "repo-metadata/1", "full_name": SOURCE, "visibility": "public", "private": False, "description": "Teste",
                "topics": ["backup"], "default_branch": "feature"}).encode()
        if lfs_tar:
            package["lfs-objects.tar"] = lfs_tar
        for name in drop:
            package.pop(name, None)
        pkg_entries = [{"remote_path": f"{BASE}/package/{n}", "sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)} for n, d in package.items()]
        evidence = {
            "schema_version": "2.0", "request_id": "req-20260101-001", "capability_id": capability,
            "source_scope": {"source_repository": SOURCE, "destination": "backups/teste"}, "captured_at": "2026-01-01T00:00:00Z",
            "capability_preflight": {"status": "PASS", "gaps": [], "factual_inventory_pending": [], "hitl_decision": "NOT_REQUIRED", "accepted_restrictions": []},
            "objects": [{"object_class": "git", "object_id": SOURCE, "disposition": git_disposition, "evidence": ["refs.tsv"]}]
                       + ([{"object_class": "labels", "object_id": f"{SOURCE}#labels", "disposition": label_disposition or self.EQUIV,
                            "evidence": ["api-labels.json"]},
                           {"object_class": "milestones", "object_id": f"{SOURCE}#milestones", "disposition": milestone_disposition or self.EQUIV,
                            "evidence": ["api-milestones.json"]}] if data else []),
            "execution": {"source_repository_mode": "READ_ONLY", "destination_validated": True, "authorization_request_id": "req-20260101-001",
                          "restrictions_reconciled": True}}
        ev_bytes = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode()
        arts = [{"path": e["remote_path"], "sha256": e["sha256"], "object_class": "preservation-package", "bytes": e["bytes"]} for e in pkg_entries]
        arts.append({"path": "evidence.json", "sha256": hashlib.sha256(ev_bytes).hexdigest(), "object_class": "evidence", "bytes": len(ev_bytes)})
        if bad_manifest_hash:
            for a in arts:
                if a["path"].endswith(bad_manifest_hash):
                    a["sha256"] = "0" * 64
        manifest = {
            "schema_version": "2.0", "request_id": "req-20260101-001", "capability_id": capability, "scope": evidence["source_scope"],
            "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:01:00Z", "status": status,
            "capability_preflight": {"status": "PASS", "hitl_decision": "NOT_REQUIRED", "accepted_restrictions": []}, "artifacts": arts,
            "reconciliation": {"preserved": 1, "equivalent": 0, "partial": 0, "non_exportable": 0, "failed": 0, "not_verified": 0,
                               "unreconciled_object_classes": [], "restrictions_reconciled": True},
            "limitations": [{"limitation_id": "LIM-x", "description": "x"}]}
        files = {f"{BASE}/package/{n}": d for n, d in package.items()}
        files[f"{BASE}/evidence/evidence.json"] = ev_bytes
        files[f"{BASE}/evidence/manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        files[f"{BASE}/evidence/onedrive-package.json"] = json.dumps(pkg_entries).encode()
        FakeOneDrive.files, FakeOneDrive.served = files, {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.git_root = self.root / "github"
        FakeGitHub.git_root = self.git_root
        FakeGitHub.existing, FakeGitHub.created, FakeGitHub.bodies, FakeGitHub.calls = {SOURCE}, [], [], []
        FakeGitHub.topics, FakeGitHub.patched, FakeGitHub.bad_token, FakeGitHub.create_private, FakeGitHub.hide_tag = {}, {}, False, True, None
        FakeGitHub.target_status = FakeGitHub.user_status = None
        FakeGitHub.default_labels = ["bug", "documentation", "enhancement"]
        FakeGitHub.labels, FakeGitHub.milestones, FakeGitHub.label_fail, FakeGitHub.milestone_fail = {}, {}, {}, {}
        FakeGitHub.ignore_color = FakeGitHub.ignore_state = FakeGitHub.ignore_description = False
        FakeGitHub.due_shift, FakeGitHub.rate_limited, FakeGitHub.order = None, 0, []
        FakeOneDrive.blob_auth = []
        self.make_backup()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CAPABILITY_ID", "RESTORE_", "PREFLIGHT", "TARGET_"))}
        self.env.update(
            REQUEST_ID="req-20260102-001", BACKUP_PATH=BASE, DESTINATION=f"{BASE}/restores", DESTINATION_VALIDATED="true",
            EVIDENCE_DIR=str(self.root / "evidence"), RESTORE_DIR=str(self.root / "work"), RESTORE_TOKEN=TOKEN, RUNNER_TEMP=str(self.root),
            ONEDRIVE_CLIENT_ID="cid", ONEDRIVE_REFRESH_TOKEN="RT", GITHUB_REPOSITORY="dono/repo_backup", ONEDRIVE_RETRY_DELAY="0", RESTORE_WRITE_DELAY="0", RESTORE_RATE_WAIT="0",
            ONEDRIVE_AUTH_BASE=f"http://127.0.0.1:{self.od.server_address[1]}", ONEDRIVE_GRAPH_BASE=f"http://127.0.0.1:{self.od.server_address[1]}",
            GITHUB_API_URL=f"http://127.0.0.1:{self.gh.server_address[1]}", GITHUB_GIT_BASE=f"file://{self.git_root}", RESTORE_RETRY_DELAY="0")
        self.outputs = []

    def tearDown(self):
        self.tmp.cleanup()

    def restore(self, sub, **env):
        proc = subprocess.run([sys.executable, str(SCRIPTS / "restore_repo.py"), sub], env=dict(self.env, **env), capture_output=True, text=True, cwd=ROOT)
        self.outputs.append(proc.stdout + proc.stderr)
        return proc

    def bkp(self, sub, **env):
        proc = subprocess.run([sys.executable, str(SCRIPTS / "bkp_repo.py"), sub], env=dict(self.env, CAPABILITY_ID="restore_repository", **env),
                              capture_output=True, text=True, cwd=ROOT)
        self.outputs.append(proc.stdout + proc.stderr)
        return proc

    @staticmethod
    def result(proc):
        marks = [l for l in proc.stdout.splitlines() if l.startswith("BKP_RESULT ")]
        return json.loads(marks[-1][len("BKP_RESULT "):]) if marks else {}

    def evidence(self, name):
        return json.loads((self.root / "evidence" / name).read_text())

    def bare_refs(self, full):
        out = run(["git", "for-each-ref", "--format=%(refname)"], cwd=self.git_root / f"{full}.git")
        return set(out.splitlines())

    def fetched(self):
        proc = self.restore("fetch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def assert_no_leak(self):
        text = "\n".join(self.outputs)
        for p in (self.root / "evidence").glob("*") if (self.root / "evidence").exists() else []:
            text += p.read_text(errors="replace")
        self.assertNotIn(TOKEN, text)


class FetchTest(Base):
    def test_baixa_e_confere_o_backup(self):
        self.fetched()
        plan = json.loads((self.root / "work" / "plan.json").read_text())
        self.assertEqual((plan["source_repository"], plan["backup_request_id"], plan["has_lfs"]), (SOURCE, "req-20260101-001", False))
        self.assertEqual(sorted(plan["files"]), ["api-labels.json", "api-milestones.json", "refs.tsv", "repo-metadata.json", "source.bundle"])
        self.assertEqual(plan["classes"], {"labels": {"status": "READY", "file": "api-labels.json", "count": 3},
                                           "milestones": {"status": "READY", "file": "api-milestones.json", "count": 2}})
        self.assertEqual((self.root / "work" / "package" / "source.bundle").read_bytes(), self.bundle)
        self.assertEqual(plan["metadata"]["default_branch"], "feature")
        # O download pré-autenticado (blob) é feito sem Authorization, uma vez por arquivo baixado.
        self.assertEqual(FakeOneDrive.blob_auth, [None] * 8)

    def test_hash_adulterado_no_onedrive_e_recusado_e_o_arquivo_apagado(self):
        FakeOneDrive.served[f"{BASE}/package/source.bundle"] = self.bundle + b"x"
        proc = self.restore("fetch")
        self.assertEqual(proc.returncode, 22)
        self.assertEqual(self.result(proc)["reason"], "ONEDRIVE_DOWNLOAD_FAILED")
        self.assertFalse((self.root / "work" / "package" / "source.bundle").exists())

    def test_arquivo_trocado_no_onedrive_depois_do_backup(self):
        # O OneDrive devolve o hash do arquivo que está lá (coerente com o download), mas o manifest registrou outro.
        FakeOneDrive.files[f"{BASE}/package/source.bundle"] = self.bundle + b"y"
        proc = self.restore("fetch")
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (41, "HASH_MISMATCH"))
        self.assertFalse((self.root / "work" / "package" / "source.bundle").exists())

    def test_sha256_do_manifest_diferente_do_pacote(self):
        self.make_backup(bad_manifest_hash="source.bundle")
        proc = self.restore("fetch")
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (41, "HASH_MISMATCH"))

    def test_backups_que_nao_sao_restauraveis(self):
        cases = [({"status": "FAILED"}, "BACKUP_NOT_COMPLETE"), ({"capability": "backup_projects"}, "NOT_A_REPOSITORY_BACKUP"),
                 ({"git_disposition": "FAILED"}, "GIT_NOT_PRESERVED"), ({"drop": ("source.bundle",)}, "BACKUP_PACKAGE_INCOMPLETE")]
        for kwargs, reason in cases:
            self.make_backup(**kwargs)
            proc = self.restore("fetch")
            self.assertEqual((proc.returncode, self.result(proc)["reason"]), (40, reason), kwargs)

    def test_caminho_do_backup_fora_do_registro(self):
        self.make_backup()
        other = "backups/outro/req-20260101-009"
        FakeOneDrive.files.update({k.replace(BASE, other): v for k, v in list(FakeOneDrive.files.items())})
        proc = self.restore("fetch", BACKUP_PATH=other)
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (40, "BACKUP_PATH_MISMATCH"))

    def test_backup_inexistente(self):
        proc = self.restore("fetch", BACKUP_PATH="backups/nao/existe")
        self.assertEqual(proc.returncode, 22)


class RestoreTest(Base):
    def go(self, **env):
        self.fetched()
        return self.restore("restore", **env)

    def test_restaura_para_um_repositorio_novo_e_privado(self):
        proc = self.go()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(FakeGitHub.created, ["dono/teste-restaurado"])
        body = FakeGitHub.bodies[0]
        self.assertEqual((body["private"], body["name"], body["description"], body["auto_init"]), (True, "teste-restaurado", "Teste", False))
        # Só branches e tags voltam; refs/pull/* não (e a origem era pública, mas o alvo é privado).
        self.assertEqual(self.bare_refs("dono/teste-restaurado"), {"refs/heads/main", "refs/heads/feature", "refs/tags/v1"})
        self.assertEqual(FakeGitHub.patched["dono/teste-restaurado"], "feature")           # branch padrão do metadado
        self.assertEqual(FakeGitHub.topics["dono/teste-restaurado"], ["backup"])
        res = self.evidence("restore-result.json")
        self.assertEqual((res["status"], res["heads"], res["tags"], res["skipped_refs"], res["default_branch"]), ("OK", 2, 1, 1, "feature"))
        self.assertTrue(any("PRIVADO" in n for n in res["notes"]))
        self.assertEqual((self.root / "evidence" / "refs-expected.tsv").read_text(), (self.root / "evidence" / "refs-restored.tsv").read_text())
        self.assert_no_leak()

    def test_target_name_opcional(self):
        proc = self.go(TARGET_NAME="copia-de-teste")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(FakeGitHub.created, ["dono/copia-de-teste"])

    def test_sem_metadados_usa_o_head_do_bundle_e_avisa(self):
        self.make_backup(metadata=False)
        proc = self.go()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(FakeGitHub.patched["dono/teste-restaurado"], "main")
        self.assertTrue(any("repo-metadata.json" in n for n in self.evidence("restore-result.json")["notes"]))
        self.assertNotIn("description", FakeGitHub.bodies[0])

    def test_alvo_existente_e_recusado_sem_criar_nada(self):
        FakeGitHub.existing.add("dono/teste-restaurado")
        proc = self.go()
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (42, "TARGET_EXISTS"))
        self.assertFalse(any(m == "POST" for m, _ in FakeGitHub.calls))
        self.assertFalse(self.evidence("restore-result.json")["created"])

    def test_alvo_igual_a_origem_e_nomes_invalidos(self):
        for name, reason in (("teste", "TARGET_IS_SOURCE"), ("a/b", "TARGET_NAME_INVALID"), ("..", "TARGET_NAME_INVALID"), ("x.git", "TARGET_NAME_INVALID")):
            FakeGitHub.calls.clear()
            proc = self.go(TARGET_NAME=name)
            self.assertEqual((proc.returncode, self.result(proc)["reason"]), (42, reason), name)
            self.assertEqual(FakeGitHub.calls, [], name)                       # nem sequer consultou a API
        self.assertEqual(FakeGitHub.created, [])

    def test_existencia_do_alvo_nao_confirmada_recusa_sem_criar(self):
        FakeGitHub.target_status = 403
        proc = self.go()
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (43, "TARGET_CHECK_FAILED"))
        self.assertFalse(any(m == "POST" for m, _ in FakeGitHub.calls))

    def test_token_recusado_em_user_nao_cria_nada(self):
        FakeGitHub.user_status = 401
        proc = self.go()
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (43, "TOKEN_INVALID"))
        self.assertFalse(any(m == "POST" for m, _ in FakeGitHub.calls))

    def test_refs_tsv_diferente_do_bundle_e_recusado_antes_de_criar(self):
        main = next(l for l in self.refs_tsv.splitlines() if l.startswith("refs/heads/main\t"))
        self.make_backup(refs_tsv=self.refs_tsv.replace(main, f"refs/heads/main\t{'f' * 40}\tcommit"))
        proc = self.go()
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (41, "REFS_DIFFER_FROM_BUNDLE"))
        self.assertEqual(FakeGitHub.created, [])

    def test_backup_sem_branches_e_recusado_antes_de_criar(self):
        only_tag = "".join(l + "\n" for l in self.refs_tsv.splitlines() if l.startswith("refs/tags/"))
        self.make_backup(refs_tsv=only_tag)
        proc = self.go()
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (40, "NO_BRANCHES"))
        self.assertEqual(FakeGitHub.created, [])

    def test_token_invalido_nao_cria_nada(self):
        FakeGitHub.bad_token = True
        proc = self.go()
        self.assertEqual(proc.returncode, 43)
        self.assertEqual(FakeGitHub.created, [])

    def test_criacao_que_devolve_publico_e_recusada_mas_registrada(self):
        FakeGitHub.create_private = False
        proc = self.go()
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (43, "REPO_CREATE_FAILED"))
        res = self.evidence("restore-result.json")
        self.assertTrue(res["created"])
        self.assertEqual(res["target_repository"], "dono/teste-restaurado")
        self.assertEqual(self.bare_refs("dono/teste-restaurado"), set())         # nada foi enviado

    def test_falha_no_envio_registra_o_alvo_parcial_sem_apagar(self):
        proc = self.go(GITHUB_GIT_BASE=f"file://{self.root / 'outro'}")
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (44, "PUSH_FAILED"))
        res = self.evidence("restore-result.json")
        self.assertEqual((res["created"], res["target_repository"]), (True, "dono/teste-restaurado"))
        self.assertFalse(any(m == "DELETE" for m, _ in FakeGitHub.calls))

    def test_refs_do_alvo_diferentes_do_refs_tsv(self):
        FakeGitHub.hide_tag = "v1"
        proc = self.go()
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (45, "REFS_DO_NOT_MATCH"))
        self.assertTrue(self.evidence("restore-result.json")["created"])

    def lfs_backup(self, tar_key):
        if not self.lfs:
            self.skipTest("git-lfs não está instalado")
        self.make_backup(bundle=self.lfs["bundle"], refs_tsv=self.lfs["refs"], lfs_tar=self.lfs[tar_key])

    def test_lfs_objetos_chegam_ao_alvo(self):
        self.lfs_backup("tar")
        proc = self.go()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        oid = self.lfs["oid"]
        self.assertTrue((self.git_root / "dono" / "teste-restaurado.git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid).exists())
        self.assertTrue(self.evidence("restore-result.json")["lfs"])

    def test_lfs_com_objeto_faltando_falha_depois_de_enviar_as_refs(self):
        self.lfs_backup("empty_tar")
        proc = self.go()
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (46, "LFS_FAILED"))
        self.assertIn("refs/heads/main", self.bare_refs("dono/teste-restaurado"))
        self.assertTrue(self.evidence("restore-result.json")["created"])

    def test_bundle_adulterado_com_hash_coerente_e_recusado_antes_de_criar(self):
        bad = self.bundle[:-20] + b"x" * 20
        self.make_backup()
        FakeOneDrive.files[f"{BASE}/package/source.bundle"] = bad
        entries = json.loads(FakeOneDrive.files[f"{BASE}/evidence/onedrive-package.json"])
        man = json.loads(FakeOneDrive.files[f"{BASE}/evidence/manifest.json"])
        for e in entries:
            if e["remote_path"].endswith("source.bundle"):
                e["sha256"], e["bytes"] = hashlib.sha256(bad).hexdigest(), len(bad)
        for a in man["artifacts"]:
            if a["path"].endswith("source.bundle"):
                a["sha256"], a["bytes"] = hashlib.sha256(bad).hexdigest(), len(bad)
        FakeOneDrive.files[f"{BASE}/evidence/onedrive-package.json"] = json.dumps(entries).encode()
        FakeOneDrive.files[f"{BASE}/evidence/manifest.json"] = json.dumps(man).encode()
        proc = self.go()
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (41, "BUNDLE_INVALID"))
        self.assertEqual(FakeGitHub.created, [])


class DataClassesTest(Base):
    """Etapa 2: labels e milestones."""

    def go(self, **env):
        self.fetched()
        return self.restore("restore", **env)

    def target(self):
        return "dono/teste-restaurado"

    def again(self, **env):
        """Segunda restauração no mesmo teste: o alvo anterior sai da lista de existentes e a pasta de trabalho é refeita."""
        import shutil
        FakeGitHub.existing.discard("dono/teste-restaurado")
        shutil.rmtree(self.root / "work", ignore_errors=True)
        return self.go(**env)

    def test_restaura_labels_e_milestones_e_confere(self):
        proc = self.go()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        labels = {l["name"]: l for l in FakeGitHub.labels[self.target()]}
        # "Bug" já existia como "bug": o nome é acertado (maiúscula) e cor e descrição igualadas; as outras são criadas.
        self.assertEqual((labels["Bug"]["color"], labels["Bug"]["description"]), ("d73a4a", "Algo não funciona"))
        self.assertEqual((labels["teste-backup"]["description"], labels["prioridade ✔ alta"]["color"]), (None, "B60205"))
        self.assertNotIn("bug", labels)
        res = self.evidence("restore-result.json")
        self.assertEqual(res["classes"]["labels"], {"status": "OK", "expected": 3, "created": 2, "updated": 1, "extras": ["documentation", "enhancement"]})
        # Milestones: 1 aberto com prazo e 3 fechado (sem o 2): os números novos são 1 e 2; o mapa fica registrado.
        ms = FakeGitHub.milestones[self.target()]
        self.assertEqual([(m["number"], m["title"], m["state"], m["due_on"]) for m in ms],
                         [(1, "Marco de teste", "open", "2026-12-31T08:00:00Z"), (2, "Marco antigo", "closed", None)])
        self.assertEqual(res["classes"]["milestones"]["numbers_changed"], [3])
        self.assertEqual(self.evidence("restore-map.json")["milestones"], {"1": 1, "3": 2})
        self.assertIn("RESTORE_CLASSES", proc.stdout)
        # Nunca DELETE, e as labels padrão que não existem na origem continuam no alvo.
        self.assertNotIn("DELETE", {m for m, _ in FakeGitHub.calls})
        self.assertTrue({"documentation", "enhancement"} <= set(labels))
        self.assert_no_leak()

    def test_evidencia_com_disposicao_equivalente_e_limitacoes(self):
        self.assertEqual(self.go().returncode, 0)
        self.assertEqual(self.bkp("preflight", RESTORE_TOKEN_OUTCOME="success").returncode, 0)
        proc = self.restore("build-evidence")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        ev, man = self.evidence("evidence.json"), self.evidence("manifest.json")
        objs = {o["object_class"]: o for o in ev["objects"]}
        self.assertEqual((objs["git"]["disposition"], objs["labels"]["disposition"], objs["milestones"]["disposition"]),
                         ("PARTIALLY-PRESERVED", Base.EQUIV, Base.EQUIV))        # a origem tem refs/pull/*, que não voltam
        self.assertEqual(objs["labels"]["object_id"], "dono/teste-restaurado#labels")
        self.assertIn("restore-map.json", objs["milestones"]["evidence"])
        lims = {l["limitation_id"]: l["description"] for l in man["limitations"]}
        self.assertIn("documentation, enhancement", lims["LIM-labels-restore"])           # padrão do GitHub listadas
        self.assertIn("internal label id is not preserved", lims["LIM-labels-restore"])
        self.assertIn("3", lims["LIM-milestones-restore"])                                # número que mudou
        self.assertIn("Issues, projects", lims["LIM-restore-scope"])
        self.assertNotIn("labels, milestones", lims["LIM-restore-scope"].replace("labels and milestones", ""))
        self.assertEqual((man["status"], man["reconciliation"]["equivalent"], man["reconciliation"]["preserved"]), ("COMPLETE_WITH_EXCEPTIONS", 2, 0))

    def test_milestones_criados_em_ordem_de_numero(self):
        self.make_backup(milestones=[
            {"number": 5, "title": "E", "state": "open"}, {"number": 2, "title": "B", "state": "open"},
            {"number": 9, "title": "I", "state": "open"}])
        self.assertEqual(self.go().returncode, 0)
        self.assertEqual(FakeGitHub.order, ["B", "E", "I"])
        self.assertEqual(self.evidence("restore-map.json")["milestones"], {"2": 1, "5": 2, "9": 3})

    def test_numeracao_sem_buracos_nao_acusa_mudanca(self):
        self.make_backup(milestones=[{"number": 1, "title": "A", "state": "open"}, {"number": 2, "title": "B", "state": "closed"}])
        self.assertEqual(self.go().returncode, 0)
        self.assertEqual(self.evidence("restore-result.json")["classes"]["milestones"]["numbers_changed"], [])

    def test_backup_sem_as_classes_fica_not_verified_e_nao_escreve(self):
        self.make_backup(data=False)
        proc = self.go()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        res = self.evidence("restore-result.json")
        self.assertEqual({c: v["status"] for c, v in res["classes"].items()}, {"labels": "NOT_VERIFIED", "milestones": "NOT_VERIFIED"})
        self.assertFalse([c for c in FakeGitHub.calls if "/labels" in c[1] or "/milestones" in c[1]])
        self.assertEqual(FakeGitHub.labels["dono/teste-restaurado"], [{"name": n, "color": "ededed", "description": None} for n in FakeGitHub.default_labels])
        self.assertEqual(self.bkp("preflight", RESTORE_TOKEN_OUTCOME="success").returncode, 0)
        self.assertEqual(self.restore("build-evidence").returncode, 0)
        ev, man = self.evidence("evidence.json"), self.evidence("manifest.json")
        self.assertEqual([o["disposition"] for o in ev["objects"][1:]], ["NOT-VERIFIED", "NOT-VERIFIED"])
        self.assertEqual((man["status"], man["reconciliation"]["not_verified"], man["reconciliation"]["unreconciled_object_classes"]),
                         ("COMPLETE_WITH_EXCEPTIONS", 2, ["labels", "milestones"]))
        self.assertIn("LIM-labels-not-restored", [l["limitation_id"] for l in man["limitations"]])

    def test_classe_que_o_backup_nao_preservou_nao_e_restaurada(self):
        self.make_backup(label_disposition="FAILED", milestone_disposition="NOT-VERIFIED")
        self.assertEqual(self.go().returncode, 0)
        classes = self.evidence("restore-result.json")["classes"]
        self.assertEqual({c: v["status"] for c, v in classes.items()}, {"labels": "NOT_VERIFIED", "milestones": "NOT_VERIFIED"})
        self.assertIn("FAILED", classes["labels"]["reason"])
        self.assertFalse([c for c in FakeGitHub.calls if "/labels" in c[1] or "/milestones" in c[1]])

    def test_so_uma_das_classes(self):
        self.make_backup(milestone_disposition="FAILED")
        self.assertEqual(self.go().returncode, 0)
        classes = self.evidence("restore-result.json")["classes"]
        self.assertEqual((classes["labels"]["status"], classes["milestones"]["status"]), ("OK", "NOT_VERIFIED"))
        self.assertEqual(FakeGitHub.milestones["dono/teste-restaurado"], [])

    def test_arquivo_ausente_ou_malformado_fica_not_verified(self):
        self.make_backup(drop=("api-labels.json",))
        self.assertEqual(self.go().returncode, 0)
        self.assertIn("não está no pacote", self.evidence("restore-result.json")["classes"]["labels"]["reason"])
        self.make_backup(raw={"api-milestones.json": b'{"nao": "e lista"}'})
        self.assertEqual(self.again().returncode, 0)
        self.assertIn("não pôde ser lido", self.evidence("restore-result.json")["classes"]["milestones"]["reason"])

    def test_hash_do_arquivo_de_labels_adulterado_recusa_antes_de_criar(self):
        FakeOneDrive.served[f"{BASE}/package/api-labels.json"] = b"[]"
        proc = self.restore("fetch")
        self.assertEqual(proc.returncode, 22)
        self.assertEqual(FakeGitHub.created, [])
        self.make_backup(bad_manifest_hash="api-milestones.json")
        proc = self.restore("fetch")
        self.assertEqual((proc.returncode, self.result(proc)["reason"]), (41, "HASH_MISMATCH"))
        self.assertEqual(FakeGitHub.created, [])

    def test_falha_ao_escrever_uma_label_sai_com_48_sem_apagar_nada(self):
        FakeGitHub.label_fail = {"teste-backup": 500}
        proc = self.go()
        self.assertEqual(proc.returncode, 48, proc.stdout + proc.stderr)
        self.assertEqual(self.result(proc)["reason"], "DATA_RESTORE_FAILED")
        res = self.evidence("restore-result.json")
        self.assertEqual((res["status"], res["classes"]["labels"]["status"], res["classes"]["milestones"]["status"]), ("OK", "FAILED", "OK"))
        self.assertIn("teste-backup", res["classes"]["labels"]["mismatches"])
        self.assertEqual(self.bare_refs("dono/teste-restaurado"), {"refs/heads/main", "refs/heads/feature", "refs/tags/v1"})   # git intacto
        self.assertIn("dono/teste-restaurado", FakeGitHub.existing)
        self.assertEqual(self.bkp("preflight", RESTORE_TOKEN_OUTCOME="success").returncode, 0)
        proc = self.restore("build-evidence")
        self.assertEqual(proc.returncode, 1)
        ev, man = self.evidence("evidence.json"), self.evidence("manifest.json")
        self.assertEqual([o["disposition"] for o in ev["objects"]], ["PARTIALLY-PRESERVED", "FAILED", Base.EQUIV])
        self.assertEqual((man["status"], man["reconciliation"]["failed"]), ("FAILED", 1))

    def test_servidor_que_ignora_a_cor_e_pego_pela_conferencia(self):
        FakeGitHub.ignore_color = True
        proc = self.go()
        self.assertEqual(proc.returncode, 48)
        labels = self.evidence("restore-result.json")["classes"]["labels"]
        self.assertEqual((labels["status"], labels["mismatches"]), ("FAILED", ["Bug"]))

    def test_servidor_que_ignora_a_descricao_e_pego_pela_conferencia(self):
        FakeGitHub.ignore_description = True
        self.assertEqual(self.go().returncode, 48)
        labels = self.evidence("restore-result.json")["classes"]["labels"]
        self.assertEqual((labels["status"], labels["mismatches"]), ("FAILED", ["Bug"]))

    def test_servidor_que_nunca_fecha_o_milestone_e_pego_pela_conferencia(self):
        FakeGitHub.ignore_state = "always"
        self.assertEqual(self.go().returncode, 48)
        ms = self.evidence("restore-result.json")["classes"]["milestones"]
        self.assertEqual((ms["status"], ms["mismatches"]), ("FAILED", ["Marco antigo"]))

    def test_erro_de_escrita_e_falha_mesmo_quando_o_estado_final_ja_confere(self):
        # "documentation" já existe no alvo igual ao backup; a atualização é recusada. Nada diverge, mas a escrita falhou.
        self.make_backup(labels=[{"name": "documentation", "color": "ededed", "description": None}])
        FakeGitHub.label_fail = {"documentation": 500}
        self.assertEqual(self.go().returncode, 48)
        labels = self.evidence("restore-result.json")["classes"]["labels"]
        self.assertEqual((labels["status"], labels["mismatches"], len(labels["errors"])), ("FAILED", [], 1))

    def test_falha_ao_criar_milestone_sai_com_48(self):
        FakeGitHub.milestone_fail = {"Marco antigo": 422}
        proc = self.go()
        self.assertEqual(proc.returncode, 48)
        ms = self.evidence("restore-result.json")["classes"]["milestones"]
        self.assertEqual((ms["status"], ms["created"], ms["mismatches"]), ("FAILED", 1, ["Marco antigo"]))
        self.assertEqual(self.evidence("restore-result.json")["classes"]["labels"]["status"], "OK")

    def test_milestone_fechado_e_fechado_depois_se_o_servidor_ignora_o_estado(self):
        FakeGitHub.ignore_state = True
        self.assertEqual(self.go().returncode, 0)
        self.assertEqual([m["state"] for m in FakeGitHub.milestones["dono/teste-restaurado"]], ["open", "closed"])
        self.assertIn(("PATCH", "/repos/dono/teste-restaurado/milestones/2"), FakeGitHub.calls)

    def test_prazo_normalizado_na_mesma_data_e_nota_e_outra_data_e_falha(self):
        FakeGitHub.due_shift = "same-day"
        self.assertEqual(self.go().returncode, 0)
        ms = self.evidence("restore-result.json")["classes"]["milestones"]
        self.assertEqual(ms["status"], "OK")
        self.assertIn("normalizou", ms["notes"][0])
        FakeGitHub.due_shift = "other-day"
        proc = self.again()
        self.assertEqual(proc.returncode, 48)
        self.assertEqual(self.evidence("restore-result.json")["classes"]["milestones"]["mismatches"], ["Marco de teste"])

    def test_prazo_que_aparece_onde_o_backup_nao_tinha_e_falha(self):
        FakeGitHub.due_shift = "add-due"
        self.assertEqual(self.go().returncode, 48)
        self.assertEqual(self.evidence("restore-result.json")["classes"]["milestones"]["mismatches"], ["Marco antigo"])

    def test_mais_de_cem_labels_sao_listadas_por_paginas(self):
        many = [{"name": f"label-{i:03d}", "color": "ededed", "description": f"d{i}"} for i in range(105)]
        self.make_backup(labels=many)
        self.assertEqual(self.go().returncode, 0)
        res = self.evidence("restore-result.json")["classes"]["labels"]
        self.assertEqual((res["status"], res["created"]), ("OK", 105))
        self.assertEqual(len(FakeGitHub.labels["dono/teste-restaurado"]), 108)                 # 105 + 3 padrão
        pages = [p for m, p in FakeGitHub.calls if m == "GET" and "/labels?" in p]
        self.assertTrue(any("page=2" in p for p in pages), pages)

    def test_limite_de_taxa_espera_e_repete_mas_nao_para_sempre(self):
        FakeGitHub.rate_limited = 1
        self.assertEqual(self.go().returncode, 0)
        FakeGitHub.rate_limited = 50                       # nunca para: depois de 3 tentativas a escrita falha
        proc = self.again()
        self.assertEqual(proc.returncode, 48)
        self.assertEqual(self.evidence("restore-result.json")["classes"]["labels"]["status"], "FAILED")

    def test_labels_e_milestones_so_depois_do_git_conferido(self):
        FakeGitHub.hide_tag = "v1"                           # as refs do alvo não conferem: saída 45
        proc = self.go()
        self.assertEqual(proc.returncode, 45)
        self.assertFalse([c for c in FakeGitHub.calls if "/labels" in c[1] or "/milestones" in c[1]])

    def test_alvo_existente_nao_recebe_labels_nem_milestones(self):
        FakeGitHub.existing.add("dono/teste-restaurado")
        self.assertEqual(self.go().returncode, 42)
        self.assertFalse([c for c in FakeGitHub.calls if "/labels" in c[1] or "/milestones" in c[1]])
        self.assertEqual(FakeGitHub.labels, {})

    def test_labels_nunca_sao_escritas_na_origem(self):
        self.assertEqual(self.go().returncode, 0)
        writes = [p for m, p in FakeGitHub.calls if m in ("POST", "PATCH", "PUT") and (p == f"/repos/{SOURCE}" or p.startswith(f"/repos/{SOURCE}/"))]
        self.assertEqual(writes, [])


class StaticGuardsTest(unittest.TestCase):
    """Garantias de código que os testes ponta a ponta não conseguem observar (lidas pela árvore sintática)."""

    @staticmethod
    def calls(name):
        import ast
        tree = ast.parse((SCRIPTS / "restore_repo.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
                yield [a.value for a in node.args if isinstance(a, ast.Constant)], node

    def test_nunca_envia_com_force_e_so_com_refs_de_branches_e_tags(self):
        pushes = [args for args, _ in self.calls("git") if args and args[0] == "push"]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0][1:], ["refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*"])
        for args, _ in self.calls("git"):
            for arg in args:
                self.assertFalse(str(arg).startswith(("--force", "-f", "+")), arg)

    def test_nunca_apaga_repositorio_nem_usa_outro_metodo_de_escrita_alem_dos_previstos(self):
        methods = sorted({args[0] for name in ("gh", "gh_write") for args, _ in self.calls(name) if args})
        self.assertEqual(methods, ["GET", "PATCH", "POST", "PUT"])                      # sem DELETE
        self.assertNotIn("DELETE", (SCRIPTS / "restore_repo.py").read_text(encoding="utf-8").replace("sem DELETE", "").replace("DELETE:", ""))

    def test_a_url_de_envio_nao_leva_o_token(self):
        text = (SCRIPTS / "restore_repo.py").read_text(encoding="utf-8")
        self.assertNotIn("@github.com", text)
        self.assertNotIn("x-access-token:{token}@", text)


class EvidenceAndPreflightTest(Base):
    def test_preflight_sem_token_bloqueia_com_tres_restricoes(self):
        proc = self.bkp("preflight", RESTORE_TOKEN_OUTCOME="failure")
        self.assertEqual(proc.returncode, 10)
        required = self.result(proc)["required_restrictions"]
        self.assertEqual(sorted(r["restriction_id"] for r in required),
                         ["RST-git-ACCESS_PERMISSION_GAP", "RST-labels-ACCESS_PERMISSION_GAP", "RST-milestones-ACCESS_PERMISSION_GAP"])
        pf = json.loads((self.root / "evidence" / "preflight.json").read_text())
        route = next(r for r in pf["assessments"][0]["routes"] if r["route_type"] == "DETERMINISTIC_EXECUTOR")
        self.assertEqual(route["route_reference"], "github_actions:restore-repository.yml")

    def test_preflight_com_token_passa_sem_o_token_de_leitura(self):
        proc = self.bkp("preflight", RESTORE_TOKEN_OUTCOME="success")
        self.assertEqual((proc.returncode, self.result(proc)["preflight_status"]), (0, "PASS"))
        pf = json.loads((self.root / "evidence" / "preflight.json").read_text())
        self.assertEqual(([a["object_class"] for a in pf["assessments"]], pf["factual_inventory_pending"], pf["gaps"]),
                         (["git", "labels", "milestones"], [], []))

    def test_validate_inputs(self):
        self.assertEqual(self.bkp("validate-inputs").returncode, 0)
        self.assertEqual(self.bkp("validate-inputs", TARGET_NAME="copia").returncode, 0)
        for bad in ({"BACKUP_PATH": "../fora"}, {"BACKUP_PATH": "/abs"}, {"TARGET_NAME": "a/b"}, {"TARGET_NAME": ".."}):
            proc = self.bkp("validate-inputs", **bad)
            self.assertEqual(proc.returncode, 2, bad)

    def finish(self, **env):
        self.assertEqual(self.bkp("preflight", RESTORE_TOKEN_OUTCOME="success").returncode, 0)
        proc = self.restore("build-evidence", **env)
        return proc, self.evidence("evidence.json"), self.evidence("manifest.json")

    def test_evidencia_e_manifest_da_restauracao(self):
        self.fetched()
        self.assertEqual(self.restore("restore").returncode, 0)
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((ev["capability_id"], man["capability_id"]), ("restore_repository", "restore_repository"))
        self.assertEqual(ev["source_scope"], {"backup_path": BASE, "source_repository": SOURCE, "target_repository": "dono/teste-restaurado",
                                              "destination": f"{BASE}/restores"})
        self.assertEqual([(o["object_class"], o["disposition"]) for o in ev["objects"]],
                         [("git", "PARTIALLY-PRESERVED"), ("labels", Base.EQUIV), ("milestones", Base.EQUIV)])
        self.assertEqual(man["status"], "COMPLETE_WITH_EXCEPTIONS")
        ids = [l["limitation_id"] for l in man["limitations"]]
        self.assertTrue({"LIM-restore-scope", "LIM-git-refs", "LIM-labels-restore", "LIM-milestones-restore"} <= set(ids), ids)
        rec = man["reconciliation"]
        self.assertEqual((rec["partial"], rec["equivalent"], rec["failed"], rec["not_verified"], rec["unreconciled_object_classes"]), (1, 2, 0, 0, []))
        self.assertIn("restore-map.json", [a["path"] for a in man["artifacts"]])
        self.assertIn("refs-restored.tsv", [a["path"] for a in man["artifacts"]])
        self.assert_no_leak()

    def test_restauracao_sem_refs_de_pull_fica_preserved(self):
        res = {"status": "OK", "source_repository": SOURCE, "target_repository": "dono/x", "skipped_refs": 0, "notes": []}
        (self.root / "evidence").mkdir(parents=True, exist_ok=True)
        (self.root / "evidence" / "restore-result.json").write_text(json.dumps(res))
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(ev["objects"][0]["disposition"], "PRESERVED")
        self.assertEqual(man["reconciliation"]["preserved"], 1)
        # Resultado sem a seção de classes: labels e milestones ficam NOT-VERIFIED, nunca sucesso presumido.
        self.assertEqual([(o["object_class"], o["disposition"]) for o in ev["objects"][1:]], [("labels", "NOT-VERIFIED"), ("milestones", "NOT-VERIFIED")])
        self.assertEqual(man["reconciliation"]["unreconciled_object_classes"], ["labels", "milestones"])

    def test_falha_vira_failed_com_saida_1(self):
        self.fetched()
        FakeGitHub.existing.add("dono/teste-restaurado")
        self.assertEqual(self.restore("restore").returncode, 42)
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual((ev["objects"][0]["disposition"], man["status"], man["reconciliation"]["failed"]), ("FAILED", "FAILED", 1))
        self.assertEqual([o["disposition"] for o in ev["objects"][1:]], ["NOT-VERIFIED", "NOT-VERIFIED"])    # o git não chegou a ser restaurado
        self.assertIn("TARGET_EXISTS", ev["objects"][0]["limitation"])

    def test_sem_resultado_da_restauracao_e_failed(self):
        proc, ev, man = self.finish()
        self.assertEqual((proc.returncode, ev["objects"][0]["disposition"]), (1, "FAILED"))

    def test_recusa_gerar_evidencia_sem_destino_validado(self):
        self.assertEqual(self.restore("build-evidence", DESTINATION_VALIDATED="").returncode, 21)

    def test_uso_incorreto(self):
        self.assertEqual(subprocess.run([sys.executable, str(SCRIPTS / "restore_repo.py"), "nada"], env=self.env, capture_output=True).returncode, 64)
        env = {k: v for k, v in self.env.items() if k != "BACKUP_PATH"}
        self.assertEqual(subprocess.run([sys.executable, str(SCRIPTS / "restore_repo.py"), "fetch"], env=env, capture_output=True).returncode, 64)


if __name__ == "__main__":
    unittest.main()
