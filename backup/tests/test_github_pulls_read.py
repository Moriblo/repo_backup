#!/usr/bin/env python3
"""Teste local do leitor github_pulls_read.py e da evidência da classe pull_requests, contra uma API SIMULADA (REST + GraphQL).

O QUE PROVA
    - leitura completa e paginada dos PRs de todos os estados, com commits, arquivos, reviews, comentários de review,
      comentários de conversa, eventos, threads de review (comentários paginados dentro de cada thread) e relações
      (base e head, referências de fechamento, referências cruzadas e conexões);
    - conferências: contadores do PR, ids únicos, merged coerente, rótulos conhecidos, lista de PRs contra os
      números do leitor de issues (estrita só se a classe issues foi lida completa; senão informativa); qualquer
      divergência vira FAILED;
    - limites declarados viram LIMITED / PARTIAL, nunca FAILED: mais de 250 commits, mais de 3000 arquivos, threads
      indisponíveis, total de comentários das threads diferente do da REST (com as duas contagens na evidência);
    - arquivo sem patch é limitação registrada (contada), não falha;
    - tetos de PRs e de bytes; repetição em 500 e em limite de taxa; 403 com a dica de permissão;
    - só GET na REST e só consultas na GraphQL (mutation recusada antes de enviar);
    - o token, títulos, branches, nomes de arquivo, usuários, mensagens e conteúdo NUNCA aparecem na saída pública;
    - evidência (bkp_repo.py): objeto pull_requests (só registro), sem campos temporais.
    NÃO prova nada contra o GitHub real: isso só o run real do backup prova.

COMO RODAR
    python3 backup/tests/test_github_pulls_read.py        (saída 0 = tudo certo)
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
import github_pulls_read as p  # noqa: E402

TOKEN = "ghp_SEGREDO_DE_TESTE_123"
REPO = "dono/teste"
SECRETS = ["TITULO_SEGREDO", "BRANCH_SEGREDA", "ARQUIVO_SEGREDO", "USUARIO_SEGREDO", "CORPO_SEGREDO", "MENSAGEM_SEGREDA", "DIFF_SEGREDO"]


def make_pr(n, merged=False, state="open", draft=False, commits=2, files=2, rc=2, cm=1):
    return {"number": n, "title": "TITULO_SEGREDO", "state": "closed" if merged or state == "closed" else "open", "draft": draft,
            "merged": merged, "merged_at": "2026-09-22T10:00:00Z" if merged else None, "merge_commit_sha": "f" * 40 if merged else None,
            "user": {"login": "USUARIO_SEGREDO"}, "labels": [{"name": "bug"}],
            "base": {"ref": "main", "sha": "a" * 40, "repo": {"full_name": REPO}},
            "head": {"ref": "BRANCH_SEGREDA", "sha": f"{n:040x}", "repo": {"full_name": "fork/teste"}},
            "commits": commits, "changed_files": files, "review_comments": rc, "comments": cm}


def make_data():
    prs = {4: make_pr(4), 5: make_pr(5, merged=True), 7: make_pr(7, state="closed", draft=True, rc=0, cm=0, commits=1, files=1)}
    prs[7]["head"]["repo"] = None                                           # fork apagado
    d = {"prs": prs, "commits": {}, "files": {}, "reviews": {}, "review_comments": {}, "comments": {}, "events": {}, "timeline": {},
         "threads": {}, "closing": {}}
    for n, pr in prs.items():
        d["commits"][n] = [{"sha": f"{n}{i:039x}", "commit": {"message": "MENSAGEM_SEGREDA"}} for i in range(pr["commits"])]
        d["files"][n] = [{"filename": f"ARQUIVO_SEGREDO_{i}.py", "patch": "DIFF_SEGREDO"} for i in range(pr["changed_files"])]
        d["reviews"][n] = [{"id": 900 + n, "state": "APPROVED", "user": {"login": "USUARIO_SEGREDO"}}]
        d["review_comments"][n] = [{"id": 1000 * n + i, "body": "CORPO_SEGREDO"} for i in range(pr["review_comments"])]
        d["comments"][n] = [{"id": 2000 * n + i, "body": "CORPO_SEGREDO"} for i in range(pr["comments"])]
        d["events"][n] = [{"id": 3000 * n, "event": "labeled"}]
        d["timeline"][n] = []
        d["threads"][n] = []
        d["closing"][n] = []
    # PR 4: 2 comentários de review numa só thread (a thread tem 3 comentários numa API de 2 por página? aqui, 2 em 1 thread).
    d["threads"][4] = [{"id": "T4", "isResolved": True, "isOutdated": False, "isCollapsed": False, "path": "ARQUIVO_SEGREDO_0.py",
                        "comments": [{"databaseId": 4000 + i, "body": "CORPO_SEGREDO"} for i in range(2)]}]
    d["threads"][5] = [{"id": "T5", "isResolved": False, "isOutdated": True, "isCollapsed": False, "path": "ARQUIVO_SEGREDO_0.py",
                        "comments": [{"databaseId": 5000 + i, "body": "CORPO_SEGREDO"} for i in range(2)]}]
    d["closing"][4] = [{"number": 2, "repository": {"nameWithOwner": REPO}}]
    d["timeline"][4] = [{"event": "cross-referenced", "created_at": "2026-09-21T10:00:00Z",
                         "source": {"issue": {"number": 9, "repository": {"full_name": REPO}, "pull_request": {"url": "x"}}}},
                        {"event": "connected", "id": 77, "created_at": "2026-09-21T10:01:00Z"}, {"event": "commented", "id": 1}]
    return d


def write_inputs(root, issues_status="OK", issues_prs=(4, 5, 7), labels=True, refs=True):
    ev, pk = root / "evidence", root / "package"
    ev.mkdir(exist_ok=True)
    pk.mkdir(exist_ok=True)
    (ev / "api-status.json").write_text(json.dumps({"classes": {"issues": {"status": issues_status, "extra": {"pull_requests_skipped": list(issues_prs)}}}}))
    if labels:
        (pk / "api-labels.json").write_text(json.dumps([{"name": "bug"}]))
    if refs:
        (ev / "refs.tsv").write_text("\n".join([f"refs/pull/{n}/head\t{n:040x}\tcommit" for n in (4, 5)] + ["refs/heads/main\t" + "a" * 40 + "\tcommit"]) + "\n")


class Fake(http.server.BaseHTTPRequestHandler):
    data = {}
    fail = {}
    flaky = {}
    ratelimit = {}
    graphql_mode = "ok"        # ok | error | forbidden
    thread_page = 50           # comentários por página de thread na GraphQL
    extra_listed = ()          # PRs a mais na lista /pulls
    detail_override = {}       # n -> dict que sobrescreve campos do detalhe
    methods = []
    gql_queries = []

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

    def _paged(self, items, q):
        per, page = int(q.get("per_page", ["100"])[0]), int(q.get("page", ["1"])[0])
        headers = {}
        if page * per < len(items):
            nq = {k: v[0] for k, v in q.items()}
            nq["page"] = page + 1
            headers["Link"] = f'<http://{self.headers["Host"]}{urllib.parse.urlparse(self.path).path}?{urllib.parse.urlencode(nq)}>; rel="next"'
        self._send(200, items[(page - 1) * per: page * per], headers)

    def _gate(self, route):
        cls = Fake
        if route in cls.fail:
            self._send(cls.fail[route], {"message": "x"})
            return True
        if cls.flaky.get(route, 0) > 0:
            cls.flaky[route] -= 1
            self._send(500, {"message": "boom"})
            return True
        if cls.ratelimit.get(route, 0) > 0:
            cls.ratelimit[route] -= 1
            self._send(403, {"message": "rate"}, {"Retry-After": "1", "X-RateLimit-Remaining": "0"})
            return True
        return False

    def do_GET(self):  # noqa: N802
        cls = Fake
        cls.methods.append("GET")
        parsed = urllib.parse.urlparse(self.path)
        route, q = parsed.path, urllib.parse.parse_qs(parsed.query)
        if self._gate(route):
            return
        d = cls.data
        base = f"/repos/{REPO}"
        if route == f"{base}/pulls":
            return self._paged([{"number": n} for n in sorted(list(d["prs"]) + list(cls.extra_listed))], q)
        parts = route[len(base) + 1:].split("/") if route.startswith(base + "/") else []
        if parts[:1] == ["pulls"] and len(parts) >= 2 and parts[1].isdigit():
            n = int(parts[1])
            if n not in d["prs"]:
                return self._send(404, {"message": "nf"})
            if len(parts) == 2:
                return self._send(200, dict(d["prs"][n], **cls.detail_override.get(n, {})))
            kind = {"commits": "commits", "files": "files", "reviews": "reviews", "comments": "review_comments"}.get(parts[2])
            if kind:
                return self._paged(d[kind][n], q)
        if parts[:1] == ["issues"] and len(parts) == 3 and parts[1].isdigit() and parts[2] in ("comments", "events", "timeline"):
            return self._paged(d[parts[2]][int(parts[1])], q)
        return self._send(404, {"message": "nf"})

    def do_POST(self):  # noqa: N802
        cls = Fake
        cls.methods.append("POST")
        route = urllib.parse.urlparse(self.path).path
        if route != "/graphql":
            return self._send(404, {"message": "nf"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        query, v = body["query"], body["variables"]
        cls.gql_queries.append(query.split("(")[0])
        if cls.graphql_mode == "forbidden":
            return self._send(403, {"message": "denied"})
        if cls.graphql_mode == "error":
            return self._send(200, {"errors": [{"message": "TITULO_SEGREDO internal"}]})
        d = cls.data
        if "PullThreads" in query:
            n = v["number"]
            threads = d["threads"].get(n, [])
            nodes = []
            for t in threads:
                first = t["comments"][:cls.thread_page]
                nodes.append({k: val for k, val in t.items() if k != "comments"} | {"comments": {
                    "totalCount": len(t["comments"]), "nodes": first,
                    "pageInfo": {"hasNextPage": len(t["comments"]) > cls.thread_page, "endCursor": str(cls.thread_page)}}})
            return self._send(200, {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "totalCount": len(nodes), "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        if "ThreadComments" in query:
            thread = next(t for ts in d["threads"].values() for t in ts if t["id"] == v["id"])
            start = int(v["after"])
            chunk = thread["comments"][start:start + cls.thread_page]
            more = start + cls.thread_page < len(thread["comments"])
            return self._send(200, {"data": {"node": {"comments": {"totalCount": len(thread["comments"]), "nodes": chunk,
                                                                   "pageInfo": {"hasNextPage": more, "endCursor": str(start + cls.thread_page)}}}}})
        if "PullClosing" in query:
            refs = d["closing"].get(v["number"], [])
            return self._send(200, {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
                "totalCount": len(refs), "nodes": refs, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        return self._send(400, {"message": "unknown"})


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
        Fake.fail, Fake.flaky, Fake.ratelimit, Fake.detail_override = {}, {}, {}, {}
        Fake.graphql_mode, Fake.thread_page, Fake.extra_listed = "ok", 50, ()
        Fake.methods, Fake.gql_queries = [], []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        write_inputs(self.root)
        for patch in (unittest.mock.patch.object(ar, "sleep", lambda s: None), unittest.mock.patch.object(g, "sleep", lambda s: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_read(self, **env):
        environ = {"SOURCE_REPOSITORY": REPO, "SOURCE_TOKEN": TOKEN, "EVIDENCE_DIR": str(self.root / "evidence"),
                   "PACKAGE_DIR": str(self.root / "package"), "GITHUB_API_URL": self.base}
        environ.update(env)
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, environ), contextlib.redirect_stdout(out):
            p.cmd_read()
        inv = json.loads((self.root / "evidence" / "api-pulls-status.json").read_text())
        return out.getvalue(), inv["classes"]["pull_requests"], inv

    def clean(self, text):
        self.assertNotIn(TOKEN, text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def package_names(self):
        return sorted(x.name for x in (self.root / "package").iterdir())

    # ---- leitura completa ----
    def test_leitura_completa_e_conferida(self):
        with unittest.mock.patch.object(p, "PER_PAGE", 1):
            out, st, inv = self.run_read()
        self.assertEqual(st["status"], "OK", st)
        self.assertTrue(all(c["ok"] for c in st["checks"]), st["checks"])
        e = st["extra"]
        self.assertEqual(e["pulls"], {"listed": 3, "read": 3})
        self.assertEqual((e["commits"], e["files"], e["reviews"], e["review_comments"], e["comments"], e["events"], e["threads"]), (5, 5, 3, 4, 2, 3, 2))
        self.assertEqual(e["state_counts"], {"AVAILABLE": 3})
        for name in ("api-pulls.json", "api-pull-commits.json", "api-pull-files.json", "api-pull-reviews.json", "api-pull-review-comments.json",
                     "api-pull-comments.json", "api-pull-events.json", "api-pull-threads.json", "api-pull-relations.json", "api-pulls-inventory.json"):
            self.assertIn(name, self.package_names())
        self.assertEqual([x["number"] for x in json.loads((self.root / "package" / "api-pulls.json").read_text())], [4, 5, 7])
        self.clean(out)

    def test_log_publico_so_contagens_e_tamanhos(self):
        out, st, inv = self.run_read()
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("API_READ pull_requests: OK prs=3/3 commits=5 files=5 reviews=3 review_comments=4 comments=2 events=3 threads=2 bytes="), lines[0])
        self.clean(out)
        self.assertIn("TITULO_SEGREDO", (self.root / "package" / "api-pulls.json").read_text())      # os dados ficam no pacote

    def test_so_get_na_rest_e_so_consultas_na_graphql(self):
        out, st, inv = self.run_read()
        self.assertEqual(set(Fake.methods), {"GET", "POST"})
        self.assertTrue(Fake.gql_queries and all(q.startswith("query") for q in Fake.gql_queries), Fake.gql_queries)
        for bad in ("mutation { x }", "query A { x } mutation B { y }", "subscription { x }", "  {x}"):
            with self.assertRaises(ValueError):
                p.assert_query_only(bad)
        p.assert_query_only("query A { x }")
        for q in (p.THREADS_QUERY, p.THREAD_COMMENTS_QUERY, p.CLOSING_QUERY):
            p.assert_query_only(q)

    def test_relacoes_base_head_fechamento_cruzadas_e_conexoes(self):
        out, st, inv = self.run_read()
        rel = {r["number"]: r for r in json.loads((self.root / "package" / "api-pull-relations.json").read_text())}
        self.assertEqual(rel[4]["closing_issues"], [{"number": 2, "repository": REPO}])
        self.assertEqual(rel[4]["cross_references"], [{"number": 9, "repository": REPO, "is_pull_request": True, "created_at": "2026-09-21T10:00:00Z"}])
        self.assertEqual(rel[4]["connections"], [{"event": "connected", "id": 77, "created_at": "2026-09-21T10:01:00Z"}])
        self.assertEqual((rel[4]["base"]["ref"], rel[4]["head"]["repository"]), ("main", "fork/teste"))
        self.assertIsNone(rel[7]["head"]["repository"])                                      # fork apagado: head.repo nulo
        self.assertEqual(rel[7]["closing_issues"], [])

    def test_comentarios_da_thread_paginados(self):
        Fake.data["threads"][4][0]["comments"] = [{"databaseId": 4000 + i, "body": "CORPO_SEGREDO"} for i in range(5)]
        Fake.data["prs"][4]["review_comments"] = 5
        Fake.data["review_comments"][4] = [{"id": 4000 + i, "body": "CORPO_SEGREDO"} for i in range(5)]
        Fake.thread_page = 2
        out, st, inv = self.run_read()
        thread = json.loads((self.root / "package" / "api-pull-threads.json").read_text())[0]["threads"][0]
        self.assertEqual(len(thread["comments"]), 5)
        self.assertEqual(st["status"], "OK", st)
        self.assertIn("ThreadComments", " ".join(Fake.gql_queries))

    # ---- lista de PRs contra o leitor de issues ----
    def test_lista_de_prs_diferente_da_de_issues_reprova_se_issues_ok(self):
        write_inputs(self.root, issues_prs=(4, 5, 7, 99))
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("pulls_list_equals_issues_pull_requests", st["detail"])
        self.assertEqual(st["extra"]["issues_crosscheck"]["only_in_issues"], [99])

    def test_lista_diferente_e_so_informativa_se_issues_nao_esta_ok(self):
        write_inputs(self.root, issues_status="FAILED", issues_prs=(4, 5, 7, 99))
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "OK", st)
        self.assertEqual(st["extra"]["issues_crosscheck"]["mode"], "informative")
        self.assertEqual(st["extra"]["issues_crosscheck"]["only_in_issues"], [99])
        (self.root / "evidence" / "api-status.json").unlink()
        out, st, inv = self.run_read()
        self.assertEqual(st["extra"]["issues_crosscheck"]["mode"], "informative")

    # ---- limites declarados: PARTIAL, nunca FAILED ----
    def test_totais_de_threads_diferentes_da_rest_viram_limited_com_as_contagens(self):
        Fake.data["threads"][4][0]["comments"] = Fake.data["threads"][4][0]["comments"][:1]
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "PARTIAL", st)
        self.assertEqual(st["extra"]["thread_total_differences"], [{"graphql": 1, "rest": 2}])
        self.assertEqual(next(i for i in inv["items"] if i["number"] == 4)["limits"], ["THREAD_TOTALS_DIFFER_FROM_REST"])
        self.assertTrue(all(c["ok"] for c in st["checks"]))

    def test_graphql_indisponivel_limita_os_prs_sem_derrubar(self):
        for mode in ("error", "forbidden"):
            Fake.graphql_mode = mode
            out, st, inv = self.run_read()
            self.assertEqual(st["status"], "PARTIAL", (mode, st))
            self.assertIn("THREADS_UNAVAILABLE", st["detail"])
            self.assertEqual(st["extra"]["pulls"]["read"], 3)
            threads = json.loads((self.root / "package" / "api-pull-threads.json").read_text())
            self.assertTrue(all(t["available"] is False for t in threads))
            self.clean(out + json.dumps(st))

    def test_acima_dos_limites_da_api_de_commits_e_arquivos(self):
        Fake.detail_override = {4: {"commits": 300}, 5: {"changed_files": 3500}}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "PARTIAL", st)
        self.assertEqual(st["extra"]["limit_counts"], {"COMMITS_ABOVE_API_LIMIT": 1, "FILES_ABOVE_API_LIMIT": 1})
        self.assertTrue(all(c["ok"] for c in st["checks"]))              # sem conferência de contagem acima do limite da API

    def test_arquivo_sem_patch_e_limitacao_registrada_nao_falha(self):
        del Fake.data["files"][4][0]["patch"]
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "OK", st)
        self.assertEqual(st["extra"]["files_without_patch"], 1)
        self.assertTrue(any("without a patch" in l for l in inv["limitations"]))

    # ---- conferências que reprovam ----
    def test_contadores_divergentes_viram_failed(self):
        cases = {"commits": ("commits_read_equal_commits", {"commits": 9}), "files": ("files_read_equal_changed_files", {"changed_files": 9}),
                 "review_comments": ("review_comments_read_equal_count", {"review_comments": 9}),
                 "comments": ("conversation_comments_read_equal_count", {"comments": 9})}
        for label, (check, override) in cases.items():
            Fake.detail_override = {4: override}
            out, st, inv = self.run_read()
            self.assertEqual(st["status"], "FAILED", label)
            self.assertIn(check, st["detail"])

    def test_merged_incoerente_e_rotulo_desconhecido_viram_failed(self):
        Fake.detail_override = {5: {"merged_at": None}}
        out, st, inv = self.run_read()
        self.assertIn("merged_consistent_with_merged_at", st["detail"])
        Fake.detail_override = {4: {"labels": [{"name": "inexistente"}]}}
        out, st, inv = self.run_read()
        self.assertIn("labels_exist_in_labels_class", st["detail"])

    def test_ids_repetidos_viram_failed(self):
        Fake.data["reviews"][4] = [{"id": 1}, {"id": 1}]
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("reviews_ids_unique", st["detail"])

    # ---- falhas, tetos, repetição ----
    def test_403_vira_failed_com_a_dica_de_permissao(self):
        Fake.fail = {f"/repos/{REPO}/pulls/4/files": 403}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("Pull requests: Read-only", st["detail"])
        self.assertIn("/pulls/{id}/files", st["detail"])
        self.assertEqual(self.package_names(), ["api-labels.json", "api-pulls-inventory.json"])
        self.clean(out + json.dumps(st))

    def test_pr_com_erro_persistente_vira_item_failed(self):
        Fake.flaky = {f"/repos/{REPO}/pulls/5/reviews": 99}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertEqual(next(i for i in inv["items"] if i["number"] == 5), {"number": 5, "state": "FAILED", "status": 500})

    def test_tetos_de_prs_e_de_bytes(self):
        out, st, inv = self.run_read(PULLS_MAX="1")
        self.assertEqual({i["number"]: i["state"] for i in inv["items"]}, {4: "AVAILABLE", 5: "SKIPPED_OVER_CAP", 7: "SKIPPED_OVER_CAP"})
        self.assertEqual(st["status"], "PARTIAL")
        out, st, inv = self.run_read(PULLS_MAX_BYTES="1")
        self.assertEqual(sum(1 for i in inv["items"] if i["state"] == "SKIPPED_OVER_CAP"), 2)

    def test_repete_em_500_e_em_limite_de_taxa_curto(self):
        Fake.flaky = {f"/repos/{REPO}/pulls": 2}
        Fake.ratelimit = {f"/repos/{REPO}/pulls/4/commits": 1}
        out, st, inv = self.run_read()
        self.assertEqual(st["status"], "OK")

    def test_cruzamento_informativo_com_as_refs_do_git(self):
        Fake.detail_override = {5: {"head": dict(Fake.data["prs"][5]["head"], sha="1" * 40)}}
        out, st, inv = self.run_read()
        self.assertEqual(st["extra"]["refs_crosscheck"], {"available": True, "compared": 2, "equal": 1, "differ": 1})
        self.assertEqual(st["status"], "OK")                              # informativo: não reprova

    def test_excecao_inesperada_variaveis_e_mascara(self):
        with unittest.mock.patch.object(p, "read_pulls", side_effect=RuntimeError("TITULO_SEGREDO")):
            out, st, inv = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("RuntimeError", st["detail"])
        self.clean(out + json.dumps(st))
        env = {k: v for k, v in os.environ.items() if k not in ("SOURCE_TOKEN", "SOURCE_REPOSITORY")}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "github_pulls_read.py"), "read"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 64)
        with unittest.mock.patch.dict(os.environ, {"SOURCE_REPOSITORY": REPO, "SOURCE_TOKEN": TOKEN, "PULLS_MAX": "muitos"}):
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
                p.cmd_read()
        self.assertEqual(ctx.exception.code, 64)
        self.assertEqual(p.mask(f"/repos/{REPO}/pulls/12/files"), f"/repos/{REPO}/pulls/{{id}}/files")


class EvidenceTest(unittest.TestCase):
    """build-evidence ponta a ponta (backup_repository) com pull_requests lida pela API simulada."""

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
        Fake.fail, Fake.flaky, Fake.ratelimit, Fake.detail_override = {}, {}, {}, {}
        Fake.graphql_mode, Fake.thread_page, Fake.extra_listed = "ok", 50, ()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        write_inputs(self.root)
        (self.root / "evidence" / "refs.tsv").write_text("refs/heads/main\t" + "a" * 40 + "\tcommit\n")
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
        self.assertNotIn("pull_requests", classes)                             # pull_requests deixou de ser restrição
        self.assertEqual(len(classes), 11)                                     # 10 classes + projects (sem PROJECTS_TOKEN_OUTCOME)
        d = json.dumps({"previous_request_id": "req-prev-0001", "hitl_decision": "CONTINUE_WITH_RESTRICTIONS",
                        "accepted_restrictions": res["required_restrictions"]})
        self.assertEqual(self.run_script("bkp_repo.py", "preflight", TOKEN_OUTCOME="success", PREFLIGHT_DECISION=d).returncode, 0)
        return d

    def finish(self, d):
        for name, cls in (("api-actions-status.json", "actions"), ("api-checks-status.json", "checks")):
            (self.root / "evidence" / name).write_text(json.dumps({"classes": {cls: {
                "status": "OK", "count": 0, "files": [], "checks": [], "extra": {"temporal_counts": {"AVAILABLE": 1}}}}}))
        status = json.loads((self.root / "evidence" / "api-status.json").read_text())
        status["classes"].update({k: {"status": "OK", "count": 0, "files": [], "checks": []} for k in ("labels", "milestones")})
        (self.root / "evidence" / "api-status.json").write_text(json.dumps(status))
        (self.root / "evidence" / "onedrive-package.json").write_text(json.dumps(
            [{"remote_path": f"backups/x/req-test-0001/package/{x.name}", "sha256": "0" * 64, "bytes": 1} for x in sorted((self.root / "package").iterdir())]))
        proc = self.run_script("bkp_repo.py", "build-evidence", PREFLIGHT_DECISION=d)
        return proc, json.loads((self.root / "evidence" / "evidence.json").read_text())

    def test_evidencia_so_registro_sem_campos_temporais(self):
        d = self.decision()
        read = self.run_script("github_pulls_read.py", "read", SOURCE_TOKEN=TOKEN, GITHUB_API_URL=self.base, SOURCE_REPOSITORY=REPO)
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        proc, ev = self.finish(d)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        obj = next(o for o in ev["objects"] if o["object_class"] == "pull_requests")
        self.assertEqual(obj["disposition"], "PRESERVED-AS-EQUIVALENT-REPRESENTATION")
        self.assertIn("record only", obj["limitation"])
        self.assertIn("api-pulls-status.json", obj["evidence"])
        self.assertIn("api-pull-threads.json", obj["evidence"])
        self.assertNotIn("temporal_state", obj)
        self.assertNotIn("alerts", obj)
        self.assertNotIn(TOKEN, json.dumps(ev))

    def test_pr_limitado_deixa_a_classe_partially_preserved(self):
        Fake.graphql_mode = "error"
        d = self.decision()
        self.run_script("github_pulls_read.py", "read", SOURCE_TOKEN=TOKEN, GITHUB_API_URL=self.base, SOURCE_REPOSITORY=REPO)
        proc, ev = self.finish(d)
        obj = next(o for o in ev["objects"] if o["object_class"] == "pull_requests")
        self.assertEqual(obj["disposition"], "PARTIALLY-PRESERVED")
        self.assertIn("THREADS_UNAVAILABLE", obj["limitation"])

    def test_sem_a_leitura_a_classe_fica_failed(self):
        d = self.decision()
        proc, ev = self.finish(d)
        obj = next(o for o in ev["objects"] if o["object_class"] == "pull_requests")
        self.assertEqual(obj["disposition"], "FAILED")
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
