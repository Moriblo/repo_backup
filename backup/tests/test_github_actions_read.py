#!/usr/bin/env python3
"""Teste local do leitor github_actions_read.py e da evidência temporal do bkp_repo.py, contra uma API SIMULADA.

O QUE PROVA
    - leitura completa e paginada de workflows, runs, tentativas (várias por run), jobs, logs e artifacts;
    - o download de logs e artifacts segue o redirecionamento SEM mandar o token ao armazenamento;
    - conferências: total_count de runs e jobs, ids únicos, artifacts por run contra a lista do repositório, ZIP
      íntegro, tamanho e digest do artifact; qualquer divergência vira FAILED;
    - estados temporais: log expirado (410) = EXPIRED, apagado (404) = DELETED_UNAVAILABLE, run em andamento =
      NOT_COMPLETED, artifact expirado não é baixado; a classe fica PARTIAL, nunca "vazio com sucesso";
    - teto de tamanho: os mais antigos entram primeiro, o resto vira SKIPPED_OVER_CAP;
    - falhas: 403 vira FAILED com a dica de permissão; repetição em 500 e em limite de taxa;
    - só GET, e o token, nomes de workflow/job/passo/branch, títulos de commit e conteúdo de log NUNCA aparecem
      na saída pública (só contagens e tamanhos; rotas sem ids);
    - evidência (bkp_repo.py build-evidence): objeto `actions` com temporal_state e RETENTION_ALERT, MRBI e CRR
      como retenção OBSERVADA (artifact e conferência manual do registro), nunca inferida.
    NÃO prova nada contra o GitHub real: isso só o run real do backup prova.

COMO RODAR
    python3 backup/tests/test_github_actions_read.py        (saída 0 = tudo certo)
"""
import contextlib
import hashlib
import http.server
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "backup" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import github_actions_read as a  # noqa: E402
import github_api_read as g  # noqa: E402

TOKEN = "ghp_SEGREDO_DE_TESTE_123"
REPO = "dono/teste"
SECRETS = ["WF_SEGREDO_NOME", "JOB_SEGREDO_NOME", "PASSO_SEGREDO_NOME", "BRANCH_SEGREDA", "TITULO_COMMIT_SEGREDO",
           "CONTEUDO_DO_LOG_SEGREDO", "ARTEFATO_SEGREDO_NOME"]
EQUIVALENT = "PRESERVED-AS-EQUIVALENT-REPRESENTATION"


def make_zip(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("1_job.txt", text)
    return buf.getvalue()


def make_data():
    """3 runs: 101 (1 tentativa), 102 (2 tentativas), 103 (em andamento). 2 artifacts (1 expirado)."""
    runs = []
    for rid, day, attempt, status in ((101, 21, 1, "completed"), (102, 22, 2, "completed"), (103, 23, 1, "in_progress")):
        runs.append({"id": rid, "name": "WF_SEGREDO_NOME", "head_branch": "BRANCH_SEGREDA", "display_title": "TITULO_COMMIT_SEGREDO",
                     "status": status, "conclusion": "success" if status == "completed" else None, "run_attempt": attempt,
                     "created_at": f"2026-09-{day}T10:00:00Z"})
    logs = {(101, 1): make_zip("CONTEUDO_DO_LOG_SEGREDO 101"), (102, 1): make_zip("CONTEUDO_DO_LOG_SEGREDO 102a"),
            (102, 2): make_zip("CONTEUDO_DO_LOG_SEGREDO 102b")}
    art_zip = make_zip("conteudo do artifact")
    arts = [
        {"id": 9001, "name": "ARTEFATO_SEGREDO_NOME", "size_in_bytes": len(art_zip), "expired": False, "run_id": 101,
         "created_at": "2026-09-21T10:05:00Z", "expires_at": "2026-12-20T10:05:00Z",
         "digest": "sha256:" + hashlib.sha256(art_zip).hexdigest()},
        {"id": 9002, "name": "ARTEFATO_SEGREDO_NOME", "size_in_bytes": 10, "expired": True, "run_id": 102,
         "created_at": "2026-06-01T10:05:00Z", "expires_at": "2026-08-30T10:05:00Z"},
    ]
    return {"workflows": [{"id": 1, "name": "WF_SEGREDO_NOME", "path": ".github/workflows/x.yml", "state": "active"}],
            "runs": runs, "logs": logs, "arts": arts, "art_zips": {9001: art_zip},
            "jobs": {"name": "JOB_SEGREDO_NOME", "steps": [{"name": "PASSO_SEGREDO_NOME", "number": 1}]}}


class Fake(http.server.BaseHTTPRequestHandler):
    """API simulada. Atributos de classe controlam os cenários."""
    data = {}
    fail = {}            # rota -> código HTTP fixo
    flaky = {}           # rota -> respostas 500 antes de responder certo
    ratelimit = {}       # rota -> respostas 403 com Retry-After antes de responder certo
    runs_total = None    # sobrescreve o total_count da lista de runs
    jobs_total_extra = 0
    repo_arts_extra = 0  # artifacts a mais na lista do repositório
    bad_zip = False
    methods = []
    storage_auth = []

    def log_message(self, *args):
        pass

    def _send(self, code, body=None, headers=None, raw=None, ctype="application/json"):
        raw = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _paged(self, key, items, q, total=None):
        per, page = int(q.get("per_page", ["100"])[0]), int(q.get("page", ["1"])[0])
        chunk = items[(page - 1) * per: page * per]
        headers = {}
        if page * per < len(items):
            nq = {k: v[0] for k, v in q.items()}
            nq["page"] = page + 1
            headers["Link"] = f'<http://{self.headers["Host"]}{urllib.parse.urlparse(self.path).path}?{urllib.parse.urlencode(nq)}>; rel="next"'
        self._send(200, {"total_count": len(items) if total is None else total, key: chunk}, headers)

    def do_GET(self):  # noqa: N802
        cls = Fake
        cls.methods.append("GET")
        parsed = urllib.parse.urlparse(self.path)
        route, q = parsed.path, urllib.parse.parse_qs(parsed.query)
        host = self.headers["Host"]
        if route.startswith("/storage/"):
            cls.storage_auth.append(self.headers.get("Authorization"))
            kind, name = route.split("/")[2:4]
            if kind == "logs":
                rid, n = name[:-4].split("-")
                body = b"nao e um zip" if cls.bad_zip else cls.data["logs"][(int(rid), int(n))]
            else:
                body = cls.data["art_zips"][int(name[:-4])]
            return self._send(200, raw=body, ctype="application/zip")
        if route in cls.fail:
            return self._send(cls.fail[route], {"message": "x"})
        if cls.flaky.get(route, 0) > 0:
            cls.flaky[route] -= 1
            return self._send(500, {"message": "boom"})
        if cls.ratelimit.get(route, 0) > 0:
            cls.ratelimit[route] -= 1
            return self._send(403, {"message": "rate"}, {"Retry-After": "1", "X-RateLimit-Remaining": "0"})
        d = cls.data
        base = f"/repos/{REPO}/actions"
        if route == f"{base}/workflows":
            return self._paged("workflows", d["workflows"], q)
        if route == f"{base}/runs":
            return self._paged("workflow_runs", d["runs"], q, cls.runs_total)
        if route == f"{base}/artifacts":
            extra = [dict(d["arts"][0], id=9900 + k) for k in range(cls.repo_arts_extra)]
            return self._paged("artifacts", d["arts"] + extra, q)
        parts = route[len(base) + 1:].split("/") if route.startswith(base + "/") else []
        if parts[:1] == ["runs"] and len(parts) >= 3:
            rid = int(parts[1])
            run = next(r for r in d["runs"] if r["id"] == rid)
            if parts[2] == "artifacts":
                return self._paged("artifacts", [x for x in d["arts"] if x["run_id"] == rid], q)
            if parts[2] == "attempts":
                n = int(parts[3])
                if len(parts) == 4:
                    return self._send(200, dict(run, run_attempt=n))
                if parts[4] == "jobs":
                    jobs = [dict(d["jobs"], id=rid * 10 + n)]
                    return self._paged("jobs", jobs, q, len(jobs) + cls.jobs_total_extra)
                if parts[4] == "logs":
                    if (rid, n) in d.get("log_gone", {}):
                        return self._send(d["log_gone"][(rid, n)], {"message": "gone"})
                    return self._send(302, headers={"Location": f"http://{host}/storage/logs/{rid}-{n}.zip"})
        if parts[:1] == ["artifacts"] and len(parts) == 3 and parts[2] == "zip":
            aid = int(parts[1])
            if aid in d.get("art_gone", {}):
                return self._send(d["art_gone"][aid], {"message": "gone"})
            return self._send(302, headers={"Location": f"http://{host}/storage/art/{aid}.zip"})
        return self._send(404, {"message": "not found"})


class ReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Fake.data = make_data()
        Fake.fail, Fake.flaky, Fake.ratelimit = {}, {}, {}
        Fake.runs_total, Fake.jobs_total_extra, Fake.repo_arts_extra, Fake.bad_zip = None, 0, 0, False
        Fake.methods, Fake.storage_auth = [], []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "evidence").mkdir()
        (self.root / "package").mkdir()
        patches = [unittest.mock.patch.object(a, "sleep", lambda s: None), unittest.mock.patch.object(g, "sleep", lambda s: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- ajudantes ----
    def run_read(self, **env):
        """Roda cmd_read como o workflow; devolve (saída pública, status da classe, inventário)."""
        environ = {"SOURCE_REPOSITORY": REPO, "SOURCE_TOKEN": TOKEN, "EVIDENCE_DIR": str(self.root / "evidence"),
                   "PACKAGE_DIR": str(self.root / "package"), "GITHUB_API_URL": self.base}
        environ.update(env)
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, environ), contextlib.redirect_stdout(out):
            a.cmd_read()
        inv = json.loads((self.root / "evidence" / "api-actions-status.json").read_text())
        return out.getvalue(), inv["classes"]["actions"], inv

    def package_names(self):
        return sorted(p.name for p in (self.root / "package").iterdir())

    def assert_public_clean(self, text):
        self.assertNotIn(TOKEN, text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    # ---- leitura completa ----
    def test_leitura_completa_pagina_e_confere(self):
        with unittest.mock.patch.object(a, "PER_PAGE", 2):
            out, st, inv = self.run_read(ACTIONS_MAX_BYTES=str(10 ** 7))
        # run 103 está em andamento: log NOT_COMPLETED => PARTIAL; o resto passou nas conferências.
        self.assertEqual(st["status"], "PARTIAL", st)
        self.assertTrue(all(c["ok"] for c in st["checks"]), st["checks"])
        self.assertEqual((st["count"], st["extra"]["attempts"], st["extra"]["jobs"]), (3, 4, 4))
        self.assertEqual(st["extra"]["logs"], {"total": 4, "saved": 3})
        self.assertEqual(st["extra"]["artifacts"], {"total": 2, "saved": 1})
        self.assertEqual(st["extra"]["temporal_counts"], {"AVAILABLE": 4, "NOT_COMPLETED": 1, "EXPIRED": 1})
        self.assertEqual(st["extra"]["observed_artifact_retention_days"], [90])
        self.assertEqual(self.package_names(), ["actions-artifacts.tar", "actions-logs.tar", "api-actions-artifacts.json",
                                                "api-actions-attempts.json", "api-actions-inventory.json", "api-actions-runs.json",
                                                "api-actions-workflows.json"])
        # Conteúdo dos tars e hashes do inventário.
        pkg = self.root / "package"
        with tarfile.open(pkg / "actions-logs.tar") as t:
            self.assertEqual(sorted(t.getnames()), ["logs/run-101-attempt-1.zip", "logs/run-102-attempt-1.zip", "logs/run-102-attempt-2.zip"])
            raw = t.extractfile("logs/run-101-attempt-1.zip").read()
        item = next(i for i in inv["items"]["logs"] if i["run_id"] == 101)
        self.assertEqual((item["sha256"], item["bytes"]), (hashlib.sha256(raw).hexdigest(), len(raw)))
        with tarfile.open(pkg / "actions-artifacts.tar") as t:
            self.assertEqual(t.getnames(), ["artifacts/9001.zip"])
        art = next(i for i in inv["items"]["artifacts"] if i["artifact_id"] == 9001)
        self.assertTrue(art["digest_verified"])
        self.assertEqual(next(f for f in st["files"] if f["file"] == "actions-logs.tar")["sha256"],
                         hashlib.sha256((pkg / "actions-logs.tar").read_bytes()).hexdigest())
        # Todas as tentativas do run 102 foram lidas, com jobs e passos.
        attempts = json.loads((pkg / "api-actions-attempts.json").read_text())
        self.assertEqual(sorted((x["run_id"], x["run_attempt"]) for x in attempts), [(101, 1), (102, 1), (102, 2), (103, 1)])
        # O token NÃO vai ao armazenamento para onde a API redireciona.
        self.assertTrue(Fake.storage_auth and all(h is None for h in Fake.storage_auth), Fake.storage_auth)
        self.assertEqual(set(Fake.methods), {"GET"})
        self.assert_public_clean(out)

    def test_log_publico_so_traz_contagens_e_tamanhos(self):
        out, st, inv = self.run_read()
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("API_READ actions: PARTIAL runs=3 attempts=4 jobs=4 logs=3/4 artifacts=1/2 bytes="), lines[0])
        self.assert_public_clean(out)
        # Os nomes ficam só no inventário/pacote (OneDrive), nunca no log público.
        self.assertIn("ARTEFATO_SEGREDO_NOME", json.dumps(inv))

    def test_tudo_disponivel_vira_ok(self):
        Fake.data["runs"] = [r for r in Fake.data["runs"] if r["id"] != 103]
        Fake.data["arts"] = Fake.data["arts"][:1]
        out, st, inv = self.run_read()
        self.assertEqual((st["status"], st["extra"]["temporal_counts"]), ("OK", {"AVAILABLE": 4}), st)
        self.assertNotIn("detail", st)

    # ---- estados temporais ----
    def test_log_expirado_e_apagado_ficam_registrados_como_lacuna(self):
        Fake.data["log_gone"] = {(101, 1): 410, (102, 1): 404}
        out, st, inv = self.run_read()
        states = {(i["run_id"], i["run_attempt"]): i["state"] for i in inv["items"]["logs"]}
        self.assertEqual(states, {(101, 1): "EXPIRED", (102, 1): "DELETED_UNAVAILABLE", (102, 2): "AVAILABLE", (103, 1): "NOT_COMPLETED"})
        self.assertEqual(st["status"], "PARTIAL")
        self.assertIn("EXPIRED", st["detail"])
        self.assertTrue(all(c["ok"] for c in st["checks"]))        # lacuna temporal NÃO é falha

    def test_artifact_expirado_nao_e_baixado_e_410_no_download_tambem_e_expirado(self):
        Fake.data["art_gone"] = {9001: 410}
        out, st, inv = self.run_read()
        self.assertEqual({i["artifact_id"]: i["state"] for i in inv["items"]["artifacts"]}, {9001: "EXPIRED", 9002: "EXPIRED"})
        self.assertNotIn("actions-artifacts.tar", self.package_names())      # nada a guardar: o .tar vazio não vai ao pacote

    # ---- teto de tamanho ----
    def test_teto_de_tamanho_pega_os_mais_antigos_primeiro(self):
        one = len(Fake.data["logs"][(101, 1)])
        out, st, inv = self.run_read(ACTIONS_MAX_BYTES=str(one + 5))         # cabe só o primeiro log (o do run mais antigo)
        states = {(i["run_id"], i["run_attempt"]): i["state"] for i in inv["items"]["logs"]}
        self.assertEqual(states[(101, 1)], "AVAILABLE")
        self.assertEqual(states[(102, 1)], "SKIPPED_OVER_CAP")
        self.assertEqual(states[(102, 2)], "SKIPPED_OVER_CAP")
        self.assertLessEqual(st["extra"]["bytes_downloaded"], one + 5)
        self.assertEqual(st["status"], "PARTIAL")
        self.assertIn("SKIPPED_OVER_CAP", st["detail"])

    def test_artifact_maior_que_o_saldo_nao_e_baixado(self):
        one = len(Fake.data["logs"][(101, 1)])
        out, st, inv = self.run_read(ACTIONS_MAX_BYTES=str(one + 1))
        art = next(i for i in inv["items"]["artifacts"] if i["artifact_id"] == 9001)
        self.assertEqual(art["state"], "SKIPPED_OVER_CAP")

    # ---- conferências que reprovam ----
    def test_total_count_de_runs_divergente_vira_failed(self):
        Fake.runs_total = 9
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("runs_read_equal_total_count", st["detail"])

    def test_total_de_jobs_divergente_vira_failed(self):
        Fake.jobs_total_extra = 1
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("jobs_read_equal_total_count_per_attempt", st["detail"])

    def test_lista_de_artifacts_do_repositorio_divergente_vira_failed(self):
        Fake.repo_arts_extra = 1
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("artifacts_per_run_equal_repository_list", st["detail"])

    def test_zip_de_log_invalido_vira_failed(self):
        Fake.bad_zip = True
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("available_items_all_saved", st["detail"])

    def test_digest_diferente_do_artifact_vira_failed(self):
        Fake.data["arts"][0]["digest"] = "sha256:" + "0" * 64
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        art = next(i for i in inv["items"]["artifacts"] if i["artifact_id"] == 9001)
        self.assertEqual(art["state"], "FAILED")
        self.assertIn("digest", art["reason"])

    def test_tamanho_do_artifact_diferente_vira_failed(self):
        Fake.data["arts"][0]["size_in_bytes"] += 1
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")

    # ---- falhas ----
    def test_403_vira_failed_com_dica_de_permissao(self):
        Fake.fail = {f"/repos/{REPO}/actions/runs": 403}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("Actions: Read", st["detail"])
        self.assertNotRegex(st["detail"], r"/\d")                         # rota sem ids
        self.assertEqual(self.package_names(), ["api-actions-inventory.json"])
        self.assert_public_clean(out)

    def test_403_no_log_tambem_e_fatal(self):
        Fake.fail = {f"/repos/{REPO}/actions/runs/101/attempts/1/logs": 403}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("/actions/runs/{id}/attempts/{id}/logs", st["detail"])

    def test_repete_em_500_e_em_limite_de_taxa_curto(self):
        Fake.flaky = {f"/repos/{REPO}/actions/workflows": 2}
        Fake.ratelimit = {f"/repos/{REPO}/actions/runs/101/attempts/1/jobs": 1}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "PARTIAL")
        self.assertTrue(all(c["ok"] for c in st["checks"]))

    def test_500_persistente_em_um_log_vira_item_failed(self):
        Fake.flaky = {f"/repos/{REPO}/actions/runs/101/attempts/1/logs": 99}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        item = next(i for i in inv["items"]["logs"] if i["run_id"] == 101)
        self.assertEqual((item["state"], item["status"]), ("FAILED", 500))

    def test_excecao_inesperada_vira_failed_sem_vazar_a_mensagem(self):
        with unittest.mock.patch.object(a, "read_actions", side_effect=RuntimeError("CONTEUDO_DO_LOG_SEGREDO")):
            out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("RuntimeError", st["detail"])
        self.assert_public_clean(out + json.dumps(st))

    def test_variaveis_e_teto_invalidos_saem_com_64(self):
        env = {k: v for k, v in os.environ.items() if k not in ("SOURCE_TOKEN", "SOURCE_REPOSITORY")}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "github_actions_read.py"), "read"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 64)
        with unittest.mock.patch.dict(os.environ, {"SOURCE_REPOSITORY": REPO, "SOURCE_TOKEN": TOKEN, "ACTIONS_MAX_BYTES": "muito"}):
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
                a.cmd_read()
        self.assertEqual(ctx.exception.code, 64)

    def test_so_get_e_token_nao_vaza_nos_arquivos(self):
        out, st, inv = self.run_read()
        self.assertEqual(set(Fake.methods), {"GET"})
        for p in list((self.root / "evidence").glob("*")) + list((self.root / "package").glob("*.json")):
            self.assertNotIn(TOKEN, p.read_text(errors="replace"))
        self.assertEqual(list(pathlib.Path(tempfile.gettempdir()).glob("actions-read-*")), [])    # área de trabalho apagada


class TemporalEvidenceTest(unittest.TestCase):
    """bkp_repo.temporal_summary e o build-evidence com a classe actions."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS))
        import bkp_repo
        cls.b = bkp_repo
        cls.cap = {"temporal_observations": {"actions": [
            {"repository": "Dono/Teste", "retention_days": 90, "source": "MANUAL_SETTINGS_CHECK", "checked_on": "2026-10-17"}]}}

    def st(self, counts=None, days=None, oldest="2026-09-21T10:00:00Z"):
        return {"extra": {"temporal_counts": counts or {"AVAILABLE": 3}, "observed_artifact_retention_days": days or [],
                          "oldest_available_created_at": oldest}}

    def test_crr_e_mrbi_observados_citam_a_conferencia_manual(self):
        t = self.b.temporal_summary(self.st(), "dono/teste", self.cap, "PRESERVED-AS-EQUIVALENT-REPRESENTATION")
        self.assertEqual(t["mrbi"], "90 days")
        self.assertIn("manual check", t["crr"])
        self.assertIn("2026-10-17", t["crr"])
        self.assertIn("not read through the API", t["crr"])
        self.assertEqual(t["state"], "AVAILABLE")

    def test_menor_retencao_vence_entre_artifact_e_conferencia(self):
        t = self.b.temporal_summary(self.st(days=[60]), "dono/teste", self.cap, "PARTIALLY-PRESERVED")
        self.assertEqual(t["mrbi"], "60 days")
        self.assertIn("artifact expires_at", t["crr"])

    def test_sem_nenhuma_observacao_crr_e_mrbi_ficam_nulos(self):
        t = self.b.temporal_summary(self.st(), "outro/repo", {}, "PARTIALLY-PRESERVED")
        self.assertEqual((t["mrbi"], t["crr"], t["alerts"]), (None, None, []))
        self.assertTrue(any("could not be observed" in n for n in t["notes"]))

    def test_expirado_ou_apagado_gera_retention_alert_e_estado(self):
        t = self.b.temporal_summary(self.st({"AVAILABLE": 2, "EXPIRED": 1}), "dono/teste", self.cap, "PARTIALLY-PRESERVED")
        self.assertEqual((t["state"], t["alerts"]), ("EXPIRED", ["RETENTION_ALERT"]))
        t = self.b.temporal_summary(self.st({"DELETED_UNAVAILABLE": 1}), "dono/teste", self.cap, "PARTIALLY-PRESERVED")
        self.assertEqual((t["state"], t["alerts"]), ("DELETED_UNAVAILABLE", ["RETENTION_ALERT"]))

    def test_alerta_quando_o_item_mais_antigo_vence_em_ate_30_dias(self):
        import datetime
        recent = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=70)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.b.temporal_summary(self.st(oldest=recent), "dono/teste", self.cap, "PRESERVED-AS-EQUIVALENT-REPRESENTATION")["alerts"], ["RETENTION_ALERT"])
        fresh = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.b.temporal_summary(self.st(oldest=fresh), "dono/teste", self.cap, "PRESERVED-AS-EQUIVALENT-REPRESENTATION")["alerts"], [])

    def test_classe_failed_fica_not_verified(self):
        self.assertEqual(self.b.temporal_summary(self.st(), "dono/teste", self.cap, "FAILED")["state"], "NOT_VERIFIED")


class EvidenceTest(unittest.TestCase):
    """build-evidence ponta a ponta (backup_repository) com a classe actions, contra a API simulada."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Fake.data = make_data()
        Fake.fail, Fake.flaky, Fake.ratelimit = {}, {}, {}
        Fake.runs_total, Fake.jobs_total_extra, Fake.repo_arts_extra, Fake.bad_zip = None, 0, 0, False
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for d in ("evidence", "package"):
            (self.root / d).mkdir()
        (self.root / "evidence" / "refs.tsv").write_text("refs/heads/main\tabc\n")
        self.env = {k: v for k, v in os.environ.items() if k not in ("TOKEN_OUTCOME", "PROJECTS_TOKEN_OUTCOME", "PREFLIGHT_DECISION")}
        self.env.update(CAPABILITY_ID="backup_repository", EVIDENCE_DIR=str(self.root / "evidence"), PACKAGE_DIR=str(self.root / "package"),
                        REQUEST_ID="req-test-0001", DESTINATION="backups/x", DESTINATION_VALIDATED="true", SOURCE_REPOSITORY="Moriblo/MIT")

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, script, *args, **env):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], env=dict(self.env, **env), capture_output=True, text=True, cwd=ROOT)

    def test_evidencia_com_actions_temporal_e_mrbi_crr_observados(self):
        # Preflight: com a classe actions implementada, ela NÃO está entre as restrições exigidas.
        pf = self.run_script("bkp_repo.py", "preflight", TOKEN_OUTCOME="success")
        self.assertEqual(pf.returncode, 10, pf.stdout)
        res = json.loads([l for l in pf.stdout.splitlines() if l.startswith("BKP_RESULT ")][-1][len("BKP_RESULT "):])
        classes = {r["object_class"] for r in res["required_restrictions"]}
        self.assertNotIn("actions", classes)
        self.assertNotIn("git", classes)
        decision = json.dumps({"previous_request_id": "req-prev-0001", "hitl_decision": "CONTINUE_WITH_RESTRICTIONS",
                               "accepted_restrictions": res["required_restrictions"]})
        self.assertEqual(self.run_script("bkp_repo.py", "preflight", TOKEN_OUTCOME="success", PREFLIGHT_DECISION=decision).returncode, 0)
        # Leitor sobre a API simulada. O repositório do ambiente é Moriblo/MIT (tem conferência manual no registro).
        read = self.run_script("github_actions_read.py", "read", SOURCE_TOKEN=TOKEN, GITHUB_API_URL=self.base, SOURCE_REPOSITORY=REPO)
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        # As outras classes de API (labels, milestones, issues) já foram lidas por outro passo: resultado mínimo aprovado.
        (self.root / "evidence" / "api-status.json").write_text(json.dumps({"classes": {
            c: {"status": "OK", "count": 0, "files": [], "checks": []} for c in ("labels", "milestones", "issues")}}))
        (self.root / "evidence" / "api-checks-status.json").write_text(json.dumps({"classes": {"checks": {
            "status": "OK", "count": 0, "files": [], "checks": [], "extra": {"temporal_counts": {"AVAILABLE": 1}}}}}))
        entries = [{"remote_path": f"backups/x/req-test-0001/package/{p.name}", "sha256": "0" * 64, "bytes": 1}
                   for p in sorted((self.root / "package").iterdir())]
        (self.root / "evidence" / "onedrive-package.json").write_text(json.dumps(entries))
        proc = self.run_script("bkp_repo.py", "build-evidence", PREFLIGHT_DECISION=decision)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        ev = json.loads((self.root / "evidence" / "evidence.json").read_text())
        man = json.loads((self.root / "evidence" / "manifest.json").read_text())
        obj = next(o for o in ev["objects"] if o["object_class"] == "actions")
        self.assertEqual(obj["disposition"], "PARTIALLY-PRESERVED")                    # run em andamento + artifact expirado
        self.assertEqual(obj["temporal_state"], "EXPIRED")
        self.assertEqual(obj["alerts"], ["RETENTION_ALERT"])
        self.assertIn("api-actions-status.json", obj["evidence"])
        self.assertIn("actions-logs.tar", obj["evidence"])
        tp = ev["execution"]["temporal_preservation"]
        self.assertEqual(tp["mrbi"], "90 days")
        self.assertIn("observed", tp["crr"])
        self.assertEqual(ev["execution"]["alerts"], ["RETENTION_ALERT"])
        self.assertTrue(any("actions" in lim["limitation_id"] for lim in man["limitations"]))
        self.assertEqual(man["reconciliation"]["failed"], 0)
        self.assertNotIn(TOKEN, json.dumps(ev) + json.dumps(man))

    def test_sem_a_leitura_a_classe_actions_fica_failed(self):
        decision_pf = self.run_script("bkp_repo.py", "preflight", TOKEN_OUTCOME="success")
        res = json.loads([l for l in decision_pf.stdout.splitlines() if l.startswith("BKP_RESULT ")][-1][len("BKP_RESULT "):])
        decision = json.dumps({"previous_request_id": "req-prev-0001", "hitl_decision": "CONTINUE_WITH_RESTRICTIONS",
                               "accepted_restrictions": res["required_restrictions"]})
        self.run_script("bkp_repo.py", "preflight", TOKEN_OUTCOME="success", PREFLIGHT_DECISION=decision)
        (self.root / "evidence" / "onedrive-package.json").write_text(json.dumps(
            [{"remote_path": "backups/x/req-test-0001/package/refs.tsv", "sha256": "0" * 64, "bytes": 1}]))
        proc = self.run_script("bkp_repo.py", "build-evidence", PREFLIGHT_DECISION=decision)
        ev = json.loads((self.root / "evidence" / "evidence.json").read_text())
        obj = next(o for o in ev["objects"] if o["object_class"] == "actions")
        self.assertEqual((obj["disposition"], obj["temporal_state"]), ("FAILED", "NOT_VERIFIED"))
        self.assertEqual(proc.returncode, 1)                                          # FAILED derruba o passo, de propósito


if __name__ == "__main__":
    unittest.main(verbosity=1)
