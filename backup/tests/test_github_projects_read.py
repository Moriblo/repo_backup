#!/usr/bin/env python3
"""Teste local do leitor github_projects_read.py, contra uma API GraphQL SIMULADA.

O QUE PROVA
    - leitura completa e paginada de um Project (o simulado espelha o quadro de teste nº 13:
      6 issues, 1 rascunho, campos Status e Prioridade) nos três escopos;
    - a descoberta por repositório, e a alternativa quando o token não enxerga o repositório
      (PARTIAL, nunca "sem Projects" como prova); um repositório ligado e OCULTO chega como `null`
      (o que o GitHub devolveu no run real req-20261003-001) e o Project vira candidato;
    - uma exceção inesperada (resposta fora do formato) vira FAILED, nunca derruba o job;
    - conferências: total de itens, conexões cortadas, opções de seleção; itens ocultos = PARTIAL;
    - falhas (HTTP 403, erro GraphQL de escopo, Project inexistente) viram FAILED com causa;
    - repetição em erro 500 e em limite de taxa;
    - só consultas (`query`): uma `mutation` é recusada sem sair do processo; token fora dos arquivos.
    NÃO prova nada contra o GitHub real: os nomes de campo do schema GraphQL foram escritos de
    memória e só o run real confirma.

COMO RODAR
    python3 backup/tests/test_github_projects_read.py        (saída 0 = tudo certo)
"""
import contextlib
import copy
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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import github_projects_read as g  # noqa: E402

TOKEN = "ghp_SEGREDO_PROJETOS_456"
SOURCE = "dono/teste"


def make_project(number=13, title="Quadro de teste (backup_teste_issues)", repos=(SOURCE,)):
    """Um Project como o GraphQL o devolve, com 7 itens (6 issues e 1 rascunho)."""
    status = {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT", "__typename": "ProjectV2SingleSelectField",
              "options": [{"id": f"S{i}", "name": n, "color": "GRAY", "description": ""} for i, n in enumerate(("Todo", "In Progress", "Done"))]}
    prio = {"id": "F_prio", "name": "Prioridade", "dataType": "SINGLE_SELECT", "__typename": "ProjectV2SingleSelectField",
            "options": [{"id": f"P{i}", "name": n, "color": "BLUE", "description": ""} for i, n in enumerate(("Alta", "Média", "Baixa"))]}
    title_field = {"id": "F_title", "name": "Title", "dataType": "TITLE", "__typename": "ProjectV2Field"}
    page = lambda nodes: {"totalCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}

    def value(field, opt_id, name):
        return {"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": name, "optionId": opt_id, "color": "GRAY",
                "description": "", "field": {"id": field["id"], "name": field["name"]}}

    items = []
    plan = [("Erro ao salvar a configuração", "S1", "In Progress", "P0", "Alta"), ("Melhorar a documentação inicial", "S0", "Todo", "P2", "Baixa"),
            ("Atender chamado urgente", "S1", "In Progress", "P0", "Alta"), ("Ideia: exportar relatório", "S0", "Todo", "P1", "Média"),
            ("Revisar a política de senhas", "S0", "Todo", "P1", "Média"), ("Tarefa já concluída", "S2", "Done", "P2", "Baixa")]
    for n, (t, s_id, s_name, p_id, p_name) in enumerate(plan, 1):
        items.append({"id": f"PVTI_{n}", "type": "ISSUE", "isArchived": False, "createdAt": "2026-10-02T19:26:00Z", "updatedAt": "2026-10-02T19:26:10Z",
                      "creator": {"login": "Moriblo"},
                      "content": {"__typename": "Issue", "id": f"I_{n}", "number": n, "title": t, "url": f"https://github.com/{SOURCE}/issues/{n}",
                                  "state": "CLOSED" if n == 6 else "OPEN", "repository": {"nameWithOwner": SOURCE}},
                      "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": [value(status, s_id, s_name), value(prio, p_id, p_name)]}})
    items.append({"id": "PVTI_7", "type": "DRAFT_ISSUE", "isArchived": False, "createdAt": "2026-10-02T19:26:20Z", "updatedAt": "2026-10-02T19:26:20Z",
                  "creator": {"login": "Moriblo"},
                  "content": {"__typename": "DraftIssue", "id": "DI_7", "title": "Rascunho sem issue", "body": "Item de rascunho do quadro de teste.",
                              "createdAt": "2026-10-02T19:26:20Z", "updatedAt": "2026-10-02T19:26:20Z", "creator": {"login": "Moriblo"},
                              "assignees": {"nodes": []}},
                  "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": [value(status, "S0", "Todo")]}})
    details = {"id": f"PVT_{number}", "number": number, "title": title, "shortDescription": None, "readme": None, "public": False, "closed": False,
               "closedAt": None, "createdAt": "2026-10-02T19:26:15Z", "updatedAt": "2026-10-02T19:26:30Z", "url": f"https://github.com/users/dono/projects/{number}",
               "owner": {"__typename": "User", "login": "dono"}, "creator": {"login": "Moriblo"},
               "repositories": page([{"nameWithOwner": r} if r else None for r in repos]),
               "fields": page([title_field, status, prio]),
               "views": page([{"id": "V1", "name": "View 1", "number": 1, "layout": "TABLE_LAYOUT", "filter": None, "createdAt": "x", "updatedAt": "x"}]),
               "workflows": page([{"id": f"W{i}", "name": f"wf{i}", "number": i, "enabled": True, "createdAt": "x", "updatedAt": "x"} for i in range(1, 7)]),
               "statusUpdates": page([])}
    return {"details": details, "items": items, "total": len(items)}


class FakeGraph(http.server.BaseHTTPRequestHandler):
    """Servidor GraphQL simulado. Atributos de classe controlam os cenários."""
    projects = []          # lista de make_project()
    repo_visible = True    # False: repository(...) devolve null (token sem `repo`)
    owner_exists = True
    http_fail = {}         # operationName -> código HTTP fixo
    gql_errors = {}        # operationName -> lista `errors`
    flaky = {}             # operationName -> quantas respostas 500 antes de responder certo
    ratelimit = {}         # operationName -> quantas respostas 403 com Retry-After
    seen = []              # (operationName, método, Authorization)

    def log_message(self, *args):
        pass

    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @staticmethod
    def _page(nodes, variables, size):
        """Paginação por cursor numérico: devolve a conexão {totalCount, pageInfo, nodes}."""
        start = int(variables.get("after") or 0)
        chunk = nodes[start:start + size]
        more = start + size < len(nodes)
        return {"totalCount": len(nodes), "pageInfo": {"hasNextPage": more, "endCursor": str(start + size) if more else None}, "nodes": chunk}

    def do_POST(self):  # noqa: N802
        cls = FakeGraph
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        op, variables = body.get("operationName"), body.get("variables") or {}
        cls.seen.append((op, self.command, self.headers.get("Authorization"), body["query"].lstrip()[:5]))
        if op in cls.http_fail:
            return self._send(cls.http_fail[op], {"message": "x"})
        if cls.flaky.get(op, 0) > 0:
            cls.flaky[op] -= 1
            return self._send(500, {"message": "boom"})
        if cls.ratelimit.get(op, 0) > 0:
            cls.ratelimit[op] -= 1
            return self._send(403, {"message": "rate"}, {"Retry-After": "1", "X-RateLimit-Remaining": "0"})
        if op in cls.gql_errors:
            return self._send(200, {"data": None, "errors": cls.gql_errors[op]})
        size = g.PAGE
        refs = lambda p: {"id": p["details"]["id"], "number": p["details"]["number"], "title": p["details"]["title"]}
        if op == "ProjectsOfRepository":
            if not cls.repo_visible:
                return self._send(200, {"data": {"repository": None}, "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]})
            mine = [p for p in cls.projects if SOURCE in [r["nameWithOwner"] for r in p["details"]["repositories"]["nodes"] if r]]
            return self._send(200, {"data": {"repository": {"nameWithOwner": SOURCE, "projectsV2": self._page([refs(p) for p in mine], variables, size)}}})
        if op == "ProjectsOfOwner":
            if not cls.owner_exists:
                return self._send(200, {"data": {"repositoryOwner": None}})
            nodes = [dict(refs(p), repositories={"pageInfo": {"hasNextPage": False}, "nodes": p["details"]["repositories"]["nodes"]}) for p in cls.projects]
            return self._send(200, {"data": {"repositoryOwner": {"__typename": "User", "login": "dono", "projectsV2": self._page(nodes, variables, size)}}})
        if op == "ProjectByNumber":
            hit = next((p for p in cls.projects if p["details"]["number"] == variables["number"]), None)
            return self._send(200, {"data": {"repositoryOwner": {"__typename": "User", "login": "dono", "projectV2": refs(hit) if hit else None}}})
        project = next((p for p in cls.projects if p["details"]["id"] == variables.get("id")), None)
        if op == "ProjectDetails":
            return self._send(200, {"data": {"node": project["details"]}})
        if op == "ProjectItems":
            conn = self._page(project["items"], variables, size)
            conn["totalCount"] = project["total"]
            return self._send(200, {"data": {"node": {"items": conn}}})
        self._send(400, {"message": "operação desconhecida"})


class ReadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGraph)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        g.sleep = lambda s: None  # não esperar de verdade

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        g.PAGE = 100
        FakeGraph.projects = [make_project()]
        FakeGraph.repo_visible, FakeGraph.owner_exists = True, True
        FakeGraph.http_fail, FakeGraph.gql_errors, FakeGraph.flaky, FakeGraph.ratelimit = {}, {}, {}, {}
        FakeGraph.seen = []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_read(self, scope="SOURCE_REPOSITORY", ids=None):
        ids = ids if ids is not None else {"source_repository": SOURCE}
        env = {"PROJECTS_TOKEN": TOKEN, "PROJECTS_SCOPE": scope, "PROJECTS_SCOPE_IDS": json.dumps(ids),
               "EVIDENCE_DIR": str(self.root / "evidence"), "PACKAGE_DIR": str(self.root / "package"),
               "GITHUB_API_URL": f"http://127.0.0.1:{self.server.server_address[1]}"}
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            g.cmd_read()
        self.out = out.getvalue()
        return json.loads((self.root / "evidence" / "api-projects-status.json").read_text())["classes"]["projects"]

    def load(self, name):
        return json.loads((self.root / "package" / name).read_text(encoding="utf-8"))

    def test_leitura_completa_por_repositorio(self):
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("OK", 1))
        project = self.load("api-projects.json")[0]
        self.assertEqual((project["number"], project["items_summary"]["total"]), (13, 7))
        self.assertEqual(project["items_summary"]["by_type"], {"DRAFT_ISSUE": 1, "ISSUE": 6})
        self.assertEqual([f["name"] for f in project["fields"]["nodes"]], ["Title", "Status", "Prioridade"])
        self.assertEqual(len(project["workflows"]["nodes"]), 6)
        items = self.load("api-project-items.json")
        self.assertEqual(len(items), 7)
        self.assertTrue(all(i["project_number"] == 13 for i in items))
        # Status e Prioridade preenchidos nas 6 issues; o rascunho só tem Status (Prioridade vazia).
        names = lambda i: [v["field"]["name"] for v in i["fieldValues"]["nodes"]]
        self.assertEqual([len(names(i)) for i in items], [2, 2, 2, 2, 2, 2, 1])
        self.assertEqual(items[6]["content"]["title"], "Rascunho sem issue")
        self.assertIn("Média", (self.root / "package" / "api-project-items.json").read_text(encoding="utf-8"))
        # Arquivos do pacote e do inventário coerentes.
        self.assertEqual([f["file"] for f in st["files"]], ["api-projects.json", "api-project-items.json"])
        self.assertTrue((self.root / "package" / "api-projects-inventory.json").exists())

    def test_paginacao(self):
        g.PAGE = 2
        st = self.run_read()
        self.assertEqual(st["status"], "OK")
        self.assertEqual(len(self.load("api-project-items.json")), 7)
        # 7 itens em páginas de 2 = 4 chamadas de ProjectItems.
        self.assertEqual(sum(1 for s in FakeGraph.seen if s[0] == "ProjectItems"), 4)

    def test_sem_projects_ligados(self):
        FakeGraph.projects = []
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("OK", 0))

    def test_repositorio_invisivel_usa_alternativa_e_fica_partial(self):
        FakeGraph.repo_visible = False
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("PARTIAL", 1))
        self.assertIn("not visible", st["detail"])
        self.assertEqual(len(self.load("api-project-items.json")), 7)

    def test_repositorio_invisivel_sem_vinculo_visivel_nao_prova_ausencia(self):
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(repos=())]
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("PARTIAL", 0))

    def test_vinculo_oculto_null_vira_candidato_partial(self):
        # Caso REAL (run req-20261003-001): o token não vê o repositório privado, o GitHub devolve
        # `null` no lugar dele em `repositories.nodes`, e o Project ligado a ele é só um candidato.
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(13, repos=(None,)),                 # vínculo oculto: candidato
                              make_project(14, "De outro repo", repos=("dono/outro",)),   # vínculo visível a outro: fora
                              make_project(15, "Sem vínculo", repos=())]       # sem vínculo: fora
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("PARTIAL", 1))
        self.assertEqual([p["number"] for p in self.load("api-projects.json")], [13])
        self.assertIn("candidates", st["detail"])
        self.assertIn("#13", st["detail"])

    def test_vinculo_visivel_e_oculto_no_mesmo_project(self):
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(13, repos=(SOURCE, None))]
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("PARTIAL", 1))

    def test_excecao_inesperada_vira_failed_sem_derrubar(self):
        # Resposta fora do formato (um campo que o schema real não tem): o leitor não pode estourar.
        del FakeGraph.projects[0]["details"]["fields"]
        st = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("Unexpected", st["detail"])
        self.assertTrue((self.root / "evidence" / "api-projects-status.json").exists())

    def test_itens_ocultos_viram_partial(self):
        FakeGraph.projects[0]["items"][0].update(type="REDACTED", content=None)
        FakeGraph.projects[0]["items"][1].update(content=None)  # ISSUE sem conteúdo visível
        st = self.run_read()
        self.assertEqual(st["status"], "PARTIAL")
        self.assertEqual(st["extra"]["redacted_items"], 2)

    def test_total_de_itens_divergente(self):
        FakeGraph.projects[0]["total"] = 8
        st = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("items_total", st["detail"])

    def test_conexao_cortada(self):
        FakeGraph.projects[0]["details"]["fields"]["pageInfo"]["hasNextPage"] = True
        st = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("connections_complete", st["detail"])

    def test_opcao_de_selecao_inexistente(self):
        FakeGraph.projects[0]["items"][0]["fieldValues"]["nodes"][0]["optionId"] = "NAO_EXISTE"
        st = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("select_options_exist", st["detail"])

    def test_escopo_project_e_owner_set(self):
        FakeGraph.projects = [make_project(13), make_project(14, "Outro", repos=())]
        one = self.run_read("PROJECT", {"owner": "dono", "number": 13})
        self.assertEqual((one["status"], one["count"]), ("OK", 1))
        every = self.run_read("OWNER_PROJECT_SET", {"owner": "dono"})
        self.assertEqual((every["status"], every["count"]), ("OK", 2))
        self.assertEqual({i["project_number"] for i in self.load("api-project-items.json")}, {13, 14})

    def test_project_inexistente_falha(self):
        st = self.run_read("PROJECT", {"owner": "dono", "number": 99})
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("não encontrado", st["detail"])

    def test_http_403_e_escopo_viram_failed_com_dica(self):
        FakeGraph.http_fail["ProjectsOfRepository"] = 403
        st = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("HTTP 403", st["detail"])
        self.assertIn("read:project", st["detail"])
        FakeGraph.http_fail = {}
        FakeGraph.gql_errors["ProjectDetails"] = [{"type": "INSUFFICIENT_SCOPES", "message": "needs read:project"}]
        st = self.run_read()
        self.assertEqual(st["status"], "FAILED")
        self.assertIn("INSUFFICIENT_SCOPES", st["detail"])
        self.assertFalse((self.root / "package" / "api-projects.json").exists())

    def test_repete_500_e_limite_de_taxa(self):
        FakeGraph.flaky["ProjectDetails"] = 2
        FakeGraph.ratelimit["ProjectItems"] = 1
        self.assertEqual(self.run_read()["status"], "OK")

    def test_500_persistente_falha(self):
        FakeGraph.flaky["ProjectDetails"] = 99
        self.assertEqual(self.run_read()["status"], "FAILED")

    def test_so_consultas_e_token_nao_vaza(self):
        self.run_read()
        self.assertEqual({s[1] for s in FakeGraph.seen}, {"POST"})
        self.assertEqual({s[3] for s in FakeGraph.seen}, {"query"})
        self.assertEqual({s[2] for s in FakeGraph.seen}, {f"Bearer {TOKEN}"})
        self.assertNotIn(TOKEN, self.out)
        for p in self.root.rglob("*.json"):
            self.assertNotIn(TOKEN, p.read_text(encoding="utf-8"))

    def test_mutation_e_recusada_sem_enviar(self):
        graph = g.Graph("http://127.0.0.1:1", TOKEN)  # porta fechada: se tentasse enviar, daria erro de rede
        with self.assertRaises(g.GraphqlError) as cm:
            graph.run_query("mutation M { deleteProjectV2(input: {projectId: \"x\"}) { clientMutationId } }")
        self.assertEqual(cm.exception.kind, "REFUSED")
        self.assertEqual(FakeGraph.seen, [])

    def test_entrada_invalida_sai_com_64(self):
        cases = [{}, {"PROJECTS_TOKEN": "t", "PROJECTS_SCOPE": "OUTRO", "PROJECTS_SCOPE_IDS": "{}"},
                 {"PROJECTS_TOKEN": "t", "PROJECTS_SCOPE": "PROJECT", "PROJECTS_SCOPE_IDS": '{"owner": "a"}'},
                 {"PROJECTS_TOKEN": "t", "PROJECTS_SCOPE": "SOURCE_REPOSITORY", "PROJECTS_SCOPE_IDS": '{"source_repository": "semBarra"}'},
                 {"PROJECTS_TOKEN": "t", "PROJECTS_SCOPE": "PROJECT", "PROJECTS_SCOPE_IDS": "não é json"}]
        for env in cases:
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
                    g.cmd_read()
            self.assertEqual(cm.exception.code, 64, env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
