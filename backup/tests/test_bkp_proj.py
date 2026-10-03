#!/usr/bin/env python3
"""Teste local do bkp_repo.py nos dois modos (BKP_PROJ novo e BKP_REPO, regressão), ponta a ponta.

O QUE PROVA
    Roda os subcomandos como o workflow roda (validate-inputs, preflight, leitores, build-evidence),
    contra servidores de API SIMULADOS (REST e GraphQL):
    - BKP_PROJ: entradas recusadas (JSON inválido, escopo e identificadores errados) com saída 2;
    - preflight de uma única classe (projects): sem o PROJECTS_READ_TOKEN vira BLOCKED (saída 10) com
      UMA restrição, com o token passa; o SOURCE_READ_TOKEN não é exigido;
    - evidência e manifest (schemas 2.0): capability_id backup_projects, escopo no lugar do repositório,
      só o objeto `projects`, sem objeto git, pacote do OneDrive listado no manifest;
    - os 3 escopos, vínculo oculto confirmado pelos títulos (SOURCE_TITLES_DIR) e classe sem token NOT-VERIFIED;
    - REGRESSÃO do BKP_REPO: mesmas 13 restrições, objeto git presente, capability_id backup_repository.
    NÃO prova nada contra o GitHub nem o OneDrive reais: isso só um run real prova.

COMO RODAR
    python3 backup/tests/test_bkp_proj.py        (saída 0 = tudo certo)
"""
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backup" / "tests"))
import test_github_api_read as ta  # noqa: E402
import test_github_projects_read as tp  # noqa: E402

SCRIPTS = ROOT / "backup" / "scripts"
IDS = {"owner": "dono", "number": 13}


def start(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class BkpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rest, cls.graph = start(ta.FakeGitHub), start(tp.FakeGraph)

    @classmethod
    def tearDownClass(cls):
        cls.rest.shutdown()
        cls.graph.shutdown()

    def setUp(self):
        ta.FakeGitHub.data = ta.make_data()
        tp.FakeGraph.projects = [tp.make_project()]
        tp.FakeGraph.repo_visible, tp.FakeGraph.owner_exists = True, True
        tp.FakeGraph.http_fail, tp.FakeGraph.gql_errors, tp.FakeGraph.flaky, tp.FakeGraph.ratelimit = {}, {}, {}, {}
        tp.FakeGraph.seen = []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for d in ("evidence", "package", "titles-work"):
            (self.root / d).mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in ("CAPABILITY_ID", "TOKEN_OUTCOME", "PROJECTS_TOKEN_OUTCOME", "PREFLIGHT_DECISION")}
        self.env.update(EVIDENCE_DIR=str(self.root / "evidence"), PACKAGE_DIR=str(self.root / "package"),
                        REQUEST_ID="req-test-0001", DESTINATION="backups/x", DESTINATION_VALIDATED="true")

    def tearDown(self):
        self.tmp.cleanup()

    # ---- ajudantes ----
    def run_script(self, script, *args, **env):
        e = dict(self.env, **env)
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], env=e, capture_output=True, text=True, cwd=ROOT)

    def proj_env(self, scope="PROJECT", ids=None, **extra):
        ids = IDS if ids is None else ids
        self.env.update(CAPABILITY_ID="backup_projects", SCOPE=scope, SCOPE_IDENTIFIERS=json.dumps(ids), **extra)

    def bkp(self, sub, **env):
        return self.run_script("bkp_repo.py", sub, **env)

    @staticmethod
    def result(proc):
        marks = [l for l in proc.stdout.splitlines() if l.startswith("BKP_RESULT ")]
        return json.loads(marks[-1][len("BKP_RESULT "):]) if marks else {}

    def read_projects(self, scope="PROJECT", ids=None, **env):
        ids = IDS if ids is None else ids
        proc = self.run_script("github_projects_read.py", "read", PROJECTS_TOKEN="x", PROJECTS_SCOPE=scope,
                               PROJECTS_SCOPE_IDS=json.dumps(ids), GITHUB_API_URL=f"http://127.0.0.1:{self.graph.server_address[1]}", **env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def finish(self):
        """Pacote já enviado (simulado) e build-evidence; devolve (processo, evidence, manifest)."""
        (self.root / "evidence" / "onedrive-package.json").write_text(
            json.dumps([{"remote_path": "backups/x/req-test-0001/package/api-projects.json", "sha256": "0" * 64, "bytes": 1}]))
        proc = self.bkp("build-evidence")
        ev = self.root / "evidence"
        load = lambda n: json.loads((ev / n).read_text()) if (ev / n).exists() else None
        return proc, load("evidence.json"), load("manifest.json")

    # ---- BKP_PROJ ----
    def test_entradas_invalidas_saem_com_2(self):
        for scope, raw in (("PROJECT", "{nao e json"), ("PROJECT", '{"owner": "dono"}'), ("TODOS", '{"owner": "dono"}'),
                           ("OWNER_PROJECT_SET", '{"owner": "../x"}'), ("SOURCE_REPOSITORY", '{"source_repository": "semBarra"}')):
            self.env.update(CAPABILITY_ID="backup_projects", SCOPE=scope, SCOPE_IDENTIFIERS=raw)
            proc = self.bkp("validate-inputs")
            self.assertEqual(proc.returncode, 2, (scope, raw, proc.stdout))
            self.assertEqual(self.result(proc).get("status"), "REJECTED")

    def test_entradas_validas_dos_tres_escopos(self):
        for scope, ids in (("PROJECT", IDS), ("OWNER_PROJECT_SET", {"owner": "dono"}), ("SOURCE_REPOSITORY", {"source_repository": "dono/teste"})):
            self.proj_env(scope, ids)
            proc = self.bkp("validate-inputs")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_preflight_sem_token_de_projects_bloqueia_com_uma_restricao(self):
        self.proj_env()
        proc = self.bkp("preflight", PROJECTS_TOKEN_OUTCOME="failure")
        self.assertEqual(proc.returncode, 10)
        required = self.result(proc)["required_restrictions"]
        self.assertEqual([r["restriction_id"] for r in required], ["RST-projects-ACCESS_PERMISSION_GAP"])
        pf = json.loads((self.root / "evidence" / "preflight.json").read_text())
        self.assertEqual([a["object_class"] for a in pf["assessments"]], ["projects"])
        route = next(r for r in pf["assessments"][0]["routes"] if r["route_type"] == "DETERMINISTIC_EXECUTOR")
        self.assertEqual(route["route_reference"], "github_actions:backup-projects.yml")

    def test_preflight_com_token_passa_sem_o_token_da_origem(self):
        self.proj_env()
        proc = self.bkp("preflight", PROJECTS_TOKEN_OUTCOME="success")      # TOKEN_OUTCOME ausente de propósito
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.result(proc)["preflight_status"], "PASS")

    def test_backup_de_um_project_evidencia_e_manifest(self):
        self.proj_env()
        self.assertEqual(self.bkp("preflight", PROJECTS_TOKEN_OUTCOME="success").returncode, 0)
        self.read_projects()
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((ev["capability_id"], man["capability_id"]), ("backup_projects", "backup_projects"))
        self.assertEqual(ev["source_scope"], {"scope": "PROJECT", "scope_identifiers": IDS, "destination": "backups/x"})
        self.assertEqual(man["scope"], ev["source_scope"])
        self.assertEqual([(o["object_class"], o["disposition"]) for o in ev["objects"]], [("projects", "PRESERVED-AS-EQUIVALENT-REPRESENTATION")])
        self.assertEqual(ev["objects"][0]["object_id"], "dono/projects/13#projects")
        self.assertEqual(man["status"], "COMPLETE_WITH_EXCEPTIONS")      # a representação equivalente é uma limitação declarada
        self.assertEqual(man["reconciliation"]["equivalent"], 1)
        paths = [a["path"] for a in man["artifacts"]]
        self.assertIn("backups/x/req-test-0001/package/api-projects.json", paths)
        self.assertIn("api-projects-status.json", paths)

    def test_escopo_do_dono_traz_todos_os_projects(self):
        tp.FakeGraph.projects = [tp.make_project(13), tp.make_project(14, "Outro", repos=())]
        self.proj_env("OWNER_PROJECT_SET", {"owner": "dono"})
        self.assertEqual(self.bkp("preflight", PROJECTS_TOKEN_OUTCOME="success").returncode, 0)
        self.read_projects("OWNER_PROJECT_SET", {"owner": "dono"})
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(ev["objects"][0]["object_id"], "dono/projects#projects")
        self.assertEqual(len(json.loads((self.root / "package" / "api-projects.json").read_text())), 2)

    def test_origem_privada_confirmada_pelos_titulos_fica_parcial(self):
        tp.FakeGraph.repo_visible = False
        tp.FakeGraph.projects = [tp.make_project(13, repos=(None,), hidden=True),
                                 tp.make_project(12, "Alheio", repos=(None,), hidden=True, issue_titles=["Outro item"], drafts=0)]
        ids = {"source_repository": ta.REPO}
        self.proj_env("SOURCE_REPOSITORY", ids)
        # Como o workflow: as issues vão para a pasta de trabalho (fora do pacote) e o leitor de Projects as lê de lá.
        rest = self.run_script("github_api_read.py", "read", SOURCE_TOKEN="x", SOURCE_REPOSITORY=ta.REPO,
                               PACKAGE_DIR=str(self.root / "titles-work"), EVIDENCE_DIR=str(self.root / "titles-work" / "evidence"),
                               GITHUB_API_URL=f"http://127.0.0.1:{self.rest.server_address[1]}")
        self.assertEqual(rest.returncode, 0, rest.stdout + rest.stderr)
        self.read_projects("SOURCE_REPOSITORY", ids, SOURCE_TITLES_DIR=str(self.root / "titles-work"))
        self.assertEqual(self.bkp("preflight", PROJECTS_TOKEN_OUTCOME="success").returncode, 0)
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(ev["objects"][0]["disposition"], "PARTIALLY-PRESERVED")
        self.assertEqual(ev["objects"][0]["object_id"], f"{ta.REPO}#projects")
        self.assertEqual([p["number"] for p in json.loads((self.root / "package" / "api-projects.json").read_text())], [13])
        self.assertFalse((self.root / "package" / "api-issues.json").exists())

    def test_sem_token_e_restricao_aceita_projects_fica_not_verified(self):
        self.proj_env()
        blocked = self.bkp("preflight", PROJECTS_TOKEN_OUTCOME="failure")
        decision = {"previous_request_id": "req-test-0000", "hitl_decision": "CONTINUE_WITH_RESTRICTIONS",
                    "accepted_restrictions": self.result(blocked)["required_restrictions"]}
        self.env["PREFLIGHT_DECISION"] = json.dumps(decision)
        self.assertEqual(self.bkp("preflight", PROJECTS_TOKEN_OUTCOME="failure").returncode, 0)
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([(o["object_class"], o["disposition"]) for o in ev["objects"]], [("projects", "NOT-VERIFIED")])
        self.assertEqual(ev["objects"][0]["restriction_ids"], ["RST-projects-ACCESS_PERMISSION_GAP"])
        self.assertEqual(man["reconciliation"]["not_verified"], 1)

    def test_leitura_nao_feita_vira_failed(self):
        self.proj_env()
        self.assertEqual(self.bkp("preflight", PROJECTS_TOKEN_OUTCOME="success").returncode, 0)
        proc, ev, man = self.finish()                     # o leitor não rodou: sem api-projects-status.json
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(ev["objects"][0]["disposition"], "FAILED")
        self.assertEqual(man["status"], "FAILED")

    def test_sem_pacote_no_destino_projects_falha(self):
        self.proj_env()
        self.assertEqual(self.bkp("preflight", PROJECTS_TOKEN_OUTCOME="success").returncode, 0)
        self.read_projects()
        proc = self.bkp("build-evidence")                 # sem onedrive-package.json
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads((self.root / "evidence" / "evidence.json").read_text())["objects"][0]["disposition"], "FAILED")

    def test_recusa_gerar_evidencia_sem_destino_validado(self):
        self.proj_env()
        self.assertEqual(self.bkp("build-evidence", DESTINATION_VALIDATED="").returncode, 21)

    # ---- REGRESSÃO do BKP_REPO ----
    def test_regressao_bkp_repo_preflight_e_evidencia(self):
        self.env.update(SOURCE_REPOSITORY=ta.REPO)
        blocked = self.bkp("preflight", TOKEN_OUTCOME="success", PROJECTS_TOKEN_OUTCOME="success")
        self.assertEqual(blocked.returncode, 10)
        classes = sorted(r["object_class"] for r in self.result(blocked)["required_restrictions"])
        self.assertEqual(len(classes), 13)
        for implemented in ("git", "labels", "milestones", "issues", "projects"):
            self.assertNotIn(implemented, classes)
        # Sem o token da origem, as 5 classes implementadas também viram lacuna (o BKP_REPO continua exigindo o token).
        no_token = self.bkp("preflight", TOKEN_OUTCOME="failure", PROJECTS_TOKEN_OUTCOME="success")
        self.assertEqual(len(self.result(no_token)["required_restrictions"]), 18)
        # Evidência completa do BKP_REPO.
        decision = {"previous_request_id": "req-test-0000", "hitl_decision": "CONTINUE_WITH_RESTRICTIONS",
                    "accepted_restrictions": self.result(blocked)["required_restrictions"]}
        self.env["PREFLIGHT_DECISION"] = json.dumps(decision)
        self.assertEqual(self.bkp("preflight", TOKEN_OUTCOME="success", PROJECTS_TOKEN_OUTCOME="success").returncode, 0)
        rest = self.run_script("github_api_read.py", "read", SOURCE_TOKEN="x", SOURCE_REPOSITORY=ta.REPO,
                               GITHUB_API_URL=f"http://127.0.0.1:{self.rest.server_address[1]}")
        self.assertEqual(rest.returncode, 0, rest.stdout + rest.stderr)
        self.read_projects("SOURCE_REPOSITORY", {"source_repository": ta.REPO})
        (self.root / "evidence" / "refs.tsv").write_text("refs/heads/main\tabc\tcommit\n")
        (self.root / "evidence" / "onedrive-package.json").write_text(json.dumps([{"remote_path": "p/source.bundle", "sha256": "0" * 64, "bytes": 1}]))
        proc = self.bkp("build-evidence")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        ev = json.loads((self.root / "evidence" / "evidence.json").read_text())
        self.assertEqual(ev["capability_id"], "backup_repository")
        self.assertEqual(ev["source_scope"], {"source_repository": ta.REPO, "destination": "backups/x"})
        disp = {o["object_class"]: o["disposition"] for o in ev["objects"]}
        self.assertEqual(disp["git"], "PRESERVED")
        for cls in ("labels", "milestones", "issues", "projects"):
            self.assertIn(disp[cls], ("PRESERVED-AS-EQUIVALENT-REPRESENTATION", "PARTIALLY-PRESERVED"), cls)
        self.assertEqual(len(disp), 18)


if __name__ == "__main__":
    unittest.main()
