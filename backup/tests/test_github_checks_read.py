#!/usr/bin/env python3
"""Teste local do leitor github_checks_read.py e da evidência temporal da classe checks, contra uma API SIMULADA.

O QUE PROVA
    - conjunto candidato de commits: pontas de branches e tags (tag anotada resolvida pelo nome), refs/pull/*/head e
      head_sha dos runs, sem repetição; refs/pull/*/merge fica de fora;
    - leitura completa e paginada de check suites, check runs, anotações e commit statuses;
    - conferências: total_count, ids únicos, check run numa suite listada, anotações lidas contra annotations_count,
      status combinado recalculado a partir da lista; qualquer divergência vira FAILED;
    - commit sem dados = NO_DATA_FOUND (não é falha nem expirado); SHA desconhecido = COMMIT_NOT_RESOLVABLE (PARTIAL);
    - checks negados (403/404, repositório privado) NÃO derrubam a classe: CHECKS_DENIED, PARTIAL, e o cabeçalho
      X-Accepted-GitHub-Permissions vai para o status de evidência; statuses continuam sendo lidos;
    - sem a permissão de statuses (403) a classe é FAILED com a dica;
    - tetos de commits e de bytes (os PRs saem primeiro), repetição em 500 e em limite de taxa, só GET;
    - o token, nomes de check, contexto, app, branch, tag, URLs e SHAs/refs nas rotas NUNCA aparecem na saída pública;
    - evidência (bkp_repo.py): objeto checks com temporal_state, RETENTION_ALERT, MRBI e CRR observados; MRBI da
      execução = menor retenção entre as classes temporais.
    NÃO prova nada contra o GitHub real: isso só o run real do backup prova.

COMO RODAR
    python3 backup/tests/test_github_checks_read.py        (saída 0 = tudo certo)
"""
import contextlib
import http.server
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "backup" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import github_actions_read as ar  # noqa: E402
import github_api_read as g  # noqa: E402
import github_checks_read as c  # noqa: E402

TOKEN = "ghp_SEGREDO_DE_TESTE_123"
REPO = "dono/teste"
SECRETS = ["CHECK_SEGREDO_NOME", "CONTEXTO_SEGREDO", "TOKEN_SEGREDO_URL", "BRANCH_SEGREDA", "TAG_SEGREDA", "APP_SEGREDO",
           "TEXTO_SEGREDO_DA_ANOTACAO"]
S1, S2, S3, S4, S5, S6, S7, SM, TAGOBJ = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40, "9" * 40, "8" * 40, "7" * 40)


def make_data():
    runs1 = [
        {"id": 101, "name": "CHECK_SEGREDO_NOME", "check_suite": {"id": 11}, "app": {"slug": "github-actions"},
         "started_at": "2026-09-21T10:00:00Z", "output": {"annotations_count": 2}},
        {"id": 102, "name": "CHECK_SEGREDO_NOME", "check_suite": {"id": 11}, "app": {"slug": "APP_SEGREDO"},
         "started_at": "2026-09-21T10:01:00Z", "output": {"annotations_count": 0}},
    ]
    return {
        "suites": {S1: [{"id": 11, "created_at": "2026-09-21T09:59:00Z", "app": {"slug": "github-actions"}}],
                   S2: [{"id": 12, "created_at": "2026-09-22T09:59:00Z"}],
                   S3: [], S4: [], S5: [{"id": 15, "created_at": "2026-09-23T09:59:00Z"}], S6: []},
        "runs": {S1: runs1,
                 S2: [{"id": 201, "name": "CHECK_SEGREDO_NOME", "check_suite": {"id": 12}, "started_at": "2026-09-22T10:00:00Z",
                       "output": {"annotations_count": 0}}],
                 S3: [], S4: [], S5: [], S6: []},
        "annotations": {101: [{"path": "x.py", "message": "TEXTO_SEGREDO_DA_ANOTACAO 1"}, {"path": "y.py", "message": "TEXTO_SEGREDO_DA_ANOTACAO 2"}]},
        "statuses": {S2: [{"id": 1, "context": "CONTEXTO_SEGREDO", "state": "success", "created_at": "2026-09-20T10:00:00Z",
                           "target_url": "https://ci.example/TOKEN_SEGREDO_URL"}],
                     S4: [{"id": 3, "context": "ctx", "state": "failure", "created_at": "2026-09-24T10:00:00Z"},
                          {"id": 2, "context": "ctx", "state": "success", "created_at": "2026-09-23T10:00:00Z"}],
                     S1: [], S3: [], S5: [], S6: []},
        "tags": {"TAG_SEGREDA": S3},
    }


def write_inputs(root, refs=True, runs=True, attempts=False):
    ev, pk = root / "evidence", root / "package"
    ev.mkdir(exist_ok=True)
    pk.mkdir(exist_ok=True)
    if refs:
        (ev / "refs.tsv").write_text("\n".join([f"refs/heads/main\t{S1}\tcommit", f"refs/heads/BRANCH_SEGREDA\t{S2}\tcommit",
                                                f"refs/tags/TAG_SEGREDA\t{TAGOBJ}\ttag", f"refs/pull/1/head\t{S4}\tcommit",
                                                f"refs/pull/1/merge\t{SM}\tcommit", f"refs/heads/vazio\t{S6}\tcommit"]) + "\n")
    if runs:
        (pk / "api-actions-runs.json").write_text(json.dumps([{"id": 1, "head_sha": S1}, {"id": 2, "head_sha": S5}]))
    if attempts:
        (pk / "api-actions-attempts.json").write_text(json.dumps([{"run_id": 1, "run_attempt": 1, "jobs": [{"id": 101}]}]))


class Fake(http.server.BaseHTTPRequestHandler):
    data = {}
    fail = {}            # rota -> código HTTP fixo (sem cabeçalho de permissão)
    deny_checks = None   # código (403/404) devolvido nas rotas de check suites, check runs e anotações
    flaky = {}
    ratelimit = {}
    combined_bad = False
    runs_total_extra = 0
    ann_short = False
    statuses_dup = False
    methods = []
    seen_routes = []

    def log_message(self, *args):
        pass

    def _send(self, code, body=None, headers=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _paged(self, key, items, q, total=None):
        per, page = int(q.get("per_page", ["100"])[0]), int(q.get("page", ["1"])[0])
        headers = {}
        if page * per < len(items):
            nq = {k: v[0] for k, v in q.items()}
            nq["page"] = page + 1
            headers["Link"] = f'<http://{self.headers["Host"]}{urllib.parse.urlparse(self.path).path}?{urllib.parse.urlencode(nq)}>; rel="next"'
        chunk = items[(page - 1) * per: page * per]
        self._send(200, chunk if key is None else {"total_count": len(items) if total is None else total, key: chunk}, headers)

    def do_GET(self):  # noqa: N802
        cls = Fake
        cls.methods.append("GET")
        parsed = urllib.parse.urlparse(self.path)
        route, q = urllib.parse.unquote(parsed.path), urllib.parse.parse_qs(parsed.query)
        cls.seen_routes.append(route)
        if route in cls.fail:
            return self._send(cls.fail[route], {"message": "x"})
        if cls.flaky.get(route, 0) > 0:
            cls.flaky[route] -= 1
            return self._send(500, {"message": "boom"})
        if cls.ratelimit.get(route, 0) > 0:
            cls.ratelimit[route] -= 1
            return self._send(403, {"message": "rate"}, {"Retry-After": "1", "X-RateLimit-Remaining": "0"})
        d = cls.data
        p = route.split("/")                      # ['', 'repos', 'dono', 'teste', ...]
        if p[1:4] != ["repos", "dono", "teste"]:
            return self._send(404, {"message": "nf"})
        rest = p[4:]
        if rest[:1] == ["commits"] and len(rest) == 2:               # commit pelo nome da tag
            sha = d["tags"].get(rest[1])
            return self._send(200, {"sha": sha}) if sha else self._send(404, {"message": "nf"})
        if rest[:1] == ["commits"] and len(rest) == 3:
            sha, what = rest[1], rest[2]
            if sha not in d["statuses"]:
                return self._send(422 if sha == S7 else 404, {"message": "No commit found"})
            if what in ("check-suites", "check-runs", ) and cls.deny_checks:
                return self._send(cls.deny_checks, {"message": "denied"}, {"X-Accepted-GitHub-Permissions": "checks=read"})
            if what == "statuses":
                items = list(d["statuses"][sha])
                if cls.statuses_dup and items:
                    items = items + [dict(items[0])]
                return self._paged(None, items, q)
            if what == "status":
                items = d["statuses"][sha]
                latest = {}
                for s in items:
                    latest.setdefault(s["context"], s["state"])
                state = "failure" if set(latest.values()) & {"failure", "error"} else ("pending" if (not latest or "pending" in latest.values()) else "success")
                if cls.combined_bad:
                    state = "success" if state != "success" else "failure"
                body = {"state": state, "total_count": len(latest), "sha": sha}
                per, page = int(q.get("per_page", ["100"])[0]), int(q.get("page", ["1"])[0])
                body["statuses"] = items[(page - 1) * per: page * per]
                headers = {}
                if page * per < len(items):
                    nq = {k: v[0] for k, v in q.items()}
                    nq["page"] = page + 1
                    headers["Link"] = f'<http://{self.headers["Host"]}{parsed.path}?{urllib.parse.urlencode(nq)}>; rel="next"'
                return self._send(200, body, headers)
            if what == "check-suites":
                return self._paged("check_suites", d["suites"][sha], q)
            if what == "check-runs":
                return self._paged("check_runs", d["runs"][sha], q, len(d["runs"][sha]) + cls.runs_total_extra)
        if rest[:1] == ["check-runs"] and len(rest) == 3 and rest[2] == "annotations":
            if cls.deny_checks:
                return self._send(cls.deny_checks, {"message": "denied"}, {"X-Accepted-GitHub-Permissions": "checks=read"})
            items = d["annotations"].get(int(rest[1]), [])
            return self._paged(None, items[:-1] if cls.ann_short else items, q)
        return self._send(404, {"message": "nf"})


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
        Fake.fail, Fake.flaky, Fake.ratelimit, Fake.deny_checks = {}, {}, {}, None
        Fake.combined_bad, Fake.runs_total_extra, Fake.ann_short, Fake.statuses_dup = False, 0, False, False
        Fake.methods, Fake.seen_routes = [], []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        write_inputs(self.root)
        for p in (unittest.mock.patch.object(ar, "sleep", lambda s: None), unittest.mock.patch.object(g, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_read(self, **env):
        environ = {"SOURCE_REPOSITORY": REPO, "SOURCE_TOKEN": TOKEN, "EVIDENCE_DIR": str(self.root / "evidence"),
                   "PACKAGE_DIR": str(self.root / "package"), "GITHUB_API_URL": self.base}
        environ.update(env)
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, environ), contextlib.redirect_stdout(out):
            c.cmd_read()
        inv = json.loads((self.root / "evidence" / "api-checks-status.json").read_text())
        return out.getvalue(), inv["classes"]["checks"], inv

    def package_names(self):
        return sorted(p.name for p in (self.root / "package").iterdir())

    def clean(self, text):
        self.assertNotIn(TOKEN, text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        for sha in (S1, S2, S3, S4, S5, S6, S7, TAGOBJ):
            self.assertNotIn(sha, text)

    def drop_unknown(self):
        """Sem o SHA que a API desconhece (para testar o caminho OK)."""
        (self.root / "evidence" / "refs.tsv").write_text(
            (self.root / "evidence" / "refs.tsv").read_text() + "")

    # ---- conjunto candidato e leitura ----
    def test_candidatos_sem_repeticao_e_leitura_completa(self):
        with unittest.mock.patch.object(c, "PER_PAGE", 1):
            out, st, inv = self.run_read()
        states = {i["sha"]: i["state"] for i in inv["items"]}
        # S1 e S5 (runs), S2 e S6 (branches), S3 (tag anotada, resolvida), S4 (PR). refs/pull/1/merge fica de fora.
        self.assertEqual(sorted(states), sorted([S1, S2, S3, S4, S5, S6]))
        self.assertNotIn(SM, states)
        self.assertEqual(states[S1], "AVAILABLE")
        self.assertEqual(states[S6], "NO_DATA_FOUND")
        self.assertEqual(states[S3], "NO_DATA_FOUND")
        self.assertEqual(next(i for i in inv["items"] if i["sha"] == S1)["sources"], ["branch", "run"])
        self.assertEqual(next(i for i in inv["items"] if i["sha"] == S3)["sources"], ["tag"])
        self.assertEqual(next(i for i in inv["items"] if i["sha"] == S4)["sources"], ["pull"])
        self.assertEqual(st["status"], "OK", st)
        self.assertTrue(all(x["ok"] for x in st["checks"]), st["checks"])
        e = st["extra"]
        self.assertEqual((e["check_suites"], e["check_runs"], e["annotations"], e["statuses"]), (3, 3, 2, 3))
        self.assertEqual(e["temporal_counts"], {"AVAILABLE": 4, "NO_DATA_FOUND": 2})
        self.assertEqual(e["oldest_available_created_at"], "2026-09-20T10:00:00Z")
        self.assertEqual(self.package_names(), ["api-actions-runs.json", "api-checks-annotations.json", "api-checks-commits.json",
                                                "api-checks-inventory.json", "api-checks-runs.json", "api-checks-suites.json",
                                                "api-commit-statuses.json"])
        stat = {x["sha"]: x for x in json.loads((self.root / "package" / "api-commit-statuses.json").read_text())}
        self.assertEqual(stat[S4]["combined"]["state"], "failure")
        self.assertEqual(len(json.loads((self.root / "package" / "api-checks-annotations.json").read_text())[0]["annotations"]), 2)
        self.assertEqual(set(Fake.methods), {"GET"})
        self.clean(out)

    def test_log_publico_so_contagens_e_tamanhos(self):
        out, st, inv = self.run_read()
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("API_READ checks: OK commits=6/6 suites=3 runs=3 annotations=2 statuses=3 no_data=2 bytes="), lines[0])
        self.clean(out)
        self.assertIn("CHECK_SEGREDO_NOME", json.dumps(json.loads((self.root / "package" / "api-checks-runs.json").read_text())))

    def test_cruzamento_informativo_com_a_classe_actions(self):
        write_inputs(self.root, attempts=True)
        out, st, inv = self.run_read()
        self.assertEqual(st["extra"]["actions_crosscheck"], {"available": True, "github_actions_check_runs": 1, "found_in_actions_jobs": 1})

    def test_sem_refs_e_sem_runs_ainda_le_o_que_tem_e_anota(self):
        write_inputs(self.root, refs=False, runs=True)
        (self.root / "evidence" / "refs.tsv").unlink()
        out, st, inv = self.run_read()
        self.assertEqual(sorted(i["sha"] for i in inv["items"]), sorted([S1, S5]))
        self.assertTrue(any("refs.tsv not found" in n for n in st["extra"]["notes"]))
        (self.root / "package" / "api-actions-runs.json").unlink()
        out, st, inv = self.run_read()
        self.assertTrue(any("actions class not read" in n for n in st["extra"]["notes"]))

    # ---- estados que não são sucesso ----
    def test_sha_desconhecido_vira_commit_not_resolvable_e_partial(self):
        (self.root / "evidence" / "refs.tsv").write_text(f"refs/heads/main\t{S1}\tcommit\nrefs/heads/x\t{S7}\tcommit\n")
        out, st, inv = self.run_read()
        self.assertEqual({i["sha"]: i["state"] for i in inv["items"]}[S7], "COMMIT_NOT_RESOLVABLE")
        self.assertEqual(st["status"], "PARTIAL")
        self.assertIn("COMMIT_NOT_RESOLVABLE", st["detail"])
        self.assertTrue(all(x["ok"] for x in st["checks"]))

    def test_tag_anotada_nao_resolvida_vira_lacuna(self):
        Fake.data["tags"] = {}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "PARTIAL")
        self.assertTrue(any(i["sha"] is None and i["state"] == "COMMIT_NOT_RESOLVABLE" for i in inv["items"]))
        self.clean(out)

    def test_checks_negados_nao_derrubam_a_classe(self):
        for code in (403, 404):
            Fake.deny_checks = code
            out, st, inv = self.run_read()
            self.assertEqual(st["status"], "PARTIAL", st)
            self.assertIn("GitHub App with Checks: Read", st["detail"])
            states = {i["sha"]: i["state"] for i in inv["items"]}
            self.assertTrue(set(states.values()) <= {"CHECKS_DENIED"})
            self.assertEqual(st["extra"]["denied_permission_headers"], ["checks=read"])
            self.assertEqual(st["extra"]["statuses"], 3)               # statuses continuam sendo lidos
            self.assertTrue(all(x["ok"] for x in st["checks"]))
            self.clean(out)

    def test_statuses_negados_sao_fatais_com_a_dica(self):
        Fake.fail = {f"/repos/{REPO}/commits/{S1}/statuses": 403}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("Commit statuses", st["detail"])
        self.assertIn("/commits/{ref}/statuses", st["detail"])
        self.assertEqual(self.package_names(), ["api-actions-runs.json", "api-checks-inventory.json"])
        self.clean(out + json.dumps(st))

    # ---- conferências que reprovam ----
    def test_status_combinado_divergente_vira_failed(self):
        Fake.combined_bad = True
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("combined_status_matches_statuses", st["detail"])

    def test_total_de_check_runs_divergente_vira_failed(self):
        Fake.runs_total_extra = 1
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("check_runs_read_equal_total_count", st["detail"])

    def test_anotacoes_a_menos_viram_failed(self):
        Fake.ann_short = True
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("annotations_read_equal_annotations_count", st["detail"])

    def test_check_run_numa_suite_nao_listada_vira_failed(self):
        Fake.data["suites"][S1] = [{"id": 99, "created_at": "2026-09-21T09:59:00Z"}]
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("check_runs_belong_to_listed_suites", st["detail"])

    def test_ids_de_status_repetidos_viram_failed(self):
        Fake.statuses_dup = True
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("statuses_ids_unique", st["detail"])

    def test_500_persistente_vira_item_failed(self):
        Fake.flaky = {f"/repos/{REPO}/commits/{S2}/statuses": 99}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertEqual(next(i for i in inv["items"] if i["sha"] == S2)["state"], "FAILED")

    # ---- tetos ----
    def test_teto_de_commits_deixa_os_prs_para_depois(self):
        out, st, inv = self.run_read(CHECKS_MAX_COMMITS="5")
        states = {i["sha"]: i["state"] for i in inv["items"]}
        self.assertEqual(states[S4], "SKIPPED_OVER_CAP")                  # o único candidato de PR é o último
        self.assertEqual(st["status"], "PARTIAL")
        self.assertIn("SKIPPED_OVER_CAP", st["detail"])
        out, st, inv = self.run_read(CHECKS_MAX_COMMITS="1")
        self.assertEqual(sum(1 for i in inv["items"] if i["state"] == "SKIPPED_OVER_CAP"), 5)

    def test_teto_de_bytes(self):
        out, st, inv = self.run_read(CHECKS_MAX_BYTES="1")
        states = [i["state"] for i in inv["items"]]
        self.assertEqual(states.count("SKIPPED_OVER_CAP"), len(states) - 1)
        self.assertEqual(st["status"], "PARTIAL")

    # ---- repetição, GET, vazamentos ----
    def test_repete_em_500_e_em_limite_de_taxa_curto(self):
        Fake.flaky = {f"/repos/{REPO}/commits/{S1}/check-suites": 2}
        Fake.ratelimit = {f"/repos/{REPO}/commits/{S2}/status": 1}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "OK")

    def test_so_get_e_token_nao_vaza_nos_arquivos(self):
        out, st, inv = self.run_read()
        self.assertEqual(set(Fake.methods), {"GET"})
        for p in list((self.root / "evidence").glob("*")) + list((self.root / "package").glob("*.json")):
            self.assertNotIn(TOKEN, p.read_text(errors="replace"))

    def test_excecao_inesperada_vira_failed_sem_vazar_a_mensagem(self):
        with unittest.mock.patch.object(c, "read_checks", side_effect=RuntimeError("TEXTO_SEGREDO_DA_ANOTACAO")):
            out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("RuntimeError", st["detail"])
        self.clean(out + json.dumps(st))

    def test_variaveis_e_tetos_invalidos_saem_com_64(self):
        env = {k: v for k, v in os.environ.items() if k not in ("SOURCE_TOKEN", "SOURCE_REPOSITORY")}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "github_checks_read.py"), "read"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 64)
        with unittest.mock.patch.dict(os.environ, {"SOURCE_REPOSITORY": REPO, "SOURCE_TOKEN": TOKEN, "CHECKS_MAX_COMMITS": "muitos"}):
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
                c.cmd_read()
        self.assertEqual(ctx.exception.code, 64)

    def test_mascara_das_rotas(self):
        self.assertEqual(c.mask(f"/repos/{REPO}/commits/{S1}/check-runs"), f"/repos/{REPO}/commits/{{ref}}/check-runs")
        self.assertEqual(c.mask(f"/repos/{REPO}/commits/TAG_SEGREDA"), f"/repos/{REPO}/commits/{{ref}}")
        self.assertEqual(c.mask(f"/repos/{REPO}/check-runs/101/annotations"), f"/repos/{REPO}/check-runs/{{id}}/annotations")


class TemporalEvidenceTest(unittest.TestCase):
    """bkp_repo.temporal_summary / merge_temporal para checks e o build-evidence ponta a ponta."""

    @classmethod
    def setUpClass(cls):
        import bkp_repo
        cls.b = bkp_repo
        obs = {"repository": "Dono/Teste", "retention_days": 90, "source": "MANUAL_SETTINGS_CHECK", "checked_on": "2026-10-17"}
        cls.cap = {"temporal_observations": {"checks": [obs], "actions": [dict(obs, retention_days=60)]}}

    def st(self, counts, oldest="2026-09-21T10:00:00Z"):
        return {"extra": {"temporal_counts": counts, "oldest_available_created_at": oldest}}

    def test_checks_usa_a_observacao_da_propria_classe(self):
        t = self.b.temporal_summary(self.st({"AVAILABLE": 2, "NO_DATA_FOUND": 1}), "dono/teste", self.cap, "PRESERVED-AS-EQUIVALENT-REPRESENTATION", "checks")
        self.assertEqual((t["state"], t["mrbi"]), ("AVAILABLE", "90 days"))
        self.assertIn("manual check", t["crr"])
        a = self.b.temporal_summary(self.st({"AVAILABLE": 1}), "dono/teste", self.cap, "PRESERVED-AS-EQUIVALENT-REPRESENTATION")
        self.assertEqual(a["mrbi"], "60 days")                                      # actions segue com a sua

    def test_sem_dado_capturado_nao_afirma_estado_temporal(self):
        t = self.b.temporal_summary(self.st({"NO_DATA_FOUND": 3}, oldest=None), "dono/teste", self.cap, "PRESERVED-AS-EQUIVALENT-REPRESENTATION", "checks")
        self.assertEqual(t["state"], "NOT_VERIFIED")
        self.assertEqual(self.b.temporal_summary(self.st({}), "dono/teste", self.cap, "FAILED", "checks")["state"], "NOT_VERIFIED")

    def test_alerta_dentro_de_30_dias_e_sem_observacao(self):
        import datetime
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=70)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.b.temporal_summary(self.st({"AVAILABLE": 1}, oldest=old), "dono/teste", self.cap, "PRESERVED-AS-EQUIVALENT-REPRESENTATION", "checks")["alerts"], ["RETENTION_ALERT"])
        t = self.b.temporal_summary(self.st({"AVAILABLE": 1}), "outro/repo", {}, "PARTIALLY-PRESERVED", "checks")
        self.assertEqual((t["mrbi"], t["crr"], t["alerts"]), (None, None, []))
        self.assertTrue(any("checks and commit statuses" in n for n in t["notes"]))

    def test_merge_usa_a_menor_retencao_e_une_alertas(self):
        a = self.b.temporal_summary(self.st({"AVAILABLE": 1}), "dono/teste", self.cap, "PRESERVED-AS-EQUIVALENT-REPRESENTATION")           # 60
        k = self.b.temporal_summary(self.st({"AVAILABLE": 1}), "dono/teste", self.cap, "PRESERVED-AS-EQUIVALENT-REPRESENTATION", "checks")  # 90
        m = self.b.merge_temporal([k, a])
        self.assertEqual(m["mrbi"], "60 days")
        self.assertEqual(self.b.merge_temporal([k])["mrbi"], "90 days")
        self.assertEqual(self.b.merge_temporal([])["mrbi"], None)


class EvidenceTest(unittest.TestCase):
    """build-evidence ponta a ponta (backup_repository) com checks lida pela API simulada."""

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
        Fake.fail, Fake.flaky, Fake.ratelimit, Fake.deny_checks = {}, {}, {}, None
        Fake.combined_bad, Fake.runs_total_extra, Fake.ann_short, Fake.statuses_dup = False, 0, False, False
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        write_inputs(self.root)
        (self.root / "evidence" / "refs.tsv").write_text(f"refs/heads/main\t{S1}\tcommit\nrefs/heads/BRANCH_SEGREDA\t{S2}\tcommit\n")
        self.env = {k: v for k, v in os.environ.items() if k not in ("TOKEN_OUTCOME", "PROJECTS_TOKEN_OUTCOME", "PREFLIGHT_DECISION")}
        self.env.update(CAPABILITY_ID="backup_repository", EVIDENCE_DIR=str(self.root / "evidence"), PACKAGE_DIR=str(self.root / "package"),
                        REQUEST_ID="req-test-0001", DESTINATION="backups/x", DESTINATION_VALIDATED="true", SOURCE_REPOSITORY="Moriblo/MIT")

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, script, *args, **env):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], env=dict(self.env, **env), capture_output=True, text=True, cwd=ROOT)

    def decision(self):
        pf = self.run_script("bkp_repo.py", "preflight", TOKEN_OUTCOME="success")
        res = json.loads([l for l in pf.stdout.splitlines() if l.startswith("BKP_RESULT ")][-1][len("BKP_RESULT "):])
        classes = {r["object_class"] for r in res["required_restrictions"]}
        self.assertNotIn("checks", classes)                                   # checks deixou de ser restrição
        self.assertEqual(len(classes), 12)                                     # 11 classes + projects (sem PROJECTS_TOKEN_OUTCOME)
        self.assertIn("projects", classes)
        d = json.dumps({"previous_request_id": "req-prev-0001", "hitl_decision": "CONTINUE_WITH_RESTRICTIONS",
                        "accepted_restrictions": res["required_restrictions"]})
        self.assertEqual(self.run_script("bkp_repo.py", "preflight", TOKEN_OUTCOME="success", PREFLIGHT_DECISION=d).returncode, 0)
        return d

    def finish(self, d):
        pk = self.root / "package"
        (self.root / "evidence" / "onedrive-package.json").write_text(json.dumps(
            [{"remote_path": f"backups/x/req-test-0001/package/{p.name}", "sha256": "0" * 64, "bytes": 1} for p in sorted(pk.iterdir())]))
        proc = self.run_script("bkp_repo.py", "build-evidence", PREFLIGHT_DECISION=d)
        ev = json.loads((self.root / "evidence" / "evidence.json").read_text())
        return proc, ev

    def minimal_other_classes(self, actions=True):
        (self.root / "evidence" / "api-status.json").write_text(json.dumps({"classes": {
            k: {"status": "OK", "count": 0, "files": [], "checks": []} for k in ("labels", "milestones", "issues")}}))
        if actions:
            (self.root / "evidence" / "api-actions-status.json").write_text(json.dumps({"classes": {"actions": {
                "status": "OK", "count": 0, "files": [], "checks": [],
                "extra": {"temporal_counts": {"AVAILABLE": 1}, "observed_artifact_retention_days": [],
                          "oldest_available_created_at": "2026-09-21T10:00:00Z"}}}}))

    def test_evidencia_com_checks_temporal_e_mrbi_crr_observados(self):
        d = self.decision()
        read = self.run_script("github_checks_read.py", "read", SOURCE_TOKEN=TOKEN, GITHUB_API_URL=self.base, SOURCE_REPOSITORY=REPO)
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        self.minimal_other_classes()
        proc, ev = self.finish(d)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        obj = next(o for o in ev["objects"] if o["object_class"] == "checks")
        self.assertEqual((obj["disposition"], obj["temporal_state"]), ("PRESERVED-AS-EQUIVALENT-REPRESENTATION", "AVAILABLE"))
        self.assertIn("api-checks-status.json", obj["evidence"])
        self.assertIn("api-commit-statuses.json", obj["evidence"])
        self.assertIn("NO_DATA_FOUND", obj["limitation"])
        tp = ev["execution"]["temporal_preservation"]
        self.assertEqual(tp["mrbi"], "90 days")                                # MIT: 90 dias nas duas classes
        self.assertIn("observed", tp["crr"])
        self.assertNotIn(TOKEN, json.dumps(ev))

    def test_sem_a_leitura_a_classe_checks_fica_failed(self):
        d = self.decision()
        self.minimal_other_classes()
        proc, ev = self.finish(d)
        obj = next(o for o in ev["objects"] if o["object_class"] == "checks")
        self.assertEqual((obj["disposition"], obj["temporal_state"]), ("FAILED", "NOT_VERIFIED"))
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
