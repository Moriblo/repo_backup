#!/usr/bin/env python3
"""Teste local do leitor github_api_read.py, contra um servidor de API SIMULADO.

O QUE PROVA
    - leitura completa e paginada de labels, milestones e issues (com 2 itens por página);
    - separação dos pull requests (ficam de fora das issues, só o número vai ao inventário);
    - conferências: contagem de abertos, comentários por issue, etiquetas e marcos citados;
    - falhas viram FAILED com causa (403) ou DISABLED (410); nunca "vazio com sucesso";
    - repetição em erro 500 e em limite de taxa curto;
    - só requisições GET, e o token nunca aparece nos arquivos gerados nem na saída.
    NÃO prova nada contra o GitHub real: isso só o run real do backup prova.

COMO RODAR
    python3 backup/tests/test_github_api_read.py        (saída 0 = tudo certo)
"""
import contextlib
import http.server
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import github_api_read as g  # noqa: E402

TOKEN = "ghp_SEGREDO_DE_TESTE_123"
REPO = "dono/teste"


def make_data():
    """Dados que espelham o repositório de teste: 6 issues, 5 comentários, 1 marco, 1 PR."""
    ms = {"number": 1, "title": "Marco de teste", "state": "open"}
    labels = [{"name": n, "color": "ededed", "description": f"{n} (teste)"}
              for n in ("bug", "teste-backup", "prioridade-alta", "documentacao-teste")]
    lab = lambda *names: [{"name": n} for n in names]
    issues = [
        {"number": 1, "title": "Erro ao salvar a configuração", "state": "open", "comments": 2,
         "labels": lab("bug", "teste-backup"), "milestone": ms, "reactions": {"total_count": 1}},
        {"number": 2, "title": "Melhorar a documentação inicial", "state": "open", "comments": 1,
         "labels": lab("documentacao-teste"), "milestone": None, "reactions": {"total_count": 0}},
        {"number": 3, "title": "Atender chamado urgente", "state": "open", "comments": 1,
         "labels": lab("teste-backup", "prioridade-alta"), "milestone": ms, "reactions": {"total_count": 0}},
        {"number": 4, "title": "Ideia: exportar relatório ✔ ©", "state": "open", "comments": 0,
         "labels": [], "milestone": None, "reactions": {"total_count": 0}},
        {"number": 5, "title": "Revisar a política de senhas", "state": "open", "comments": 0,
         "labels": [], "milestone": None, "reactions": {"total_count": 0}},
        {"number": 6, "title": "Tarefa já concluída", "state": "closed", "comments": 1,
         "labels": [], "milestone": None, "reactions": {"total_count": 0}},
        # Pull request: aparece na API de issues com o campo pull_request.
        {"number": 7, "title": "PR de teste", "state": "open", "comments": 0, "labels": [], "milestone": None,
         "pull_request": {"url": "x"}, "reactions": {"total_count": 0}},
    ]
    base = f"https://api.github.com/repos/{REPO}/issues"
    comments = [{"id": 100 + k, "issue_url": f"{base}/{n}", "body": f"comentário {k}", "reactions": {"total_count": 0}}
                for k, n in enumerate([1, 1, 2, 3, 6])]
    comments[0]["reactions"] = {"total_count": 1}
    events = [{"id": 900 + k, "event": "labeled", "issue": {"number": n, "title": "texto grande"}}
              for k, n in enumerate([1, 1, 3, 7])]
    return {"labels": labels, "milestones": [ms], "issues": issues, "comments": comments, "events": events}


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    """Servidor simulado. Atributos de classe controlam cenários de falha."""
    data = {}
    fail = {}          # rota -> código HTTP fixo
    flaky = {}         # rota -> quantas respostas 500 antes de responder certo
    ratelimit = {}     # rota -> quantas respostas 403 com Retry-After antes de responder certo
    open_count = None  # sobrescreve o open_issues_count
    methods = []
    seen_tokens = set()

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

    def do_GET(self):  # noqa: N802
        FakeGitHub.methods.append("GET")
        FakeGitHub.seen_tokens.add(self.headers.get("Authorization"))
        parsed = urllib.parse.urlparse(self.path)
        route, q = parsed.path, urllib.parse.parse_qs(parsed.query)
        cls = FakeGitHub
        if route in cls.fail:
            return self._send(cls.fail[route], {"message": "x"})
        if cls.flaky.get(route, 0) > 0:
            cls.flaky[route] -= 1
            return self._send(500, {"message": "boom"})
        if cls.ratelimit.get(route, 0) > 0:
            cls.ratelimit[route] -= 1
            return self._send(403, {"message": "rate"}, {"Retry-After": "1", "X-RateLimit-Remaining": "0"})
        d = cls.data
        if route == f"/repos/{REPO}":
            opened = sum(1 for i in d["issues"] if i["state"] == "open")
            return self._send(200, {"open_issues_count": cls.open_count if cls.open_count is not None else opened,
                                    "full_name": REPO, "private": True, "visibility": "private", "description": "Repositório de teste",
                                    "topics": ["backup", "teste"], "default_branch": "main", "archived": False, "token_like": "NAO_COPIAR"})
        lists = {f"/repos/{REPO}/labels": d["labels"], f"/repos/{REPO}/milestones": d["milestones"],
                 f"/repos/{REPO}/issues": d["issues"], f"/repos/{REPO}/issues/comments": d["comments"],
                 f"/repos/{REPO}/issues/events": d["events"]}
        if route in lists:
            return self._paged(lists[route], q, route)
        if route.endswith("/reactions"):
            return self._send(200, [{"id": 1, "content": "+1"}])
        self._send(404, {"message": "Not Found"})

    def _paged(self, items, q, route):
        per = int(q.get("per_page", ["30"])[0])
        page = int(q.get("page", ["1"])[0])
        chunk = items[(page - 1) * per: page * per]
        headers = {}
        if page * per < len(items):
            host = f"http://127.0.0.1:{self.server.server_address[1]}"
            headers["Link"] = f'<{host}{route}?per_page={per}&page={page + 1}>; rel="next"'
        self._send(200, chunk, headers)


class ReadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        g.PER_PAGE = 2          # força paginação com poucos itens
        g.sleep = lambda s: None  # não esperar de verdade

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeGitHub.data = make_data()
        FakeGitHub.fail, FakeGitHub.flaky, FakeGitHub.ratelimit = {}, {}, {}
        FakeGitHub.open_count = None
        FakeGitHub.methods, FakeGitHub.seen_tokens = [], set()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_read(self):
        env = {"SOURCE_TOKEN": TOKEN, "SOURCE_REPOSITORY": REPO, "EVIDENCE_DIR": str(self.root / "evidence"),
               "PACKAGE_DIR": str(self.root / "package"),
               "GITHUB_API_URL": f"http://127.0.0.1:{self.server.server_address[1]}"}
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            g.cmd_read()
        self.out = out.getvalue()
        return json.loads((self.root / "evidence" / "api-status.json").read_text())

    def load(self, name):
        return json.loads((self.root / "package" / name).read_text())

    def test_leitura_completa(self):
        st = self.run_read()["classes"]
        self.assertEqual({c: st[c]["status"] for c in g.CLASSES}, {"labels": "OK", "milestones": "OK", "issues": "OK"})
        self.assertEqual((st["labels"]["count"], st["milestones"]["count"], st["issues"]["count"]), (4, 1, 6))
        # PR fora das issues, mas listado no inventário.
        self.assertEqual([i["number"] for i in self.load("api-issues.json")], [1, 2, 3, 4, 5, 6])
        self.assertEqual(st["issues"]["extra"]["pull_requests_skipped"], [7])
        # 5 comentários; eventos do PR (issue 7) descartados e issue embutida reduzida ao número.
        self.assertEqual(len(self.load("api-issue-comments.json")), 5)
        events = self.load("api-issue-events.json")
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["issue"].keys() == {"number"} for e in events))
        # Reações detalhadas só onde há reações (issue 1 e o primeiro comentário).
        self.assertEqual([r["target"] for r in self.load("api-issue-reactions.json")], ["issue:1", "comment:100"])
        # Acentos preservados no arquivo (sem \u escapado).
        self.assertIn("configuração", (self.root / "package" / "api-issues.json").read_text(encoding="utf-8"))
        # Inventário traz SHA-256 coerente com o arquivo.
        import hashlib
        entry = st["labels"]["files"][0]
        self.assertEqual(entry["sha256"], hashlib.sha256((self.root / "package" / "api-labels.json").read_bytes()).hexdigest())

    def test_metadados_do_repositorio_para_a_restauracao(self):
        status = self.run_read()
        meta = self.load("repo-metadata.json")
        self.assertEqual((meta["schema"], meta["full_name"], meta["visibility"], meta["default_branch"]),
                         ("repo-metadata/1", REPO, "private", "main"))
        self.assertEqual((meta["description"], meta["topics"]), ("Repositório de teste", ["backup", "teste"]))
        self.assertNotIn("token_like", meta)                 # só os campos da lista são copiados
        self.assertTrue(status["repo_metadata"]["written"])

    def test_falha_nos_metadados_nao_derruba_as_classes(self):
        FakeGitHub.fail = {f"/repos/{REPO}": 403}
        status = self.run_read()
        self.assertFalse((self.root / "package" / "repo-metadata.json").exists())
        self.assertFalse(status["repo_metadata"]["written"])
        self.assertIn("403", status["repo_metadata"]["error"])
        self.assertEqual(status["classes"]["labels"]["status"], "OK")

    def test_somente_get_e_token_nao_vaza(self):
        self.run_read()
        self.assertEqual(set(FakeGitHub.methods), {"GET"})
        self.assertEqual(FakeGitHub.seen_tokens, {f"Bearer {TOKEN}"})
        self.assertNotIn(TOKEN, self.out)
        for p in self.root.rglob("*.json"):
            self.assertNotIn(TOKEN, p.read_text(encoding="utf-8"))

    def test_permissao_negada_vira_failed(self):
        FakeGitHub.fail[f"/repos/{REPO}/issues"] = 403
        st = self.run_read()["classes"]
        self.assertEqual(st["labels"]["status"], "OK")
        self.assertEqual(st["issues"]["status"], "FAILED")
        self.assertIn("HTTP 403", st["issues"]["detail"])
        self.assertIn("Issues: Read", st["issues"]["detail"])
        self.assertFalse((self.root / "package" / "api-issues.json").exists())

    def test_recurso_desativado_vira_disabled(self):
        FakeGitHub.fail[f"/repos/{REPO}/issues"] = 410
        st = self.run_read()["classes"]
        self.assertEqual(st["issues"]["status"], "DISABLED")
        self.assertEqual(st["issues"]["count"], 0)

    def test_contagem_de_abertos_divergente(self):
        FakeGitHub.open_count = 99
        st = self.run_read()["classes"]["issues"]
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("open_issues_count", st["detail"])

    def test_comentario_faltando(self):
        FakeGitHub.data["comments"].pop()  # a issue 6 diz ter 1 comentário, mas nenhum foi devolvido
        st = self.run_read()["classes"]["issues"]
        self.assertEqual(st["status"], "FAILED")
        mismatch = next(c for c in st["checks"] if c["check"] == "comments_per_issue")
        self.assertEqual(mismatch["mismatched_issues"], [6])

    def test_etiqueta_e_marco_inexistentes(self):
        FakeGitHub.data["labels"].pop(0)           # remove "bug"
        FakeGitHub.data["milestones"].clear()      # remove o marco
        st = self.run_read()["classes"]["issues"]
        self.assertEqual(st["status"], "FAILED")
        checks = {c["check"]: c for c in st["checks"]}
        self.assertEqual(checks["labels_exist"]["missing"], ["bug"])
        self.assertEqual(checks["milestones_exist"]["missing"], [1])

    def test_repete_erro_500_e_limite_de_taxa(self):
        FakeGitHub.flaky[f"/repos/{REPO}/labels"] = 2
        FakeGitHub.ratelimit[f"/repos/{REPO}/milestones"] = 1
        st = self.run_read()["classes"]
        self.assertEqual(st["labels"]["status"], "OK")
        self.assertEqual(st["milestones"]["status"], "OK")

    def test_500_persistente_falha(self):
        FakeGitHub.flaky[f"/repos/{REPO}/labels"] = 99
        st = self.run_read()["classes"]
        self.assertEqual(st["labels"]["status"], "FAILED")
        self.assertEqual(st["milestones"]["status"], "OK")

    def test_variavel_ausente_sai_com_64(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
                g.cmd_read()
        self.assertEqual(cm.exception.code, 64)


if __name__ == "__main__":
    import unittest.mock  # noqa: F401  (usado pelos testes)
    unittest.main(verbosity=2)
