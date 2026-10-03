#!/usr/bin/env python3
"""Teste local do bkp_repo.py no modo backup_issues (BKP_ISSUES), ponta a ponta, contra uma API do GitHub SIMULADA.

O QUE PROVA
    Roda os subcomandos como o workflow backup-issues.yml roda (validate-inputs, preflight, leitor de API,
    build-evidence):
    - entradas recusadas (repositório fora do formato, destino com ".." ou ausente) com saída 2;
    - preflight de TRÊS classes (labels, milestones, issues): sem o SOURCE_READ_TOKEN vira BLOCKED (saída 10) com três
      restrições, com o token passa, e não exige nenhum outro token;
    - evidência e manifest (schemas 2.0): capability_id backup_issues, o repositório no escopo, só os objetos labels,
      milestones e issues (sem objeto git), pacote do OneDrive listado no manifest, repo-metadata.json no pacote;
    - falha de uma classe vira FAILED sem esconder as outras; recurso desativado (410) vira PRESERVED sem objetos;
      restrição aceita vira NOT-VERIFIED; leitura não feita e pacote não enviado viram FAILED;
    - o token nunca aparece na saída nem nos arquivos de evidência.
    NÃO prova nada contra o GitHub nem o OneDrive reais: isso só um run real prova.

COMO RODAR
    python3 backup/tests/test_bkp_issues.py        (saída 0 = tudo certo)
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

SCRIPTS = ROOT / "backup" / "scripts"
CLASSES = ["labels", "milestones", "issues"]
EQUIVALENT = "PRESERVED-AS-EQUIVALENT-REPRESENTATION"


def start(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class IssuesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rest = start(ta.FakeGitHub)

    @classmethod
    def tearDownClass(cls):
        cls.rest.shutdown()

    def setUp(self):
        ta.FakeGitHub.data = ta.make_data()
        ta.FakeGitHub.fail, ta.FakeGitHub.flaky, ta.FakeGitHub.ratelimit, ta.FakeGitHub.open_count = {}, {}, {}, None
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for d in ("evidence", "package"):
            (self.root / d).mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in ("TOKEN_OUTCOME", "PROJECTS_TOKEN_OUTCOME", "PREFLIGHT_DECISION")}
        self.env.update(CAPABILITY_ID="backup_issues", EVIDENCE_DIR=str(self.root / "evidence"), PACKAGE_DIR=str(self.root / "package"),
                        REQUEST_ID="req-test-0001", DESTINATION="backups/x", DESTINATION_VALIDATED="true", SOURCE_REPOSITORY=ta.REPO)
        self.outputs = []

    def tearDown(self):
        self.tmp.cleanup()

    # ---- ajudantes ----
    def run_script(self, script, *args, **env):
        proc = subprocess.run([sys.executable, str(SCRIPTS / script), *args], env=dict(self.env, **env), capture_output=True, text=True, cwd=ROOT)
        self.outputs.append(proc.stdout + proc.stderr)
        return proc

    def bkp(self, sub, **env):
        return self.run_script("bkp_repo.py", sub, **env)

    @staticmethod
    def result(proc):
        marks = [l for l in proc.stdout.splitlines() if l.startswith("BKP_RESULT ")]
        return json.loads(marks[-1][len("BKP_RESULT "):]) if marks else {}

    def read_api(self):
        proc = self.run_script("github_api_read.py", "read", SOURCE_TOKEN=ta.TOKEN, GITHUB_API_URL=f"http://127.0.0.1:{self.rest.server_address[1]}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def finish(self, uploaded=True):
        """Pacote já enviado (simulado) e build-evidence; devolve (processo, evidence, manifest)."""
        if uploaded:
            entries = [{"remote_path": f"backups/x/req-test-0001/package/{p.name}", "sha256": "0" * 64, "bytes": 1}
                       for p in sorted((self.root / "package").iterdir())]
            (self.root / "evidence" / "onedrive-package.json").write_text(json.dumps(entries))
        proc = self.bkp("build-evidence")
        ev = self.root / "evidence"
        load = lambda n: json.loads((ev / n).read_text()) if (ev / n).exists() else None
        return proc, load("evidence.json"), load("manifest.json")

    def dispositions(self, ev):
        return {o["object_class"]: o["disposition"] for o in ev["objects"]}

    def passed(self):
        self.assertEqual(self.bkp("preflight", TOKEN_OUTCOME="success").returncode, 0)

    # ---- entradas e preflight ----
    def test_entradas_invalidas_saem_com_2(self):
        for bad in ({"SOURCE_REPOSITORY": "semBarra"}, {"SOURCE_REPOSITORY": "a/b/c"}, {"DESTINATION": "../fora"}, {"DESTINATION": "/abs"}, {"DESTINATION": ""}):
            proc = self.bkp("validate-inputs", **bad)
            self.assertEqual(proc.returncode, 2, (bad, proc.stdout))
            self.assertEqual(self.result(proc).get("status"), "REJECTED")

    def test_entradas_validas(self):
        proc = self.bkp("validate-inputs")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(ta.REPO, proc.stdout)

    def test_preflight_sem_token_bloqueia_com_tres_restricoes(self):
        proc = self.bkp("preflight", TOKEN_OUTCOME="failure")
        self.assertEqual(proc.returncode, 10)
        required = self.result(proc)["required_restrictions"]
        self.assertEqual(sorted(r["restriction_id"] for r in required), sorted(f"RST-{c}-ACCESS_PERMISSION_GAP" for c in CLASSES))
        pf = json.loads((self.root / "evidence" / "preflight.json").read_text())
        self.assertEqual([a["object_class"] for a in pf["assessments"]], CLASSES)
        route = next(r for r in pf["assessments"][0]["routes"] if r["route_type"] == "DETERMINISTIC_EXECUTOR")
        self.assertEqual(route["route_reference"], "github_actions:backup-issues.yml")

    def test_preflight_com_token_passa_sem_outro_token(self):
        proc = self.bkp("preflight", TOKEN_OUTCOME="success")          # PROJECTS_TOKEN_OUTCOME e RESTORE_TOKEN_OUTCOME ausentes
        self.assertEqual((proc.returncode, self.result(proc)["preflight_status"]), (0, "PASS"))
        pf = json.loads((self.root / "evidence" / "preflight.json").read_text())
        self.assertEqual((len(pf["assessments"]), pf["gaps"], pf["factual_inventory_pending"]), (3, [], []))

    # ---- evidência e manifest ----
    def test_backup_completo_evidencia_e_manifest(self):
        self.passed()
        self.read_api()
        self.assertTrue((self.root / "package" / "repo-metadata.json").exists())
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((ev["capability_id"], man["capability_id"]), ("backup_issues", "backup_issues"))
        self.assertEqual(ev["source_scope"], {"source_repository": ta.REPO, "destination": "backups/x"})
        self.assertEqual(self.dispositions(ev), {c: EQUIVALENT for c in CLASSES})            # e nenhum objeto git
        self.assertEqual(man["status"], "COMPLETE_WITH_EXCEPTIONS")                          # a representação equivalente é uma limitação declarada
        self.assertEqual((man["reconciliation"]["equivalent"], man["reconciliation"]["failed"], man["reconciliation"]["not_verified"]), (3, 0, 0))
        issues_obj = next(o for o in ev["objects"] if o["object_class"] == "issues")
        self.assertIn("api-issues.json", issues_obj["evidence"])
        self.assertIn("api-issue-comments.json", issues_obj["evidence"])
        for name in ("api-issue-sub-issues.json", "api-issue-dependencies.json"):
            self.assertIn(name, issues_obj["evidence"])
        paths = [a["path"] for a in man["artifacts"]]
        for expected in ("backups/x/req-test-0001/package/api-issues.json", "backups/x/req-test-0001/package/repo-metadata.json", "backups/x/req-test-0001/package/api-issue-sub-issues.json", "backups/x/req-test-0001/package/api-issue-dependencies.json", "api-status.json"):
            self.assertIn(expected, paths)
        self.assertEqual({a["object_class"] for a in man["artifacts"]}, {"preservation-package", "evidence"})
        # Somente GET na origem e o token nunca vaza.
        self.assertEqual(set(ta.FakeGitHub.methods), {"GET"})
        text = "\n".join(self.outputs) + "".join(p.read_text(errors="replace") for p in (self.root / "evidence").glob("*"))
        self.assertNotIn(ta.TOKEN, text)

    def test_falha_de_uma_classe_vira_failed_sem_esconder_as_outras(self):
        ta.FakeGitHub.fail = {f"/repos/{ta.REPO}/issues": 403}
        self.passed()
        self.read_api()
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.dispositions(ev), {"labels": EQUIVALENT, "milestones": EQUIVALENT, "issues": "FAILED"})
        self.assertEqual((man["status"], man["reconciliation"]["failed"], man["reconciliation"]["equivalent"]), ("FAILED", 1, 2))

    def test_recurso_desativado_vira_preserved_sem_objetos(self):
        ta.FakeGitHub.fail = {f"/repos/{ta.REPO}/issues": 410}
        self.passed()
        self.read_api()
        proc, ev, man = self.finish()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.dispositions(ev)["issues"], "PRESERVED")
        self.assertEqual(man["reconciliation"]["preserved"], 1)

    def test_sem_token_e_restricoes_aceitas_ficam_not_verified(self):
        blocked = self.bkp("preflight", TOKEN_OUTCOME="failure")
        decision = {"previous_request_id": "req-test-0000", "hitl_decision": "CONTINUE_WITH_RESTRICTIONS",
                    "accepted_restrictions": self.result(blocked)["required_restrictions"]}
        self.env["PREFLIGHT_DECISION"] = json.dumps(decision)
        self.assertEqual(self.bkp("preflight", TOKEN_OUTCOME="failure").returncode, 0)
        proc, ev, man = self.finish(uploaded=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.dispositions(ev), {c: "NOT-VERIFIED" for c in CLASSES})
        self.assertEqual(man["reconciliation"]["not_verified"], 3)
        self.assertEqual(sorted(i for o in ev["objects"] for i in o["restriction_ids"]), sorted(f"RST-{c}-ACCESS_PERMISSION_GAP" for c in CLASSES))

    def test_leitura_nao_feita_vira_failed(self):
        self.passed()
        proc, ev, man = self.finish()                     # o leitor não rodou: sem api-status.json
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.dispositions(ev), {c: "FAILED" for c in CLASSES})

    def test_sem_pacote_no_destino_falha(self):
        self.passed()
        self.read_api()
        proc, ev, man = self.finish(uploaded=False)       # sem onedrive-package.json
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.dispositions(ev), {c: "FAILED" for c in CLASSES})

    def test_recusa_gerar_evidencia_sem_destino_validado(self):
        self.assertEqual(self.bkp("build-evidence", DESTINATION_VALIDATED="").returncode, 21)


if __name__ == "__main__":
    unittest.main()
