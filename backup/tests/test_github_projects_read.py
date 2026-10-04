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


DEFAULT_TITLES = ["Erro ao salvar a configuração", "Melhorar a documentação inicial", "Atender chamado urgente",
                  "Ideia: exportar relatório", "Revisar a política de senhas", "Tarefa já concluída"]


def make_project(number=13, title="Quadro de teste (backup_teste_issues)", repos=(SOURCE,), issue_titles=None, hidden=False, drafts=1, archived=0):
    """Um Project como o GraphQL o devolve: 6 issues (ou `issue_titles`) e `drafts` rascunhos.

    hidden=True imita o token sem acesso ao repositório privado (run real req-20261003-002): o item de
    issue vem com `content: null`, mas o valor do campo Title e os de Status e Prioridade continuam lá."""
    status = {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT", "__typename": "ProjectV2SingleSelectField",
              "options": [{"id": f"S{i}", "name": n, "color": "GRAY", "description": ""} for i, n in enumerate(("Todo", "In Progress", "Done"))]}
    prio = {"id": "F_prio", "name": "Prioridade", "dataType": "SINGLE_SELECT", "__typename": "ProjectV2SingleSelectField",
            "options": [{"id": f"P{i}", "name": n, "color": "BLUE", "description": ""} for i, n in enumerate(("Alta", "Média", "Baixa"))]}
    title_field = {"id": "F_title", "name": "Title", "dataType": "TITLE", "__typename": "ProjectV2Field"}
    page = lambda nodes: {"totalCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}

    def text_value(text):
        return {"__typename": "ProjectV2ItemFieldTextValue", "text": text, "field": {"id": title_field["id"], "name": "Title"}}

    def value(field, opt_id, name):
        return {"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": name, "optionId": opt_id, "color": "GRAY",
                "description": "", "field": {"id": field["id"], "name": field["name"]}}

    items = []
    states = [("S1", "In Progress", "P0", "Alta"), ("S0", "Todo", "P2", "Baixa"), ("S1", "In Progress", "P0", "Alta"),
              ("S0", "Todo", "P1", "Média"), ("S0", "Todo", "P1", "Média"), ("S2", "Done", "P2", "Baixa")]
    titles = DEFAULT_TITLES if issue_titles is None else issue_titles
    plan = [(t,) + states[k % len(states)] for k, t in enumerate(titles)]
    for n, (t, s_id, s_name, p_id, p_name) in enumerate(plan, 1):
        items.append({"id": f"PVTI_{n}", "type": "ISSUE", "isArchived": False, "createdAt": "2026-10-02T19:26:00Z", "updatedAt": "2026-10-02T19:26:10Z",
                      "creator": {"login": "Moriblo"},
                      "content": None if hidden else {"__typename": "Issue", "id": f"I_{n}", "number": n, "title": t, "url": f"https://github.com/{SOURCE}/issues/{n}",
                                  "state": "CLOSED" if n == 6 else "OPEN", "repository": {"nameWithOwner": SOURCE}},
                      "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": [text_value(t), value(status, s_id, s_name), value(prio, p_id, p_name)]}})
    for d in range(drafts):
        k = len(plan) + d + 1
        items.append({"id": f"PVTI_{k}", "type": "DRAFT_ISSUE", "isArchived": False, "createdAt": "2026-10-02T19:26:20Z", "updatedAt": "2026-10-02T19:26:20Z",
                      "creator": {"login": "Moriblo"},
                      "content": {"__typename": "DraftIssue", "id": f"DI_{k}", "title": "Rascunho sem issue", "body": "Item de rascunho do quadro de teste.",
                                  "createdAt": "2026-10-02T19:26:20Z", "updatedAt": "2026-10-02T19:26:20Z", "creator": {"login": "Moriblo"},
                                  "assignees": {"nodes": []}},
                      "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": [text_value("Rascunho sem issue"), value(status, "S0", "Todo")]}})
    for a in range(archived):
        k = len(plan) + drafts + a + 1
        items.append({"id": f"PVTI_{k}", "type": "ISSUE", "isArchived": True, "createdAt": "2026-10-02T19:26:30Z", "updatedAt": "2026-10-02T19:26:40Z",
                      "creator": {"login": "Moriblo"}, "content": None,
                      "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": [text_value("Item arquivado"), value(status, "S2", "Done")]}})
    details = {"id": f"PVT_{number}", "number": number, "title": title, "shortDescription": None, "readme": None, "public": False, "closed": False,
               "closedAt": None, "createdAt": "2026-10-02T19:26:15Z", "updatedAt": "2026-10-02T19:26:30Z", "url": f"https://github.com/users/dono/projects/{number}",
               "owner": {"__typename": "User", "login": "dono"}, "creator": {"login": "Moriblo"},
               "repositories": page([{"nameWithOwner": r} if r else None for r in repos]),
               "fields": page([title_field, status, prio]),
               "views": page([{"id": "V1", "name": "View 1", "number": 1, "layout": "TABLE_LAYOUT", "filter": None, "createdAt": "x", "updatedAt": "x",
                               "configuration": {"visibleFields": page([{"id": "F_title", "name": "Title"}, {"id": "F_status", "name": "Status"}])},
                               "groupByFields": page([]), "verticalGroupByFields": page([]),
                               "sortByFields": page([{"direction": "ASC", "field": {"id": "F_prio", "name": "Prioridade"}}])},
                              {"id": "V2", "name": "View 2", "number": 2, "layout": "BOARD_LAYOUT", "filter": "status:Todo", "createdAt": "x", "updatedAt": "x",
                               "configuration": {"visibleFields": page([{"id": "F_title", "name": "Title"}])},
                               "groupByFields": page([{"id": "F_status", "name": "Status"}]), "verticalGroupByFields": page([]), "sortByFields": page([])}]),
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
    queries = {}              # operationName -> texto da última consulta enviada
    probe_requests = []       # nomes de tipo pedidos à sonda, na ordem
    probe_nodes = {}          # nome do tipo -> nó cru da introspecção (quando preenchido, tem prioridade sobre probe_types)
    probe_malformed = False   # a sonda recebe uma resposta fora do formato (o schema real pode diferir do esperado)
    probe_mutations = ["createProjectV2", "copyProjectV2", "addProjectV2ItemById", "deleteIssue"]
    probe_types = {"ProjectV2View": {"kind": "OBJECT", "fields": ["id", "name", "layout", "filter", "groupByFields"]},
                   "CreateProjectV2Input": {"kind": "INPUT_OBJECT", "inputFields": ["ownerId", "title"]}}

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
        cls.queries[op] = body["query"]
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
        if op == "ProbeMutations" and cls.probe_malformed:
            return self._send(200, {"data": {"__schema": None}})
        if op == "ProbeMutations":
            return self._send(200, {"data": {"__schema": {"mutationType": {"fields": [{"name": n} for n in cls.probe_mutations]}}}})
        if op == "ProbeType":
            cls.probe_requests.append(variables["name"])
        if op == "ProbeType" and variables["name"] in cls.probe_nodes:
            return self._send(200, {"data": {"__type": cls.probe_nodes[variables["name"]]}})
        if op == "ProbeType":
            spec = cls.probe_types.get(variables["name"])
            if spec is None:
                return self._send(200, {"data": {"__type": None}})
            ref = {"kind": "NON_NULL", "name": None, "ofType": {"kind": "SCALAR", "name": "String", "ofType": None}}
            lst = lambda names: [{"name": n, "type": ref} for n in names]
            return self._send(200, {"data": {"__type": {"name": variables["name"], "kind": spec["kind"], "enumValues": None,
                                                         "fields": lst(spec["fields"]) if "fields" in spec else None,
                                                         "inputFields": lst(spec["inputFields"]) if "inputFields" in spec else None}}})
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
        FakeGraph.probe_malformed = False
        FakeGraph.probe_nodes = {}
        FakeGraph.queries = {}
        FakeGraph.probe_requests = []
        FakeGraph.probe_mutations = ["createProjectV2", "copyProjectV2", "addProjectV2ItemById", "deleteIssue"]
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

    def seed_issues(self, titles):
        """Grava package/api-issues.json como o passo de labels/milestones/issues faz (só os títulos importam)."""
        (self.root / "package").mkdir(parents=True, exist_ok=True)
        (self.root / "package" / "api-issues.json").write_text(json.dumps([{"number": n, "title": t} for n, t in enumerate(titles, 1)]), encoding="utf-8")

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
        # Title, Status e Prioridade nas 6 issues; o rascunho só tem Title e Status (Prioridade vazia).
        names = lambda i: [v["field"]["name"] for v in i["fieldValues"]["nodes"]]
        self.assertEqual([len(names(i)) for i in items], [3, 3, 3, 3, 3, 3, 2])
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

    def test_vinculo_oculto_confirmado_pelos_titulos(self):
        # Caso REAL (run req-20261003-002): o token não vê o repositório privado, o GitHub devolve `null` no
        # lugar dele, e as issues do Project vêm com o conteúdo oculto. O vínculo é confirmado porque os
        # títulos dos itens batem com as issues da origem (lidas pela API REST no passo anterior).
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(13, repos=(None,), hidden=True)]
        self.seed_issues(DEFAULT_TITLES)
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("PARTIAL", 1))
        project = self.load("api-projects.json")[0]
        self.assertEqual(project["linkage"], {"method": "TITLE_MATCH", "matched": 6, "issue_items": 6})
        self.assertEqual(project["items_summary"]["redacted"], 6)
        self.assertEqual(st["extra"]["excluded_candidates"], [])
        self.assertIn("title match", st["detail"])

    def test_candidato_alheio_e_excluido_e_nao_entra_no_pacote(self):
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(13, repos=(None,), hidden=True),
                              make_project(12, "Minha_Caixinha_de_Saude", repos=(None,), hidden=True,
                                           issue_titles=["[MCS-PB-001] Use physical weekly pill organizer", "[MCS-PB-002] Keep backend invisible"], drafts=0),
                              make_project(5, "ESG Value Mining", repos=(None,), hidden=True, issue_titles=["Algo sem relação"], drafts=0)]
        self.seed_issues(DEFAULT_TITLES)
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("PARTIAL", 1))
        self.assertEqual([p["number"] for p in self.load("api-projects.json")], [13])
        self.assertEqual({i["project_number"] for i in self.load("api-project-items.json")}, {13})
        excluded = {e["number"]: e for e in st["extra"]["excluded_candidates"]}
        self.assertEqual(set(excluded), {12, 5})
        self.assertEqual((excluded[12]["matched"], excluded[12]["issue_items"], excluded[12]["reason"]), (0, 2, "TITLES_DO_NOT_MATCH"))
        self.assertEqual(excluded[12]["title"], "Minha_Caixinha_de_Saude")
        # Nada do conteúdo dos excluídos vaza para os arquivos do pacote.
        for name in ("api-projects.json", "api-project-items.json"):
            text = (self.root / "package" / name).read_text(encoding="utf-8")
            self.assertNotIn("MCS-PB", text)
            self.assertNotIn("ESG Value Mining", text)
        self.assertIn("#12", st["detail"])

    def test_sem_titulos_da_origem_nao_confirma_nenhum_candidato(self):
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(13, repos=(None,), hidden=True)]
        # (sem seed_issues: o passo das issues falhou ou a classe está desativada)
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("PARTIAL", 0))
        self.assertEqual(st["extra"]["excluded_candidates"][0]["reason"], "NO_SOURCE_TITLES")

    def test_candidato_so_com_rascunhos_nao_e_confirmado(self):
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(13, repos=(None,), hidden=True, issue_titles=[], drafts=3)]
        self.seed_issues(DEFAULT_TITLES)
        st = self.run_read()
        self.assertEqual(st["count"], 0)
        self.assertEqual(st["extra"]["excluded_candidates"][0]["reason"], "NO_ISSUE_ITEMS")

    def test_limiar_de_metade_dos_itens_de_issue(self):
        FakeGraph.repo_visible = False
        self.seed_issues(["a", "b", "c", "d"])
        FakeGraph.projects = [make_project(1, repos=(None,), hidden=True, issue_titles=["a", "x", "y", "z"], drafts=0)]   # 1 de 4: fora
        self.assertEqual(self.run_read()["count"], 0)
        FakeGraph.projects = [make_project(1, repos=(None,), hidden=True, issue_titles=["a", "b", "y", "z"], drafts=0)]   # 2 de 4: fica
        self.assertEqual(self.run_read()["count"], 1)

    def test_titulos_comparados_sem_diferenca_de_caixa_ou_espacos(self):
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(13, repos=(None,), hidden=True, issue_titles=["  Erro   ao SALVAR a configuração "], drafts=0)]
        self.seed_issues(["Erro ao salvar a configuração"])
        self.assertEqual(self.run_read()["count"], 1)

    def test_conteudo_visivel_da_origem_confirma_sem_precisar_de_titulos(self):
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(13, repos=(None,))]   # itens COM conteúdo, repositório = origem
        st = self.run_read()                                      # (sem seed_issues)
        self.assertEqual(st["count"], 1)
        self.assertEqual(self.load("api-projects.json")[0]["linkage"]["matched"], 6)

    def test_escopos_explicitos_nao_filtram_por_titulo(self):
        FakeGraph.projects = [make_project(13, repos=(None,), hidden=True)]
        st = self.run_read("PROJECT", {"owner": "dono", "number": 13})   # sem seed_issues
        self.assertEqual(st["count"], 1)
        self.assertEqual(self.load("api-projects.json")[0]["linkage"], {"method": "REQUESTED_SCOPE"})

    def test_titulos_lidos_de_outra_pasta_com_source_titles_dir(self):
        # No BKP_PROJ as issues ficam numa pasta de trabalho, fora do pacote (SOURCE_TITLES_DIR).
        FakeGraph.repo_visible = False
        FakeGraph.projects = [make_project(13, repos=(None,), hidden=True)]
        work = self.root / "titles-work"
        work.mkdir()
        (work / "api-issues.json").write_text(json.dumps([{"number": n, "title": t} for n, t in enumerate(DEFAULT_TITLES, 1)]), encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {"SOURCE_TITLES_DIR": str(work)}):
            st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("PARTIAL", 1))
        self.assertEqual(self.load("api-projects.json")[0]["linkage"]["method"], "TITLE_MATCH")
        self.assertFalse((self.root / "package" / "api-issues.json").exists())

    def test_vinculo_visivel_tem_linkage_visible_link(self):
        self.run_read()
        self.assertEqual(self.load("api-projects.json")[0]["linkage"], {"method": "VISIBLE_LINK"})

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
        FakeGraph.projects[0]["items"][0]["fieldValues"]["nodes"][1]["optionId"] = "NAO_EXISTE"
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

    def test_itens_arquivados_e_ordem_por_posicao_sao_pedidos(self):
        FakeGraph.projects = [make_project(archived=2)]
        st = self.run_read()
        query = FakeGraph.queries["ProjectItems"]
        self.assertIn("archivedStates: [ARCHIVED, NOT_ARCHIVED]", query)               # sem isto o GitHub só entrega os não arquivados
        self.assertIn("orderBy: {field: POSITION, direction: ASC}", query)
        items = self.load("api-project-items.json")
        self.assertEqual([i["isArchived"] for i in items], [False] * 7 + [True] * 2)  # 6 issues + 1 rascunho + 2 arquivados
        self.assertEqual([i["position_order"] for i in items], list(range(9)))        # a posição fica registrada no próprio item
        checks = {c["check"]: c for c in st["checks"]}
        self.assertEqual((checks["items_total"]["ok"], checks["items_total"]["read"]), (True, 9))
        self.assertEqual(checks["archived_items"]["archived"], 2)

    def test_views_completas_sao_pedidas_e_guardadas(self):
        self.run_read()
        query = FakeGraph.queries["ProjectDetails"]
        for piece in ("visibleFields(first: 100)", "groupByFields(first: 20)", "verticalGroupByFields(first: 20)", "sortByFields(first: 20)",
                      "template", "multiSelectOptions"):
            self.assertIn(piece, query)
        views = self.load("api-projects.json")[0]["views"]["nodes"]
        self.assertEqual([(v["name"], v["layout"], v["filter"]) for v in views], [("View 1", "TABLE_LAYOUT", None), ("View 2", "BOARD_LAYOUT", "status:Todo")])
        self.assertEqual([f["name"] for f in views[0]["configuration"]["visibleFields"]["nodes"]], ["Title", "Status"])
        self.assertEqual(views[0]["sortByFields"]["nodes"], [{"direction": "ASC", "field": {"id": "F_prio", "name": "Prioridade"}}])
        self.assertEqual([f["name"] for f in views[1]["groupByFields"]["nodes"]], ["Status"])

    def test_conexao_de_view_cortada_falha_a_conferencia(self):
        project = make_project()
        project["details"]["views"]["nodes"][1]["sortByFields"]["pageInfo"]["hasNextPage"] = True
        project["details"]["views"]["nodes"][0]["configuration"]["visibleFields"]["pageInfo"]["hasNextPage"] = True
        FakeGraph.projects = [project]
        st = self.run_read()
        check = next(c for c in st["checks"] if c["check"] == "connections_complete")
        self.assertFalse(check["ok"])
        self.assertEqual(sorted(check["truncated"]), ["views[1].visibleFields", "views[2].sortByFields"])
        self.assertEqual(st["status"], "FAILED")

    def test_so_consultas_de_verdade_passam_pelo_bloqueio(self):
        graph = g.Graph("http://127.0.0.1:1", TOKEN)
        for query in ("mutation { deleteProjectV2(input: {projectId: \"x\"}) { clientMutationId } }",
                      "mutation($i: ID!) { deleteProjectV2(input: {projectId: $i}) { clientMutationId } }",
                      "query Q { x }\nmutation\n  M { y }",
                      "subscription S { x }",
                      "query Q { x } subscription S { y }",         # segunda operação no mesmo documento
                      "{ viewer { login } }",                       # não começa por `query`: recusada
                      "  MUTATION M { x }"):
            with self.assertRaises(g.GraphqlError, msg=query) as cm:
                graph.run_query(query)
            self.assertEqual(cm.exception.kind, "REFUSED", query)
        self.assertEqual(FakeGraph.seen, [])
        # A introspecção cita "mutation" sem ser uma operação: o bloqueio não pode pegá-la (e a sonda usa estas consultas).
        for query in (g.Q_PROBE_MUTATIONS, g.Q_PROBE_TYPE):
            self.assertIsNone(g.MUTATION_OP.search(query), query)

    def test_sonda_do_schema_vai_no_inventario(self):
        self.run_read()
        probe = json.loads((self.root / "package" / "api-projects-inventory.json").read_text())["schema_probe"]
        self.assertEqual(probe["status"], "OK")
        self.assertEqual(probe["mutations_total"], 4)
        self.assertEqual(probe["project_v2_mutations"], ["addProjectV2ItemById", "copyProjectV2", "createProjectV2"])   # só as de Projects
        self.assertEqual(probe["types"]["ProjectV2View"]["fields"], {n: "String!" for n in ("id", "name", "layout", "filter", "groupByFields")})
        self.assertEqual(probe["types"]["CreateProjectV2Input"]["input_fields"], {"ownerId": "String!", "title": "String!"})
        self.assertIsNone(probe["types"]["ProjectV2Workflow"])           # tipo que a API não tem aparece como null, não some
        status = json.loads((self.root / "evidence" / "api-projects-status.json").read_text())
        self.assertEqual(status["schema_probe"]["status"], "OK")
        self.assertIn("API_PROBE projects: OK", self.out)
        # Só consultas, e o token não vai para os arquivos.
        self.assertEqual({s[3] for s in FakeGraph.seen}, {"query"})
        for p in self.root.rglob("*.json"):
            self.assertNotIn(TOKEN, p.read_text(encoding="utf-8"))

    @staticmethod
    def sref(name, kind="SCALAR"):
        return {"kind": kind, "name": name, "ofType": None}

    @classmethod
    def non_null(cls, ref):
        return {"kind": "NON_NULL", "name": None, "ofType": ref}

    def node(self, name, kind, **parts):
        base = {"name": name, "kind": kind, "fields": None, "inputFields": None, "enumValues": None, "possibleTypes": None}
        base.update(parts)
        return base

    def test_sonda_registra_argumentos_e_segue_os_tipos_de_projects(self):
        S, N = self.sref, self.non_null
        arg = lambda n, t: {"name": n, "type": t}
        FakeGraph.probe_nodes = {
            # ProjectV2.items tem argumentos; um deles é de um tipo de Projects (seguido) e outro não (não seguido).
            "ProjectV2": self.node("ProjectV2", "OBJECT", fields=[
                {"name": "items", "type": N(S("ProjectV2ItemConnection", "OBJECT")),
                 "args": [arg("first", S("Int")), arg("orderBy", S("ProjectV2ItemOrder", "INPUT_OBJECT")), arg("query", S("String"))]},
                {"name": "title", "type": N(S("String")), "args": []},
                {"name": "views", "type": N(S("ProjectV2ViewConnection", "OBJECT")), "args": [arg("orderBy", S("ProjectV2ViewOrder", "INPUT_OBJECT"))]}]),
            "ProjectV2ItemOrder": self.node("ProjectV2ItemOrder", "INPUT_OBJECT", inputFields=[
                {"name": "field", "type": N(S("ProjectV2ItemOrderField", "ENUM"))}, {"name": "direction", "type": N(S("OrderDirection", "ENUM"))}]),
            "ProjectV2ItemOrderField": self.node("ProjectV2ItemOrderField", "ENUM", enumValues=[{"name": "POSITION"}]),
            "OrderDirection": self.node("OrderDirection", "ENUM", enumValues=[{"name": "ASC"}]),
            # citado por dois tipos (ProjectV2ItemOrder e esta entrada): tem de ser pedido uma vez só
            "ProjectV2ViewOrder": self.node("ProjectV2ViewOrder", "INPUT_OBJECT", inputFields=[
                {"name": "field", "type": N(S("ProjectV2ItemOrderField", "ENUM"))}]),
            "ProjectV2ItemConnection": self.node("ProjectV2ItemConnection", "OBJECT", fields=[
                {"name": "content", "type": S("ProjectV2ItemContent", "UNION"), "args": []}]),
            "ProjectV2ItemContent": self.node("ProjectV2ItemContent", "UNION", possibleTypes=[{"name": "Issue"}, {"name": "DraftIssue"}]),
            # A entrada de cada mutation de ProjectV2 é sondada pelo nome (<Mutation>Input), mesmo sem ninguém citá-la.
            "CopyProjectV2Input": self.node("CopyProjectV2Input", "INPUT_OBJECT", inputFields=[
                {"name": "projectId", "type": N(S("ID"))}, {"name": "template", "type": S("ProjectV2TemplateOption", "INPUT_OBJECT")}]),
            "ProjectV2TemplateOption": self.node("ProjectV2TemplateOption", "INPUT_OBJECT", inputFields=[]),
        }
        self.run_read()
        probe = json.loads((self.root / "package" / "api-projects-inventory.json").read_text())["schema_probe"]
        types = probe["types"]
        self.assertEqual(types["ProjectV2"]["field_args"], {"items": {"first": "Int", "orderBy": "ProjectV2ItemOrder", "query": "String"},
                                                           "views": {"orderBy": "ProjectV2ViewOrder"}})
        self.assertNotIn("title", types["ProjectV2"]["field_args"])                       # campo sem argumentos não entra
        self.assertEqual(types["ProjectV2ItemOrder"]["input_fields"], {"direction": "OrderDirection!", "field": "ProjectV2ItemOrderField!"})
        self.assertEqual(types["ProjectV2ItemOrderField"]["enum_values"], ["POSITION"])   # seguido: o nome tem ProjectV2
        self.assertNotIn("OrderDirection", types)                                         # não seguido: o nome não tem ProjectV2
        self.assertEqual(types["ProjectV2ItemContent"]["possible_types"], ["DraftIssue", "Issue"])
        self.assertEqual(types["CopyProjectV2Input"]["input_fields"], {"projectId": "ID!", "template": "ProjectV2TemplateOption"})
        self.assertIn("ProjectV2TemplateOption", types)                                   # seguido a partir da entrada da mutation
        for mutation in ("createProjectV2", "addProjectV2ItemById"):
            self.assertIn(mutation[0].upper() + mutation[1:] + "Input", types)            # sondada pelo nome, mesmo ausente do schema (null)
        self.assertNotIn("DeleteIssueInput", types)                                       # mutation que não é de ProjectV2 não entra
        self.assertEqual(len(FakeGraph.probe_requests), len(set(FakeGraph.probe_requests)))   # nenhum tipo é pedido duas vezes
        self.assertIn("ProjectV2ItemOrderField", FakeGraph.probe_requests)

    def test_sonda_tem_teto_de_tipos(self):
        S, N = self.sref, self.non_null
        many = [{"name": f"f{n}", "type": N(S(f"ProjectV2Extra{n}", "OBJECT")), "args": []} for n in range(40)]
        FakeGraph.probe_nodes = {"ProjectV2": self.node("ProjectV2", "OBJECT", fields=many)}
        for n in range(40):
            FakeGraph.probe_nodes[f"ProjectV2Extra{n}"] = self.node(f"ProjectV2Extra{n}", "OBJECT", fields=[])
        with unittest.mock.patch.object(g, "PROBE_MAX_TYPES", 12):
            self.run_read()
        probe = json.loads((self.root / "package" / "api-projects-inventory.json").read_text())["schema_probe"]
        self.assertEqual(probe["status"], "OK")
        self.assertEqual(len(probe["types"]), 12)                                         # parou no teto
        self.assertGreater(len(probe["types_not_followed"]), 0)                           # e diz o que deixou de seguir

    def test_falha_na_sonda_nunca_derruba_a_leitura(self):
        FakeGraph.gql_errors = {"ProbeMutations": [{"type": "FORBIDDEN", "message": "introspection disabled"}]}
        st = self.run_read()
        self.assertEqual((st["status"], st["count"]), ("OK", 1))                  # a classe projects não é afetada
        probe = json.loads((self.root / "package" / "api-projects-inventory.json").read_text())["schema_probe"]
        self.assertEqual(probe["status"], "UNAVAILABLE")
        self.assertIn("introspection disabled", probe["detail"])
        self.assertNotIn("Unexpected", probe["detail"])             # erro da API é tratado como tal, não como falha inesperada
        FakeGraph.gql_errors = {}
        FakeGraph.probe_malformed = True                                          # resposta fora do formato: também não derruba
        st = self.run_read()
        self.assertEqual(st["status"], "OK")
        probe = json.loads((self.root / "package" / "api-projects-inventory.json").read_text())["schema_probe"]
        self.assertEqual(probe["status"], "UNAVAILABLE")
        self.assertIn("Unexpected", probe["detail"])
        FakeGraph.probe_malformed = False
        FakeGraph.http_fail = {"ProbeType": 500}
        self.run_read()
        self.assertEqual(json.loads((self.root / "package" / "api-projects-inventory.json").read_text())["schema_probe"]["status"], "UNAVAILABLE")

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
