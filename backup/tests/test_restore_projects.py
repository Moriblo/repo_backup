#!/usr/bin/env python3
"""Teste local de restore_projects.py (RST_REPO, etapa 4: Projects), contra uma API GraphQL SIMULADA e COM ESTADO.

O QUE PROVA
    - o planejamento (puro): casamento de itens com issues pelo TÍTULO (espaços normalizados), itens ocultos pelo
      campo Title, duplicados, outro repositório, tipos não restauráveis, rascunhos, ordem por posição;
    - a restauração de ponta a ponta: Project NOVO, privado, com o título + " (restaurado)", ligado ao repositório,
      campos (e opções do Status), views (campos visíveis traduzidos para os ids novos, filtro), itens na ordem,
      valores, arquivados, status updates, Project fechado;
    - a conferência por leitura de volta: um workflow que sobrescreve o Status é pego e corrigido (até 2 regravações),
      um que persiste ou que fecha a issue vira FAILED; falha de escrita para na hora e traz o nome da operação;
    - recusas sem escrever nada: token sem o escopo project, ids ausentes, teto de escritas;
    - o resultado declara o que não volta (workflows, seleção múltipla, itens sem issue) e o checklist dos workflows.
    NÃO prova nada contra o GitHub real: os nomes e formas do schema vêm da sonda e do que já rodou em leitura.

COMO RODAR
    python3 backup/tests/test_restore_projects.py        (saída 0 = tudo certo)
"""
import copy
import http.server
import json
import pathlib
import re
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE))
import github_projects_read as g  # noqa: E402
import restore_projects as rp  # noqa: E402
from test_github_projects_read import make_project, SOURCE, DEFAULT_TITLES  # noqa: E402

TOKEN = "ghp_SEGREDO_RESTAURA_789"
RESTORED = "dono/teste-restaurado"
NEW_BASE = 100                                  # número novo da issue n do backup = NEW_BASE + n


def page(nodes):
    return {"totalCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


class World:
    """Estado do GitHub simulado: Projects criados, issues (id de nó -> issue) e os cenários."""

    def __init__(self):
        self.projects, self.ops, self.seen_auth = {}, [], set()
        self.counter = 0
        self.issues = {f"NODE_{n + 1}": {"number": NEW_BASE + n + 1, "title": t} for n, t in enumerate(DEFAULT_TITLES)}
        self.issue_state = {NEW_BASE + n + 1: "open" for n in range(len(DEFAULT_TITLES))}
        self.default_public = True              # o Project novo nasce público: o restore tem de torná-lo privado
        self.status_on_add = True               # workflow padrão: item incluído -> Status = Todo
        self.drop_status = 0                    # as próximas N escritas de Status não ficam gravadas (workflow que sobrescreve)
        self.auto_close = False                 # workflow: Status = Done fecha a issue
        self.ignore = set()                     # mutations que respondem sucesso mas não gravam nada (a conferência tem de pegar)
        self.view_layout = None                 # o GitHub cria toda view com este layout, ignorando o pedido
        self.vanish = set()                     # adições que devolvem um id mas o item não fica no Project
        self.ignore_field = None                # nome do campo cuja criação é ignorada
        self.fail = {}                          # operationName -> lista `errors`
        self.flaky = {}                         # operationName -> respostas com erro interno antes de funcionar
        self.introspection = {}                 # tipo de payload -> campos (vazio: a introspecção falha)
        self.default_workflows = [("Item added to project", True), ("Auto-close issue", True), ("Auto-add to project", False)]

    def new_id(self, prefix):
        self.counter += 1
        return f"{prefix}_{self.counter}"


class FakeAPI(http.server.BaseHTTPRequestHandler):
    world = None

    def log_message(self, *args):
        pass

    def _send(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        w = FakeAPI.world
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        op, variables = body["operationName"], body.get("variables") or {}
        w.seen_auth.add(self.headers.get("Authorization"))
        if w.flaky.get(op, 0) > 0:
            w.flaky[op] -= 1
            return self._send({"data": None, "errors": [{"type": "INTERNAL", "message": "Something went wrong while executing your query. Please include `X`."}]})
        if op in w.fail:
            return self._send({"data": None, "errors": w.fail[op]})
        mutation = re.search(r"\{ (\w+)\(input", body["query"]) if body["query"].lstrip().startswith("mutation") else None
        if mutation:
            w.ops.append((op, mutation.group(1), variables["input"]))
            return self._send({"data": {mutation.group(1): self.mutate(w, mutation.group(1), variables["input"], body["query"])}})
        return self._send({"data": self.read(w, op, variables)})

    # --- escritas ---------------------------------------------------------------------------
    @staticmethod
    def field_of(p, field_id):
        return next(f for f in p["fields"] if f["id"] == field_id)

    @staticmethod
    def item_of(p, item_id):
        return next(i for i in p["items"] if i["id"] == item_id)

    def mutate(self, w, name, data, query):
        out = {"clientMutationId": None}
        if name == "createProjectV2":
            number = len(w.projects) + 1
            pid = f"PVT_new{number}"
            status = {"id": w.new_id("RF"), "name": "Status", "dataType": "SINGLE_SELECT", "__typename": "ProjectV2SingleSelectField",
                      "options": [{"id": w.new_id("RO"), "name": n, "color": "GRAY", "description": ""} for n in ("Todo", "In Progress", "Done")]}
            title = {"id": w.new_id("RF"), "name": "Title", "dataType": "TITLE", "__typename": "ProjectV2Field"}
            w.projects[pid] = {"id": pid, "number": number, "title": data["title"], "short": None, "readme": None, "public": w.default_public, "closed": False,
                               "repo_id": data.get("repositoryId"), "owner_id": data["ownerId"], "fields": [title, status],
                               "views": [{"id": w.new_id("RV"), "name": "View 1", "number": 1, "layout": "TABLE_LAYOUT", "filter": None, "visible": None}],
                               "workflows": [{"name": n, "enabled": e} for n, e in w.default_workflows], "items": [], "status_updates": []}
            out["projectV2"] = {"id": pid, "number": number, "url": f"https://github.com/users/dono/projects/{number}"}
            return out
        p = w.projects[data["projectId"]] if "projectId" in data else None
        if name in w.ignore or (name == "createProjectV2Field" and data["name"] == w.ignore_field):
            return out
        if name == "updateProjectV2":
            for src, dst in (("shortDescription", "short"), ("readme", "readme"), ("public", "public"), ("closed", "closed"), ("title", "title")):
                if src in data:
                    p[dst] = data[src]
        elif name == "createProjectV2Field":
            if any(f["name"] == data["name"] for f in p["fields"]):
                raise AssertionError("campo repetido")
            field = {"id": w.new_id("RF"), "name": data["name"], "dataType": data["dataType"], "__typename": "ProjectV2Field"}
            if data["dataType"] == "SINGLE_SELECT":
                field.update(__typename="ProjectV2SingleSelectField", options=[dict(o, id=w.new_id("RO")) for o in data["singleSelectOptions"]])
            if data["dataType"] == "ITERATION":
                cfg = data["iterationConfiguration"]
                field.update(__typename="ProjectV2IterationField", configuration={"duration": cfg["duration"], "startDay": 1, "completedIterations": [],
                                                                                   "iterations": [dict(i, id=w.new_id("RI")) for i in cfg["iterations"]]})
            p["fields"].append(field)
        elif name == "updateProjectV2Field":
            field = next(f for q in w.projects.values() for f in q["fields"] if f["id"] == data["fieldId"])
            old = {o["name"]: o["id"] for o in field["options"]}
            field["options"] = [dict(o, id=old.get(o["name"]) or w.new_id("RO")) for o in data["singleSelectOptions"]]
        elif name == "createProjectV2View":
            view = {"id": w.new_id("RV"), "name": data["name"], "number": len(p["views"]) + 1, "layout": w.view_layout or data["layout"], "filter": None,
                    "visible": (data.get("configuration") or {}).get("visibleFieldIds")}
            p["views"].append(view)
            out["projectV2View"] = {"id": view["id"]}
        elif name == "updateProjectV2View":
            view = next(v for q in w.projects.values() for v in q["views"] if v["id"] == data["viewId"])
            for key in ("name", "filter"):
                if key in data:
                    view[key] = data[key] or None
            if "configuration" in data:
                view["visible"] = data["configuration"].get("visibleFieldIds")
        elif name in ("addProjectV2ItemById", "addProjectV2DraftIssue"):
            item = {"id": w.new_id("PVTI"), "archived": False, "values": {}}
            if name == "addProjectV2ItemById":
                item["node"] = data["contentId"]
            else:
                item.update(draft={"title": data["title"], "body": data.get("body", "")})
            if w.status_on_add:
                item["values"]["Status"] = {"select": "Todo"}
            if name not in w.vanish:
                p["items"].append(item)
            out["item" if name == "addProjectV2ItemById" else "projectItem"] = {"id": item["id"]}
            if name == "addProjectV2ItemById" and '"item"' in json.dumps(w.introspection):
                pass
        elif name == "updateProjectV2ItemFieldValue":
            field, item = self.field_of(p, data["fieldId"]), self.item_of(p, data["itemId"])
            value = data["value"]
            if "singleSelectOptionId" in value:
                option = next(o["name"] for o in field["options"] if o["id"] == value["singleSelectOptionId"])
                if field["name"] == "Status" and w.drop_status > 0:
                    w.drop_status -= 1
                    return out
                item["values"][field["name"]] = {"select": option}
                if field["name"] == "Status" and option == "Done" and w.auto_close and item.get("node"):
                    w.issue_state[w.issues[item["node"]]["number"]] = "closed"
            elif "iterationId" in value:
                it = next(i for i in field["configuration"]["iterations"] if i["id"] == value["iterationId"])
                item["values"][field["name"]] = {"iteration": it}
            else:
                item["values"][field["name"]] = value
        elif name == "archiveProjectV2Item":
            self.item_of(p, data["itemId"])["archived"] = True
        elif name == "createProjectV2StatusUpdate":
            p["status_updates"].append(data)
        else:
            raise AssertionError(f"mutation desconhecida {name}")
        return out

    # --- leituras ---------------------------------------------------------------------------
    @staticmethod
    def details(p):
        views = [{"id": v["id"], "name": v["name"], "number": v["number"], "layout": v["layout"], "filter": v["filter"], "createdAt": "x", "updatedAt": "x"}
                 for v in p["views"]]
        return {"id": p["id"], "number": p["number"], "title": p["title"], "shortDescription": p["short"], "readme": p["readme"], "public": p["public"],
                "closed": p["closed"], "closedAt": None, "createdAt": "x", "updatedAt": "x", "url": f"https://github.com/users/dono/projects/{p['number']}",
                "owner": {"__typename": "User", "login": "dono"}, "creator": {"login": "dono"}, "repositories": page([{"nameWithOwner": RESTORED}]),
                "fields": page(copy.deepcopy(p["fields"])), "views": page(views),
                "workflows": page([{"id": f"W{n}", "name": w["name"], "number": n, "enabled": w["enabled"], "createdAt": "x", "updatedAt": "x"}
                                   for n, w in enumerate(p["workflows"], 1)]),
                "statusUpdates": page([])}

    @staticmethod
    def item_node(w, p, item):
        def select(field, option):
            return {"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": option, "optionId": "x", "color": "GRAY", "description": "",
                    "field": {"id": field["id"], "name": field["name"]}}
        values = []
        if item.get("draft"):
            title = item["draft"]["title"]
            content = {"__typename": "DraftIssue", "id": "DI", "title": title, "body": item["draft"]["body"], "createdAt": "x", "updatedAt": "x",
                       "creator": {"login": "dono"}, "assignees": {"nodes": []}}
        else:
            issue = w.issues[item["node"]]
            title = issue["title"]
            content = {"__typename": "Issue", "id": item["node"], "number": issue["number"], "title": title, "url": "u", "state": w.issue_state[issue["number"]].upper(),
                       "repository": {"nameWithOwner": RESTORED}}
        values.append({"__typename": "ProjectV2ItemFieldTextValue", "text": title, "field": {"id": "t", "name": "Title"}})
        for name, v in item["values"].items():
            field = next(f for f in p["fields"] if f["name"] == name)
            if "select" in v:
                values.append(select(field, v["select"]))
            elif "text" in v:
                values.append({"__typename": "ProjectV2ItemFieldTextValue", "text": v["text"], "field": {"id": field["id"], "name": name}})
            elif "number" in v:
                values.append({"__typename": "ProjectV2ItemFieldNumberValue", "number": v["number"], "field": {"id": field["id"], "name": name}})
            elif "date" in v:
                values.append({"__typename": "ProjectV2ItemFieldDateValue", "date": v["date"], "field": {"id": field["id"], "name": name}})
            else:
                values.append({"__typename": "ProjectV2ItemFieldIterationValue", "title": v["iteration"]["title"], "iterationId": v["iteration"]["id"],
                               "startDate": v["iteration"]["startDate"], "duration": v["iteration"]["duration"], "field": {"id": field["id"], "name": name}})
        return {"id": item["id"], "type": "DRAFT_ISSUE" if item.get("draft") else "ISSUE", "isArchived": item["archived"], "createdAt": "x", "updatedAt": "x",
                "creator": {"login": "dono"}, "content": content, "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": values}}

    def read(self, w, op, variables):
        if op == "PayloadFields":
            fields = w.introspection.get(variables["n"])
            return {"__type": {"fields": [{"name": n} for n in fields]} if fields else None}
        p = w.projects[variables["id"]]
        if op == "ProjectDetails":
            return {"node": self.details(p)}
        live, archived = [i for i in p["items"] if not i["archived"]], [i for i in p["items"] if i["archived"]]
        if op == "ProjectItems":
            return {"node": {"items": dict(page([self.item_node(w, p, i) for i in live]))}}
        if op == "ProjectItemsArchived":
            return {"node": {"items": page([self.item_node(w, p, i) for i in archived])}}
        if op == "ProjectItemsOrder":
            return {"node": {"items": page([{"id": i["id"]} for i in p["items"]])}}
        keys = {"ProjectViewVisibleFields": "configuration", "ProjectViewVisibleFieldsSmall": "configuration", "ProjectViewFields": "fields",
                "ProjectViewGroupBy": "groupByFields", "ProjectViewVerticalGroupBy": "verticalGroupByFields", "ProjectViewSortBy": "sortByFields"}
        nodes = []
        for v in p["views"]:
            shown = [f for f in p["fields"] if v["visible"] is None or f["id"] in v["visible"]]
            part = {"configuration": {"visibleFields": page([{"id": f["id"], "name": f["name"]} for f in shown])},
                    "fields": page([{"id": f["id"], "name": f["name"]} for f in shown])}.get(keys[op], page([]))
            nodes.append({"number": v["number"], keys[op]: part})
        return {"node": {"views": {"nodes": nodes}}}


def source_project(number=13, **kw):
    """(Project do backup, itens com project_number e position_order) a partir do simulado da leitura."""
    made = make_project(number=number, **kw)
    for view in made["details"]["views"]["nodes"]:                      # as partes das views, que o leitor mescla em `details`
        view.update(copy.deepcopy(made["view_parts"][view["number"]]))
    items = []
    for n, item in enumerate(made["items"]):
        item = copy.deepcopy(item)
        item.update(project_number=number, position_order=n)
        items.append(item)
    return made["details"], items


def backup_issues(titles=None):
    return [{"number": n, "title": t} for n, t in enumerate(titles or DEFAULT_TITLES, 1)]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.world = FakeAPI.world = World()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = pathlib.Path(self.tmp.name) / "evidence"

    def run_restore(self, projects=None, items=None, issues=None, **kw):
        if projects is None:
            details, items = source_project()
            projects = [details]
        issues = issues if issues is not None else backup_issues()
        args = dict(source=SOURCE, projects=projects, items=items, issues=issues,
                    issue_map={i["number"]: NEW_BASE + i["number"] for i in issues}, node_ids={i["number"]: f"NODE_{i['number']}" for i in issues},
                    issue_states={i["number"]: "open" for i in issues}, repo_node_id="R_new", owner_id="U_dono", api_base=self.base, token=TOKEN,
                    token_scopes=["repo", "project"], budget=450, rest_get=self.rest_get, evidence_dir=self.evidence,
                    write_delay=0, rate_wait=0, settle=0, tries=2, delay=0)
        args.update(kw)
        return rp.restore_projects(**args)

    def rest_get(self, number):
        return 200, {"state": self.world.issue_state.get(number, "open")}

    def project(self, n=1):
        return self.world.projects[f"PVT_new{n}"]

    def ops(self, name=None):
        return [o for o in self.world.ops if name is None or o[1] == name]

    def titles(self, p):
        return [i["draft"]["title"] if i.get("draft") else self.world.issues[i["node"]]["title"] for i in p["items"]]


class PlanTest(unittest.TestCase):
    def plan(self, items, issues=None, issue_map=None, node_ids=None, source=SOURCE, **kw):
        details, base_items = source_project(**kw)
        items = base_items if items is None else items
        issues = issues if issues is not None else backup_issues()
        return rp.plan_projects(source, [details], items, issues, issue_map if issue_map is not None else {i["number"]: 100 + i["number"] for i in issues},
                                node_ids if node_ids is not None else {i["number"]: f"N{i['number']}" for i in issues})[0]

    def test_plano_basico_na_ordem_da_posicao(self):
        plan = self.plan(None)
        self.assertEqual(plan["title"], "Quadro de teste (backup_teste_issues) (restaurado)")
        self.assertEqual([s["kind"] for s in plan["items"]], ["issue"] * 6 + ["draft"])
        self.assertEqual((plan["skipped"], plan["unsupported"]), ([], []))
        details, items = source_project()
        items[0]["position_order"], items[1]["position_order"] = 9, 0                  # troca a posição do 1º e do 2º
        order = [s["title"] for s in self.plan(items)["items"]]
        self.assertEqual(order[:2], [DEFAULT_TITLES[1], DEFAULT_TITLES[2]])
        self.assertEqual(self.plan(items)["items"][-1]["title"], DEFAULT_TITLES[0])

    def test_itens_ocultos_sao_casados_pelo_titulo_do_campo_title(self):
        details, items = source_project(hidden=True)
        plan = self.plan(items)
        self.assertEqual([s["old"] for s in plan["items"] if s["kind"] == "issue"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(plan["skipped"], [])

    def test_titulo_com_espacos_diferentes_casa(self):
        details, items = source_project(hidden=True, issue_titles=["  Erro   ao salvar "])
        plan = self.plan(items, issues=[{"number": 1, "title": "Erro ao salvar"}])
        self.assertEqual([s["old"] for s in plan["items"] if s["kind"] == "issue"], [1])

    def test_titulo_ausente_ou_duplicado_nao_e_ligado(self):
        details, items = source_project(hidden=True, issue_titles=["Sem par", "Repetida"])
        plan = self.plan(items, issues=[{"number": 1, "title": "Repetida"}, {"number": 2, "title": "Repetida"}])
        reasons = {s["title"]: s["reason"] for s in plan["skipped"]}
        self.assertIn("nenhuma issue", reasons["Sem par"])
        self.assertIn("ambíguo", reasons["Repetida"])
        self.assertEqual([s["kind"] for s in plan["items"]], ["draft"])

    def test_dois_itens_com_o_mesmo_titulo_ligam_so_o_primeiro(self):
        details, items = source_project(hidden=True, issue_titles=["Igual", "Igual"])
        plan = self.plan(items, issues=[{"number": 1, "title": "Igual"}])
        self.assertEqual(len([s for s in plan["items"] if s["kind"] == "issue"]), 1)
        self.assertIn("já está no Project", plan["skipped"][0]["reason"])

    def test_issue_de_outro_repositorio_e_pull_request_ficam_de_fora(self):
        details, items = source_project(drafts=0, issue_titles=["A", "B", "C"])
        items[0]["content"]["repository"]["nameWithOwner"] = "outro/repo"
        items[1]["type"], items[1]["content"] = "PULL_REQUEST", {"__typename": "PullRequest", "title": "B"}
        plan = self.plan(items, issues=[{"number": 1, "title": "A"}, {"number": 2, "title": "B"}, {"number": 3, "title": "C"}])
        self.assertEqual([s["old"] for s in plan["items"]], [3])
        reasons = " | ".join(s["reason"] for s in plan["skipped"])
        self.assertIn("outro/repo", reasons)
        self.assertIn("PULL_REQUEST", reasons)

    def test_issue_nao_restaurada_fica_de_fora(self):
        plan = self.plan(None, issue_map={n: 100 + n for n in range(1, 6)})              # a 6 não foi criada
        self.assertEqual(len([s for s in plan["items"] if s["kind"] == "issue"]), 5)
        self.assertIn("não foi restaurada", plan["skipped"][0]["reason"])
        plan = self.plan(None, node_ids={n: f"N{n}" for n in range(1, 6)})                # a 6 sem id de nó
        self.assertEqual(len(plan["skipped"]), 1)

    def test_multipla_selecao_e_declarada(self):
        details, items = source_project()
        details["fields"]["nodes"].append({"id": "F_m", "name": "Etiquetas", "dataType": "MULTI_SELECT", "__typename": "ProjectV2SingleSelectField"})
        plan = rp.plan_projects(SOURCE, [details], items, backup_issues(), {n: 100 + n for n in range(1, 7)}, {n: f"N{n}" for n in range(1, 7)})[0]
        self.assertEqual(plan["unsupported"], ["Etiquetas"])
        self.assertNotIn("Etiquetas", [f["name"] for f in plan["fields"]])

    def test_valores_restauraveis_do_item(self):
        details, items = source_project()
        values, unsupported = rp.item_values(items[0], {"Status", "Prioridade"})
        self.assertEqual(sorted(values), [("Prioridade", "select", "Alta"), ("Status", "select", "In Progress")])
        self.assertEqual(unsupported, [])
        self.assertEqual(rp.item_values(items[0], {"Status"})[0], [("Status", "select", "In Progress")])

    def test_valor_de_selecao_multipla_e_declarado_e_nao_restaurado(self):
        details, items = source_project()
        items[0]["fieldValues"]["nodes"].append({"__typename": "ProjectV2ItemFieldMultiSelectValue", "field": {"id": "F_m", "name": "Etiquetas"}})
        values, unsupported = rp.item_values(items[0], {"Status", "Prioridade"})
        self.assertEqual(unsupported, ["Etiquetas"])
        self.assertNotIn("Etiquetas", [v[0] for v in values])

    def test_norm_e_titulo_do_item(self):
        self.assertEqual(rp.norm("  a \n b  "), "a b")
        self.assertEqual(rp.norm(None), "")
        details, items = source_project(hidden=True)
        self.assertEqual(rp.item_title(items[0]), DEFAULT_TITLES[0])
        self.assertEqual(rp.item_title(items[-1]), "Rascunho sem issue")

    def test_estimativa_de_escritas_cobre_o_que_o_plano_faz(self):
        plan = self.plan(None)
        self.assertEqual(plan["writes"], 2 + 2 + 1 + (1 + 2) + 7 + (6 * 2 + 1))


class RestoreTest(Base):
    def test_restaura_projeto_completo(self):
        result = self.run_restore()
        self.assertEqual(result["status"], "OK", result)
        p = self.project()
        self.assertEqual(p["title"], "Quadro de teste (backup_teste_issues) (restaurado)")
        self.assertIs(p["public"], False)                                  # o Project nasceu público no simulado: o restore o tornou privado
        self.assertEqual((p["repo_id"], p["owner_id"]), ("R_new", "U_dono"))
        self.assertEqual(self.titles(p), DEFAULT_TITLES + ["Rascunho sem issue"])
        by_name = {f["name"]: f for f in p["fields"]}
        self.assertEqual([o["name"] for o in by_name["Prioridade"]["options"]], ["Alta", "Média", "Baixa"])
        self.assertEqual([i["values"]["Status"]["select"] for i in p["items"]], ["In Progress", "Todo", "In Progress", "Todo", "Todo", "Done", "Todo"])
        self.assertEqual([i["values"].get("Prioridade", {}).get("select") for i in p["items"]], ["Alta", "Baixa", "Alta", "Média", "Média", "Baixa", None])
        self.assertEqual([(v["name"], v["layout"], v["filter"]) for v in p["views"]], [("View 1", "TABLE_LAYOUT", None), ("View 2", "BOARD_LAYOUT", "status:Todo")])
        self.assertEqual(p["views"][1]["visible"], [by_name["Title"]["id"]])        # campos visíveis traduzidos para os ids do Project novo
        self.assertEqual(self.ops("createProjectV2")[0][2], {"ownerId": "U_dono", "title": p["title"], "repositoryId": "R_new"})
        self.assertEqual(result["projects"][0]["target_number"], 1)
        self.assertEqual(result["projects"][0]["items"]["restored"], 7)
        self.assertLessEqual(result["writes"], result["planned_writes"])
        self.assertEqual(self.world.seen_auth, {f"Bearer {TOKEN}"})

    def test_a_primeira_view_reaproveita_a_padrao_e_a_de_outro_layout_e_criada(self):
        self.run_restore()
        self.assertEqual(len(self.ops("createProjectV2View")), 1)           # só a "View 2" (board) foi criada
        details, items = source_project()
        details["views"]["nodes"][0]["layout"] = "BOARD_LAYOUT"
        self.setUp()
        self.run_restore(projects=[details], items=items)
        self.assertEqual(len(self.ops("createProjectV2View")), 2)

    def test_opcoes_do_status_diferentes_sao_atualizadas(self):
        details, items = source_project()
        status = next(f for f in details["fields"]["nodes"] if f["name"] == "Status")
        status["options"].append({"id": "S9", "name": "Bloqueado", "color": "RED", "description": "x"})
        items[0]["fieldValues"]["nodes"][1].update(name="Bloqueado", optionId="S9")
        result = self.run_restore(projects=[details], items=items)
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(len(self.ops("updateProjectV2Field")), 1)
        self.assertEqual(self.project()["items"][0]["values"]["Status"]["select"], "Bloqueado")

    def test_itens_ocultos_sao_ligados_pelo_titulo(self):
        details, items = source_project(hidden=True)
        result = self.run_restore(projects=[details], items=items)
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(self.titles(self.project()), DEFAULT_TITLES + ["Rascunho sem issue"])

    def test_item_sem_issue_correspondente_vira_partial_e_e_listado(self):
        details, items = source_project(hidden=True, issue_titles=DEFAULT_TITLES[:5] + ["Sem issue no backup"])
        result = self.run_restore(projects=[details], items=items)
        self.assertEqual(result["status"], "PARTIAL", result)
        proj = result["projects"][0]
        self.assertEqual((proj["status"], proj["items"]["restored"], proj["items"]["skipped_total"]), ("PARTIAL", 6, 1))
        self.assertIn("Sem issue no backup", proj["items"]["skipped"][0]["title"])
        self.assertEqual(len(self.project()["items"]), 6)

    def test_selecao_multipla_deixa_partial(self):
        details, items = source_project()
        details["fields"]["nodes"].append({"id": "F_m", "name": "Etiquetas", "dataType": "MULTI_SELECT", "__typename": "ProjectV2SingleSelectField"})
        result = self.run_restore(projects=[details], items=items)
        self.assertEqual((result["status"], result["projects"][0]["unsupported"]), ("PARTIAL", ["Etiquetas"]))

    def test_item_arquivado_volta_arquivado_e_na_mesma_posicao(self):
        details, items = source_project(archived=1)                       # o arquivado é o último (issue 90, "Item arquivado")
        issues = backup_issues() + [{"number": 90, "title": "Item arquivado"}]
        self.world.issues["NODE_90"] = {"number": NEW_BASE + 90, "title": "Item arquivado"}
        self.world.issue_state[NEW_BASE + 90] = "open"
        result = self.run_restore(projects=[details], items=items, issues=issues, issue_states={i["number"]: "open" for i in issues})
        self.assertEqual(result["status"], "OK", result)
        p = self.project()
        self.assertEqual([i["archived"] for i in p["items"]], [False] * 7 + [True])
        self.assertEqual(self.titles(p)[-1], "Item arquivado")
        self.assertEqual(result["projects"][0]["items"]["archived"], 1)
        self.assertEqual(len(self.ops("archiveProjectV2Item")), 1)

    def test_project_fechado_e_status_update(self):
        details, items = source_project()
        details["closed"] = True
        details["shortDescription"], details["readme"] = "curta", "# leia"
        details["statusUpdates"]["nodes"] = [{"id": "SU", "body": "tudo certo", "status": "ON_TRACK", "startDate": "2026-10-01", "targetDate": None, "createdAt": "x"}]
        result = self.run_restore(projects=[details], items=items)
        self.assertEqual(result["status"], "OK", result)
        p = self.project()
        self.assertEqual((p["closed"], p["short"], p["readme"]), (True, "curta", "# leia"))
        self.assertEqual(p["status_updates"][0]["projectId"], p["id"])
        self.assertEqual((p["status_updates"][0]["body"], p["status_updates"][0]["status"], p["status_updates"][0]["startDate"]),
                         ("tudo certo", "ON_TRACK", "2026-10-01"))
        self.assertNotIn("targetDate", p["status_updates"][0])

    def test_campos_de_texto_numero_data_e_iteracao(self):
        details, items = source_project()
        nodes = details["fields"]["nodes"]
        nodes += [{"id": "F_txt", "name": "Nota", "dataType": "TEXT", "__typename": "ProjectV2Field"},
                  {"id": "F_num", "name": "Pontos", "dataType": "NUMBER", "__typename": "ProjectV2Field"},
                  {"id": "F_dt", "name": "Prazo", "dataType": "DATE", "__typename": "ProjectV2Field"},
                  {"id": "F_it", "name": "Sprint", "dataType": "ITERATION", "__typename": "ProjectV2IterationField",
                   "configuration": {"duration": 14, "startDay": 1, "completedIterations": [],
                                     "iterations": [{"id": "I1", "title": "Sprint 1", "startDate": "2026-10-05", "duration": 14}]}}]
        ref = lambda n: {"id": f"F_{n}", "name": {"txt": "Nota", "num": "Pontos", "dt": "Prazo", "it": "Sprint"}[n]}
        items[0]["fieldValues"]["nodes"] += [
            {"__typename": "ProjectV2ItemFieldTextValue", "text": "olá", "field": ref("txt")},
            {"__typename": "ProjectV2ItemFieldNumberValue", "number": 3, "field": ref("num")},
            {"__typename": "ProjectV2ItemFieldDateValue", "date": "2026-10-09", "field": ref("dt")},
            {"__typename": "ProjectV2ItemFieldIterationValue", "title": "Sprint 1", "iterationId": "I1", "startDate": "2026-10-05", "duration": 14, "field": ref("it")}]
        result = self.run_restore(projects=[details], items=items)
        self.assertEqual(result["status"], "OK", result)
        values = self.project()["items"][0]["values"]
        self.assertEqual((values["Nota"], values["Pontos"], values["Prazo"]), ({"text": "olá"}, {"number": 3}, {"date": "2026-10-09"}))
        self.assertEqual(values["Sprint"]["iteration"]["title"], "Sprint 1")
        self.assertEqual(self.ops("createProjectV2Field")[-1][2]["iterationConfiguration"],
                         {"duration": 14, "startDate": "2026-10-05", "iterations": [{"title": "Sprint 1", "startDate": "2026-10-05", "duration": 14}]})

    def test_campo_existente_com_outro_tipo_nao_e_recriado_e_vira_partial(self):
        details, items = source_project()
        next(f for f in details["fields"]["nodes"] if f["name"] == "Status").update(dataType="TEXT", __typename="ProjectV2Field")
        items = [dict(i, fieldValues={"pageInfo": {"hasNextPage": False}, "nodes": [n for n in i["fieldValues"]["nodes"] if n["field"]["name"] != "Status"]}) for i in items]
        result = self.run_restore(projects=[details], items=items)
        self.assertEqual(result["status"], "PARTIAL", result)
        self.assertIn("outro tipo", result["notes"][0])

    def test_checklist_dos_workflows(self):
        result = self.run_restore()
        flows = {w["name"]: w for w in result["projects"][0]["workflows"]}
        self.assertEqual(flows["wf1"]["action"], "ligar (não existe no Project restaurado)")
        text = (self.evidence / "RESTAURAR-PROJETO.txt").read_text(encoding="utf-8")
        self.assertIn("wf1: no backup LIGADO; no restaurado ausente", text)
        self.assertIn("https://github.com/users/dono/projects/1", text)
        self.assertNotIn(TOKEN, text)
        details, items = source_project()
        details["workflows"]["nodes"] = [{"id": "W", "name": "Item added to project", "number": 1, "enabled": False, "createdAt": "x", "updatedAt": "x"},
                                         {"id": "W", "name": "Auto-add to project", "number": 3, "enabled": False, "createdAt": "x", "updatedAt": "x"}]
        self.setUp()
        flows = {w["name"]: w["action"] for w in self.run_restore(projects=[details], items=items)["projects"][0]["workflows"]}
        self.assertEqual(flows, {"Item added to project": "desligar", "Auto-add to project": "nada"})

    def test_dois_projects_sao_restaurados_na_ordem_do_numero(self):
        d13, i13 = source_project(13)
        d14, i14 = source_project(14, issue_titles=DEFAULT_TITLES[:2], drafts=0)
        d14["title"] = "Outro"
        result = self.run_restore(projects=[d14, d13], items=i13 + i14)
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual([p["title"] for p in self.world.projects.values()], ["Quadro de teste (backup_teste_issues) (restaurado)", "Outro (restaurado)"])
        self.assertEqual([len(p["items"]) for p in self.world.projects.values()], [7, 2])


class VerificationTest(Base):
    def test_workflow_que_sobrescreve_o_status_e_pego_e_corrigido(self):
        self.world.drop_status = 1
        result = self.run_restore()
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual((result["projects"][0]["repair_passes"], result["projects"][0]["verify_attempts"]), (1, 2))
        self.assertEqual(len(self.ops("updateProjectV2ItemFieldValue")), 13 + 1)
        self.assertEqual(self.project()["items"][0]["values"]["Status"]["select"], "In Progress")

    def test_workflow_que_sempre_sobrescreve_vira_failed_com_o_valor_divergente(self):
        self.world.drop_status = 999
        result = self.run_restore(tries=4)
        self.assertEqual(result["status"], "FAILED", result)
        proj = result["projects"][0]
        self.assertEqual(proj["repair_passes"], 2)                           # no máximo 2 regravações
        self.assertIn("Status", proj["mismatches"][0])
        self.assertIn("divergência", result["reason"])

    def test_workflow_que_fecha_a_issue_vira_failed(self):
        self.world.auto_close = True
        result = self.run_restore()
        self.assertEqual(result["status"], "FAILED", result)
        self.assertTrue(any("estado" in m and "workflow" in m for m in result["projects"][0]["mismatches"]), result)

    def test_issue_que_ja_era_fechada_nao_e_divergencia(self):
        self.world.auto_close = True
        self.world.issue_state[NEW_BASE + 6] = "closed"                      # a 6 estava fechada (Done) no backup
        issues = backup_issues()
        result = self.run_restore(issue_states={i["number"]: ("closed" if i["number"] == 6 else "open") for i in issues})
        self.assertEqual(result["status"], "OK", result)

    def test_titulo_ou_visibilidade_divergentes_na_releitura_falham(self):
        self.world.default_public = True
        original = rp.Writer.write

        def keep_public(writer, operation, mutation, data, returns=None):
            if mutation == "updateProjectV2" and "public" in data:
                data = {k: v for k, v in data.items() if k != "public"}           # o GitHub "ignora" a mudança de visibilidade
            return original(writer, operation, mutation, data, returns)
        rp.Writer.write = keep_public
        self.addCleanup(setattr, rp.Writer, "write", original)
        result = self.run_restore()
        self.assertEqual(result["status"], "FAILED", result)
        self.assertIn("public", result["projects"][0]["mismatches"][0])

    def test_item_fora_da_ordem_esperada_falha(self):
        original = FakeAPI.read.__func__ if hasattr(FakeAPI.read, "__func__") else FakeAPI.read

        def reversed_order(handler, w, op, variables):
            data = original(handler, w, op, variables)
            if op == "ProjectItemsOrder":
                data["node"]["items"]["nodes"].reverse()                  # a posição devolvida não é a da inclusão
            return data
        FakeAPI.read = reversed_order
        self.addCleanup(setattr, FakeAPI, "read", original)
        result = self.run_restore()
        self.assertEqual(result["status"], "FAILED", result)
        self.assertIn("na posição 1", result["projects"][0]["mismatches"][0])


class SilentFailureTest(Base):
    """O GitHub responde sucesso mas não grava: só a releitura de conferência pode pegar."""

    def failed(self, **kw):
        result = self.run_restore(**kw)
        self.assertEqual(result["status"], "FAILED", result)
        return " | ".join(result["projects"][0]["mismatches"])

    def test_arquivamento_ignorado(self):
        details, items = source_project(archived=1)
        issues = backup_issues() + [{"number": 90, "title": "Item arquivado"}]
        self.world.issues["NODE_90"] = {"number": NEW_BASE + 90, "title": "Item arquivado"}
        self.world.issue_state[NEW_BASE + 90] = "open"
        self.world.ignore = {"archiveProjectV2Item"}
        self.assertIn("arquivado", self.failed(projects=[details], items=items, issues=issues, issue_states={i["number"]: "open" for i in issues}))

    def test_item_que_nao_ficou_no_project(self):
        self.world.vanish = {"addProjectV2DraftIssue"}                     # o último item (rascunho) não é gravado
        details, items = source_project()
        items[-1]["fieldValues"]["nodes"] = items[-1]["fieldValues"]["nodes"][:1]          # só o Title: nenhuma escrita de valor no item sumido
        self.assertIn("6 itens no Project restaurado, esperava 7", self.failed(projects=[details], items=items))

    def test_filtro_da_view_ignorado(self):
        self.world.ignore = {"updateProjectV2View"}
        self.assertIn("view View 2", self.failed())

    def test_layout_da_view_ignorado(self):
        self.world.view_layout = "TABLE_LAYOUT"
        self.assertIn("view View 2", self.failed())

    def test_campo_que_nao_foi_criado(self):
        self.world.ignore_field = "Prioridade"
        self.assertIn("campo Prioridade ausente", self.failed())

    def test_opcoes_do_status_nao_atualizadas(self):
        details, items = source_project()
        next(f for f in details["fields"]["nodes"] if f["name"] == "Status")["options"].append({"id": "S9", "name": "Bloqueado", "color": "RED", "description": ""})
        self.world.ignore = {"updateProjectV2Field"}
        self.assertIn("campo Status: opções diferentes", self.failed(projects=[details], items=items))

    def test_falha_de_conferencia_no_primeiro_project_nao_tenta_o_segundo(self):
        d13, i13 = source_project(13)
        d14, i14 = source_project(14)
        self.world.drop_status = 999
        result = self.run_restore(projects=[d13, d14], items=i13 + i14)
        self.assertEqual((result["status"], len(result["projects"]), len(self.ops("createProjectV2"))), ("FAILED", 1, 1))

    def test_espera_antes_de_gravar_os_valores(self):
        import unittest.mock
        with unittest.mock.patch.object(rp.time, "sleep") as sleep:
            self.run_restore(settle=7.5)
        self.assertIn(unittest.mock.call(7.5), sleep.call_args_list)


class FailureTest(Base):
    def test_falha_de_escrita_para_na_hora_e_nomeia_a_operacao(self):
        self.world.fail["RstCreateField"] = [{"type": "UNPROCESSABLE", "message": "nome inválido"}]
        result = self.run_restore()
        self.assertEqual(result["status"], "FAILED", result)
        self.assertIn("[RstCreateField] UNPROCESSABLE: nome inválido", result["reason"])
        self.assertEqual(self.ops("addProjectV2ItemById"), [])               # nada depois da falha
        self.assertEqual(len(self.world.projects), 1)

    def test_falha_ao_criar_o_projeto_nao_deixa_nada(self):
        self.world.fail["RstCreateProject"] = [{"type": "FORBIDDEN", "message": "sem permissão"}]
        result = self.run_restore()
        self.assertEqual((result["status"], self.world.projects), ("FAILED", {}))
        self.assertIn("FORBIDDEN", result["reason"])

    def test_falha_no_primeiro_project_nao_tenta_o_segundo(self):
        d13, i13 = source_project(13)
        d14, i14 = source_project(14)
        self.world.fail["RstUpdateProject"] = [{"type": "X", "message": "y"}]
        self.run_restore(projects=[d13, d14], items=i13 + i14)
        self.assertEqual(len(self.ops("createProjectV2")), 1)

    def test_erro_interno_do_github_e_repetido(self):
        self.world.flaky["RstCreateProject"] = 1
        result = self.run_restore()
        self.assertEqual(result["status"], "OK", result)

    def test_erro_interno_persistente_falha(self):
        self.world.flaky["RstCreateProject"] = 99
        result = self.run_restore()
        self.assertEqual(result["status"], "FAILED", result)
        self.assertIn("RstCreateProject", result["reason"])

    def test_erro_na_leitura_de_conferencia_vira_failed(self):
        self.world.fail["ProjectItems"] = [{"type": "X", "message": "leitura quebrou"}]
        result = self.run_restore()
        self.assertEqual(result["status"], "FAILED", result)
        self.assertIn("leitura de conferência", result["reason"])

    def test_checklist_e_gravado_mesmo_na_falha_e_sem_token(self):
        self.world.fail["RstCreateField"] = [{"type": "X", "message": "y"}]
        self.run_restore()
        text = (self.evidence / "RESTAURAR-PROJETO.txt").read_text(encoding="utf-8")
        self.assertIn("O QUE FAZER À MÃO", text)
        self.assertNotIn(TOKEN, text)


class RefusalTest(Base):
    def test_token_sem_escopo_project_nao_escreve(self):
        result = self.run_restore(token_scopes=["repo", "workflow"])
        self.assertEqual(result["status"], "NOT_VERIFIED")
        self.assertIn("escopo project", result["reason"])
        self.assertEqual(self.world.ops, [])

    def test_token_fine_grained_sem_cabecalho_de_escopos_segue(self):
        self.assertEqual(self.run_restore(token_scopes=None)["status"], "OK")

    def test_sem_ids_de_dono_ou_repositorio_nao_escreve(self):
        for key in ("owner_id", "repo_node_id"):
            self.assertEqual(self.run_restore(**{key: None})["status"], "NOT_VERIFIED")
        self.assertEqual(self.world.ops, [])

    def test_teto_de_escritas_recusa_antes_de_escrever(self):
        result = self.run_restore(budget=10)
        self.assertEqual(result["status"], "NOT_VERIFIED")
        self.assertIn("nada foi escrito", result["reason"])
        self.assertEqual(self.world.ops, [])
        result = self.run_restore(budget=-5)
        self.assertIn("(0)", result["reason"])

    def test_teto_exato_basta(self):
        planned = self.run_restore(budget=0)["reason"]
        need = int(re.search(r"cerca de (\d+)", planned).group(1))
        self.assertEqual(self.run_restore(budget=need)["status"], "OK")


class PayloadFieldTest(Base):
    def test_o_campo_do_payload_e_descoberto_por_introspeccao(self):
        self.world.introspection = {"AddProjectV2DraftIssuePayload": ["clientMutationId", "item"]}
        writer = rp.Writer(self.base, TOKEN, 0, 0)
        self.assertEqual(writer.payload_field("addProjectV2DraftIssue", ["projectItem", "item"]), "item")

    def test_sem_introspeccao_usa_o_primeiro_candidato(self):
        writer = rp.Writer(self.base, TOKEN, 0, 0)
        self.assertEqual(writer.payload_field("addProjectV2DraftIssue", ["projectItem", "item"]), "projectItem")
        self.assertEqual(self.run_restore()["status"], "OK")

    def test_a_introspeccao_nao_conta_como_escrita(self):
        self.world.introspection = {"CreateProjectV2Payload": ["projectV2"]}
        result = self.run_restore()
        self.assertEqual(result["writes"], len(self.world.ops))


if __name__ == "__main__":
    unittest.main(verbosity=1)
