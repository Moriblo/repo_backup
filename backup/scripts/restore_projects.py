#!/usr/bin/env python3
"""Restauração de Projects (GitHub Projects v2) a partir do pacote de um backup (RST_REPO, etapa 4).

O QUE É
    Módulo usado pelo restore_repo.py, depois de as issues terem voltado OK. Para cada Project do pacote
    (`api-projects.json` e `api-project-items.json`) cria um Project NOVO, sempre PRIVADO, com o título original
    mais " (restaurado)", ligado ao repositório restaurado, e recria nele campos, views, itens, valores e status
    updates. Nada é apagado e nenhum Project existente é tocado.

O QUE É RESTAURADO
    Metadados (descrição, readme, estado fechado), campos personalizados (texto, número, data, seleção única,
    iteração) e as opções do Status, views (nome, layout, filtro e campos visíveis, quando o backup os tem),
    itens na ordem original (issues casadas pelo TÍTULO, rascunhos recriados, itens arquivados de volta
    arquivados), valores dos campos e status updates.

O QUE NÃO É RESTAURADO (declarado no resultado)
    - Regras dos workflows: a API do GitHub não as expõe nem permite criá-las. O resultado traz um checklist com
      o nome e o estado ligado ou desligado de cada workflow, comparado com o Project restaurado.
    - Agrupamento, agrupamento vertical e ordenação das views: legíveis, mas sem campo de escrita.
    - Colaboradores e permissões do Project, datas de criação e autores originais.
    - Campos de seleção múltipla (o leitor do backup ainda não lê as opções nem os valores).
    - Itens que não são issue deste repositório nem rascunho: pull requests, issues de outro repositório, itens
      ocultos que não casam por título.

CASAMENTO DOS ITENS COM AS ISSUES
    O token de leitura do backup não enxerga repositório privado: o conteúdo do item vem oculto, mas o título
    vem pelo campo Title. O item é casado com a issue do mesmo backup de título igual (espaços normalizados).
    Título repetido ou ausente = item NÃO ligado e reportado; um item que aparece com o conteúdo visível usa o
    número da issue.

CONFERÊNCIA
    Relê o Project novo com o MESMO leitor do backup (github_projects_read.py) e compara metadados, campos,
    views, itens (tipo, título, arquivamento, valores e ORDEM) e o estado de cada issue casada (os workflows
    padrão do Project novo podem fechar issues ou mudar o Status). Valor que o GitHub sobrescreve depois da
    escrita é regravado (até duas passagens de reparo). Divergência que sobra = FAILED.

REGRAS QUE ESTE MÓDULO NUNCA QUEBRA
    - Só escreve no Project que ele mesmo criou (e lê o resto). A origem nunca é escrita.
    - Recusa ANTES de escrever se o plano não cabe no teto de escritas.
    - Para na primeira falha de escrita.
"""
import json
import pathlib
import re
import time
import urllib.error
import urllib.request

import github_projects_read as pr

# Tipos de campo que o GitHub cria sozinho em todo Project (não são recriados) e os que a API deixa criar.
CUSTOM_TYPES = ("TEXT", "NUMBER", "DATE", "SINGLE_SELECT", "ITERATION")
SUFFIX = " (restaurado)"
INTERNAL_ERROR = pr.INTERNAL_ERROR
VALUE_TYPES = {"ProjectV2ItemFieldTextValue": "text", "ProjectV2ItemFieldNumberValue": "number", "ProjectV2ItemFieldDateValue": "date",
               "ProjectV2ItemFieldSingleSelectValue": "select", "ProjectV2ItemFieldIterationValue": "iteration"}

LIMITATIONS = ("Restored through the API into a NEW private project: workflow rules (the API exposes only name and enabled state and cannot create "
               "them), view grouping and sorting (readable but not writable), project collaborators, original creation dates and authors, "
               "multi-select fields, and items that are not issues of the restored repository or drafts are NOT restored; items are linked "
               "to the restored issues by title.")


class ProjectRestoreError(Exception):
    """Falha de escrita ou de leitura do GraphQL que interrompe a restauração dos Projects."""


def norm(text):
    """Texto com os espaços normalizados, para casar títulos."""
    return " ".join((text or "").split())


# --- planejamento (puro: não escreve nada) -------------------------------------------------

def item_title(item):
    """Título de um item: o conteúdo do rascunho ou o valor do campo Title (vem mesmo com o conteúdo da issue oculto)."""
    content = item.get("content") or {}
    if content.get("__typename") == "DraftIssue" and content.get("title"):
        return content["title"]
    for value in (item.get("fieldValues") or {}).get("nodes", []):
        if value.get("__typename") == "ProjectV2ItemFieldTextValue" and (value.get("field") or {}).get("name") == "Title":
            return value.get("text") or ""
    return content.get("title") or ""


def match_issue(item, source, by_title, by_number):
    """Casa um item de issue com uma issue do backup. Devolve (número antigo, motivo): motivo != None = não casou."""
    content = item.get("content") or {}
    if content.get("number") is not None:
        repo = ((content.get("repository") or {}).get("nameWithOwner") or "").lower()
        if repo and repo != source.lower():
            return None, f"a issue está em {repo}, outro repositório"
        if content["number"] in by_number:
            return content["number"], None
        return None, f"a issue {content['number']} não está no backup"
    title = norm(item_title(item))
    found = by_title.get(title, [])
    if len(found) == 1:
        return found[0], None
    if not found:
        return None, f"nenhuma issue com o título '{title}' no backup"
    return None, f"{len(found)} issues com o título '{title}' no backup (ambíguo)"


def restorable_fields(project):
    """Campos que o restore recria ou reaproveita: os de tipo personalizado (inclui o Status, que é seleção única)."""
    return [f for f in project["fields"]["nodes"] if f.get("dataType") in CUSTOM_TYPES]


def item_values(item, field_names):
    """Valores restauráveis de um item: lista de (campo, tipo, valor...). Só campos restauráveis; o Title fica de fora."""
    out, unsupported = [], []
    for value in (item.get("fieldValues") or {}).get("nodes", []):
        kind = VALUE_TYPES.get(value.get("__typename"))
        name = (value.get("field") or {}).get("name")
        if kind is None or name not in field_names:
            if value.get("__typename") == "ProjectV2ItemFieldMultiSelectValue":
                unsupported.append(name)
            continue
        if kind == "text":
            out.append((name, "text", value.get("text") or ""))
        elif kind == "number":
            out.append((name, "number", value.get("number")))
        elif kind == "date":
            out.append((name, "date", value.get("date")))
        elif kind == "select" and value.get("name") is not None:
            out.append((name, "select", value["name"]))
        elif kind == "iteration":
            out.append((name, "iteration", value.get("title"), value.get("startDate")))
    return out, unsupported


def plan_projects(source, projects, items, issues, issue_map, node_ids):
    """Monta, SEM escrever nada, o que será recriado em cada Project. Devolve a lista de planos (um por Project do backup)."""
    by_title, by_number = {}, {i["number"] for i in issues}
    for i in issues:
        by_title.setdefault(norm(i.get("title")), []).append(i["number"])
    plans = []
    for project in sorted(projects, key=lambda p: p["number"]):
        fields = restorable_fields(project)
        names = {f["name"] for f in fields}
        mine = sorted((i for i in items if i.get("project_number") == project["number"]),
                      key=lambda i: (i.get("position_order") is None, i.get("position_order") or 0, i.get("createdAt") or ""))
        steps, skipped, unsupported, used = [], [], set(), set()
        for item in mine:
            kind, title = item.get("type"), item_title(item)
            values, unsup = item_values(item, names)
            unsupported.update(u for u in unsup if u)
            step = {"title": title, "archived": bool(item.get("isArchived")), "values": values, "position": item.get("position_order")}
            if kind == "DRAFT_ISSUE":
                step.update(kind="draft", body=(item.get("content") or {}).get("body") or "")
            elif kind == "ISSUE":
                old, reason = match_issue(item, source, by_title, by_number)
                new = issue_map.get(old) if old is not None else None
                if reason is None and (new is None or not node_ids.get(old)):
                    reason = f"a issue {old} não foi restaurada"
                if reason is None and old in used:
                    reason = f"a issue {old} já está no Project (outro item com o mesmo título)"
                if reason:
                    skipped.append({"title": title, "reason": reason})
                    continue
                used.add(old)
                step.update(kind="issue", old=old, new=new, node_id=node_ids[old])
            else:
                skipped.append({"title": title, "reason": f"item do tipo {kind} não é restaurado"})
                continue
            steps.append(step)
        multi = [f["name"] for f in project["fields"]["nodes"] if f.get("dataType") == "MULTI_SELECT"]
        views = sorted(project["views"]["nodes"], key=lambda v: v["number"])
        writes = (2 + len(fields) + 1 + sum(1 + bool(v.get("filter")) for v in views) + len(steps)
                  + sum(len(s["values"]) for s in steps) + sum(s["archived"] for s in steps)
                  + len((project.get("statusUpdates") or {}).get("nodes", [])) + bool(project.get("closed")))
        plans.append({"source": project, "title": project["title"] + SUFFIX, "fields": fields, "views": views, "items": steps,
                      "skipped": skipped, "unsupported": sorted(set(multi) | unsupported), "writes": writes,
                      "status_updates": (project.get("statusUpdates") or {}).get("nodes", []),
                      "workflows": [{"name": w["name"], "enabled": w["enabled"]} for w in project["workflows"]["nodes"]]})
    return plans


# --- GraphQL de escrita -------------------------------------------------------------------

class Writer:
    """Cliente GraphQL de ESCRITA (o leitor do backup só faz consultas). Conta as escritas e para na primeira falha."""

    def __init__(self, base, token, delay=0.8, rate_wait=60.0):
        self.base, self.url = base, base.rstrip("/") + "/graphql"
        self.token, self.delay, self.rate_wait, self.writes = token, delay, rate_wait, 0
        self._payload_fields = {}

    def graph(self):
        """Leitor do backup (só consultas), com o mesmo token, para reler o que foi escrito."""
        return pr.Graph(self.base, self.token)

    def _post(self, operation, query, variables):
        body = json.dumps({"query": query, "variables": variables, "operationName": operation}).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "application/vnd.github+json",
                   "User-Agent": "repo-backup-restore"}
        last = None
        for attempt in range(1, 6):
            request = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")[:160]
                if exc.code in (502, 503, 504) and attempt < 5:
                    time.sleep(2 * attempt)
                    continue
                if exc.code in (403, 429) and "rate limit" in text.lower() and attempt < 5:
                    time.sleep(self.rate_wait)
                    continue
                raise ProjectRestoreError(f"[{operation}] HTTP {exc.code}: {text}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = f"falha de rede: {type(exc).__name__}"
                if attempt < 5:
                    time.sleep(2 * attempt)
                    continue
                raise ProjectRestoreError(f"[{operation}] {last}") from None
            errors = payload.get("errors")
            if errors and str(errors[0].get("message", "")).startswith(INTERNAL_ERROR) and attempt < 5:
                time.sleep(2 * attempt)
                continue
            if errors and errors[0].get("type") == "RATE_LIMITED" and attempt < 5:
                time.sleep(self.rate_wait)
                continue
            if errors:
                raise ProjectRestoreError(f"[{operation}] {errors[0].get('type') or 'GRAPHQL_ERROR'}: {errors[0].get('message', '')}")
            return payload["data"]
        raise ProjectRestoreError(f"[{operation}] {last or 'sem resposta'}")

    def read(self, operation, query, variables=None):
        """Consulta (não conta como escrita)."""
        return self._post(operation, query, variables or {})

    def payload_field(self, mutation, candidates):
        """Nome do campo do payload da mutation que devolve o objeto criado, descoberto por introspecção (só leitura)."""
        key = (mutation, tuple(candidates))
        if key not in self._payload_fields:
            type_name = mutation[0].upper() + mutation[1:] + "Payload"
            query = "query PayloadFields($n: String!) { __type(name: $n) { fields { name } } }"
            try:
                names = {f["name"] for f in (self.read("PayloadFields", query, {"n": type_name})["__type"] or {}).get("fields") or []}
            except ProjectRestoreError:
                names = set()
            self._payload_fields[key] = next((c for c in candidates if c in names), candidates[0])
        return self._payload_fields[key]

    def write(self, operation, mutation, data, returns=None):
        """Executa UMA mutation (`<Mutation>Input` é o tipo da entrada). `returns` = (candidatos, seleção) para obter o objeto criado."""
        selection = ""
        if returns:
            field = self.payload_field(mutation, returns[0])
            selection = f"{field} {{ {returns[1]} }}"
        query = (f"mutation {operation}($input: {mutation[0].upper() + mutation[1:]}Input!) "
                 f"{{ {mutation}(input: $input) {{ clientMutationId {selection} }} }}")
        time.sleep(self.delay)
        out = self._post(operation, query, {"input": data})
        self.writes += 1
        return (out[mutation] or {}).get(field) if returns else out[mutation]


# --- execução -----------------------------------------------------------------------------

def read_project(graph, project_id):
    """Relê um Project com o MESMO leitor do backup. Devolve (detalhes, itens em ordem de posição, arquivados incluídos)."""
    details = graph.run_query(pr.Q_DETAILS, {"id": project_id})["node"]
    items, _ = pr.read_items(graph, project_id)
    extras, _, archived = pr.read_optional_parts(graph, {"id": project_id}, details, items)
    everything = sorted(items + archived, key=lambda i: (i.get("position_order") is None, i.get("position_order") or 0))
    return details, everything, extras


def field_index(details):
    """Campos de um Project lido: nome -> {id, tipo, opções por nome}."""
    out = {}
    for f in details["fields"]["nodes"]:
        out[f["name"]] = {"id": f["id"], "type": f.get("dataType"), "options": {o["name"]: o["id"] for o in f.get("options") or []},
                          "iterations": {(it["title"], it["startDate"]): it["id"]
                                         for it in ((f.get("configuration") or {}).get("iterations") or [])
                                         + ((f.get("configuration") or {}).get("completedIterations") or [])}}
    return out


def value_input(index, name, value):
    """Entrada `ProjectV2FieldValue` de um valor do plano, ou (None, motivo) se o destino não tem o campo, a opção ou a iteração."""
    field = index.get(name)
    if field is None:
        return None, f"o campo {name} não existe no Project restaurado"
    kind = value[1]
    if kind == "text":
        return {"text": value[2]}, None
    if kind == "number":
        return ({"number": value[2]}, None) if value[2] is not None else (None, "número vazio")
    if kind == "date":
        return ({"date": value[2]}, None) if value[2] else (None, "data vazia")
    if kind == "select":
        option = field["options"].get(value[2])
        return ({"singleSelectOptionId": option}, None) if option else (None, f"o campo {name} não tem a opção {value[2]}")
    iteration = field["iterations"].get((value[2], value[3]))
    return ({"iterationId": iteration}, None) if iteration else (None, f"o campo {name} não tem a iteração {value[2]}")


def comparable(node):
    """Valor de um nó `fieldValues` do leitor, na forma do plano: (campo, tipo, valor...)."""
    kind = VALUE_TYPES.get(node.get("__typename"))
    name = (node.get("field") or {}).get("name")
    if kind == "text":
        return name, "text", node.get("text") or ""
    if kind == "number":
        return name, "number", node.get("number")
    if kind == "date":
        return name, "date", node.get("date")
    if kind == "select":
        return name, "select", node.get("name")
    if kind == "iteration":
        return name, "iteration", node.get("title"), node.get("startDate")
    return None


def create_fields(writer, plan, project_id, details, notes):
    """Recria os campos que faltam e ajusta as opções do Status. Devolve (índice de campos atualizado, campos que não deu para recriar)."""
    index = field_index(details)
    changed, conflicts = False, set()
    for f in plan["fields"]:
        name, dtype = f["name"], f["dataType"]
        current = index.get(name)
        options = [{"name": o["name"], "color": o.get("color") or "GRAY", "description": o.get("description") or ""} for o in f.get("options") or []]
        if current is None:
            data = {"projectId": project_id, "dataType": dtype, "name": name}
            if dtype == "SINGLE_SELECT":
                data["singleSelectOptions"] = options
            if dtype == "ITERATION":
                cfg = f.get("configuration") or {}
                its = (cfg.get("completedIterations") or []) + (cfg.get("iterations") or [])
                if not its:
                    notes.append(f"campo de iteração {name} sem iterações no backup: não recriado")
                    conflicts.add(name)
                    continue
                data["iterationConfiguration"] = {"duration": cfg.get("duration") or its[0]["duration"], "startDate": min(i["startDate"] for i in its),
                                                  "iterations": [{"title": i["title"], "startDate": i["startDate"], "duration": i["duration"]} for i in its]}
            writer.write("RstCreateField", "createProjectV2Field", data)
            changed = True
        elif current["type"] != dtype:
            notes.append(f"o campo {name} existe no Project restaurado com outro tipo ({current['type']}); não recriado")
            conflicts.add(name)
        elif dtype == "SINGLE_SELECT" and set(current["options"]) != {o["name"] for o in options}:
            writer.write("RstUpdateField", "updateProjectV2Field", {"fieldId": current["id"], "singleSelectOptions": options})
            changed = True
    if changed:
        index = field_index(pr_read_details(writer, project_id))
    return index, conflicts


def pr_read_details(writer, project_id):
    return writer.graph().run_query(pr.Q_DETAILS, {"id": project_id})["node"]


def visible_ids(view, index):
    """Ids (no Project novo) dos campos visíveis de uma view do backup, por nome; None se o backup não os tem."""
    nodes = (((view.get("configuration") or {}).get("visibleFields") or {}).get("nodes")) or ((view.get("fields") or {}).get("nodes"))
    if not nodes:
        return None
    return [index[n["name"]]["id"] for n in nodes if n.get("name") in index]


def create_views(writer, plan, project_id, details, index):
    """Recria as views: a primeira reaproveita a view padrão do Project novo quando o layout bate; as outras são criadas."""
    existing = list(details["views"]["nodes"])
    for n, view in enumerate(plan["views"]):
        ids = visible_ids(view, index)
        target = existing[0] if n == 0 and existing and existing[0]["layout"] == view["layout"] else None
        if target is None:
            data = {"projectId": project_id, "name": view["name"], "layout": view["layout"]}
            if ids:
                data["configuration"] = {"visibleFieldIds": ids}
            created = writer.write("RstCreateView", "createProjectV2View", data, (["projectV2View"], "id"))
            view_id = created["id"]
            if view.get("filter"):
                writer.write("RstUpdateView", "updateProjectV2View", {"viewId": view_id, "filter": view["filter"]})
        else:
            data = {"viewId": target["id"], "name": view["name"], "filter": view.get("filter") or ""}
            if ids:
                data["configuration"] = {"visibleFieldIds": ids}
            writer.write("RstUpdateView", "updateProjectV2View", data)


def verify(graph, rest_get, plan, project_id, expected_items, issue_states, project_meta, conflicts):
    """Relê o Project e compara com o plano. Devolve (divergências, valores a regravar, Project relido)."""
    details, items, _ = read_project(graph, project_id)
    bad, repair = [], []
    for key, want in project_meta.items():
        got = details.get(key)
        same = (got or "") == (want or "") if key in ("title", "shortDescription", "readme") else got == want
        if not same:
            bad.append(f"Project: {key} é {got!r}, esperava {want!r}")
    index = field_index(details)
    for f in plan["fields"]:
        if f["name"] in conflicts:
            continue
        current = index.get(f["name"])
        if current is None or current["type"] != f["dataType"]:
            bad.append(f"campo {f['name']} ausente ou de outro tipo")
        elif f["dataType"] == "SINGLE_SELECT" and set(current["options"]) != {o["name"] for o in f.get("options") or []}:
            bad.append(f"campo {f['name']}: opções diferentes")
    names = {v["name"]: v for v in details["views"]["nodes"]}
    for v in plan["views"]:
        got = names.get(v["name"])
        if got is None or got["layout"] != v["layout"] or (got.get("filter") or "") != (v.get("filter") or ""):
            bad.append(f"view {v['name']} ausente ou diferente (layout ou filtro)")
    if len(items) != len(expected_items):
        bad.append(f"{len(items)} itens no Project restaurado, esperava {len(expected_items)}")
    for n, (step, item_id) in enumerate(expected_items):
        if n >= len(items):
            break
        got = items[n]
        label = f"item {n + 1} ({step['title']})"
        if norm(item_title(got)) != norm(step["title"]):
            bad.append(f"{label}: na posição {n + 1} está '{item_title(got)}'")
            continue
        if bool(got.get("isArchived")) != step["archived"]:
            bad.append(f"{label}: arquivado = {got.get('isArchived')}, esperava {step['archived']}")
        have = {c[:2]: c for c in (comparable(v) for v in (got.get("fieldValues") or {}).get("nodes", [])) if c}
        for want in step["applied"]:
            if have.get(want[:2]) != tuple(want):
                bad.append(f"{label}: {want[0]} é {have.get(want[:2], (None, None, None))[2:]!r}, esperava {want[2:]!r}")
                repair.append((item_id, want))
    for step, _ in expected_items:
        if step["kind"] == "issue":
            want = issue_states[step["new"]]
            status, data = rest_get(step["new"])
            if status != 200 or data.get("state") != want:
                bad.append(f"issue {step['old']} (nova {step['new']}): estado {data.get('state')!r}, esperava {want!r} (um workflow do Project pode tê-la mudado)")
    return bad, repair, details


def restore_one(writer, graph, rest_get, plan, ctx, notes):
    """Cria e confere UM Project. Devolve o resultado dele."""
    source = plan["source"]
    created = writer.write("RstCreateProject", "createProjectV2", {"ownerId": ctx["owner_id"], "title": plan["title"], "repositoryId": ctx["repo_node_id"]},
                           (["projectV2"], "id number url"))
    project_id = created["id"]
    result = {"source_number": source["number"], "target_number": created["number"], "url": created.get("url"), "title": plan["title"]}
    meta = {"title": plan["title"], "shortDescription": source.get("shortDescription"), "readme": source.get("readme"), "public": False}
    writer.write("RstUpdateProject", "updateProjectV2",
                 {"projectId": project_id, "shortDescription": source.get("shortDescription") or "", "readme": source.get("readme") or "", "public": False})
    details = pr_read_details(writer, project_id)
    index, conflicts = create_fields(writer, plan, project_id, details, notes)
    create_views(writer, plan, project_id, details, index)

    expected = []
    for step in plan["items"]:
        if step["kind"] == "issue":
            added = writer.write("RstAddIssueItem", "addProjectV2ItemById", {"projectId": project_id, "contentId": step["node_id"]}, (["item"], "id"))
        else:
            added = writer.write("RstAddDraftItem", "addProjectV2DraftIssue", {"projectId": project_id, "title": step["title"], "body": step["body"]},
                                 (["projectItem", "item"], "id"))
        expected.append((step, added["id"]))
    time.sleep(ctx["settle"])                       # os workflows padrão do Project novo reagem à inclusão (por exemplo, Status = Todo)
    skipped_values = []
    for step, item_id in expected:
        step["applied"] = []
        for value in step["values"]:
            data, reason = value_input(index, value[0], value)
            if data is None:
                skipped_values.append(f"{step['title']}: {reason}")
                continue
            step["applied"].append(value)
            writer.write("RstSetValue", "updateProjectV2ItemFieldValue",
                         {"projectId": project_id, "itemId": item_id, "fieldId": index[value[0]]["id"], "value": data})
    for step, item_id in expected:
        if step["archived"]:
            writer.write("RstArchiveItem", "archiveProjectV2Item", {"projectId": project_id, "itemId": item_id})
    for update in plan["status_updates"]:
        data = {"projectId": project_id, "body": update.get("body") or ""}
        data.update({k: update[k] for k in ("status", "startDate", "targetDate") if update.get(k)})
        writer.write("RstStatusUpdate", "createProjectV2StatusUpdate", data)
    if source.get("closed"):
        writer.write("RstCloseProject", "updateProjectV2", {"projectId": project_id, "closed": True})
        meta["closed"] = True
    else:
        meta["closed"] = False

    issue_states = {s["new"]: ctx["issue_state"][s["old"]] for s, _ in expected if s["kind"] == "issue"}
    attempts, repaired = 0, 0
    for attempts in range(1, ctx["tries"] + 1):
        bad, repair, restored = verify(graph, rest_get, plan, project_id, expected, issue_states, meta, conflicts)
        if not bad:
            break
        if repair and repaired < 2:                 # um workflow pode ter sobrescrito o valor depois da escrita: regrava e confere de novo
            repaired += 1
            for item_id, value in repair:
                data, _ = value_input(index, value[0], value)
                if data is not None:
                    writer.write("RstRepairValue", "updateProjectV2ItemFieldValue",
                                 {"projectId": project_id, "itemId": item_id, "fieldId": index[value[0]]["id"], "value": data})
        if attempts < ctx["tries"]:
            time.sleep(ctx["delay"])
    result["verify_attempts"], result["repair_passes"] = attempts, repaired
    if bad:
        result.update(status="FAILED", mismatches=bad[:10])
        return result
    restored_workflows = {w["name"]: w["enabled"] for w in restored["workflows"]["nodes"]}
    result["workflows"] = [{"name": w["name"], "enabled_in_backup": w["enabled"], "enabled_in_restored": restored_workflows.get(w["name"]),
                            "action": ("nada" if restored_workflows.get(w["name"]) == w["enabled"]
                                       else ("ligar" if w["enabled"] else "desligar") + (" (não existe no Project restaurado)" if w["name"] not in restored_workflows else ""))}
                           for w in plan["workflows"]]
    result.update(items={"expected": len(plan["items"]), "restored": len(expected), "archived": sum(s["archived"] for s, _ in expected),
                         "skipped": plan["skipped"][:20], "skipped_total": len(plan["skipped"])},
                  fields={"restored": [f["name"] for f in plan["fields"]]}, views={"restored": [v["name"] for v in plan["views"]]},
                  status_updates=len(plan["status_updates"]), skipped_values=skipped_values[:10])
    pending = plan["skipped"] or skipped_values or plan["unsupported"]
    result["status"] = "PARTIAL" if pending else "OK"
    if plan["unsupported"]:
        result["unsupported"] = plan["unsupported"]
    return result


def checklist_text(results):
    """Checklist legível dos workflows (e do que não voltou) para o HITL fazer à mão no Project restaurado."""
    lines = ["PROJECTS RESTAURADOS - O QUE FAZER À MÃO", "",
             "A API do GitHub não expõe as regras dos workflows de Project nem permite criá-las ou ligá-las.",
             "Para cada Project restaurado, abra Workflows e confira o estado de cada um contra o backup.", ""]
    for r in results:
        if r.get("status") == "FAILED" or "workflows" not in r:
            continue
        lines.append(f"Project {r['source_number']} -> {r.get('url')}")
        for w in r["workflows"]:
            lines.append(f"  - {w['name']}: no backup {'LIGADO' if w['enabled_in_backup'] else 'DESLIGADO'}; "
                         f"no restaurado {({True: 'LIGADO', False: 'DESLIGADO', None: 'ausente'})[w['enabled_in_restored']]}; ação: {w['action']}")
        lines += ["  - Agrupamento e ordenação das views, colaboradores e as regras (gatilho, filtro, ação) dos workflows não voltam.", ""]
    return "\n".join(lines) + "\n"


def restore_projects(*, source, projects, items, issues, issue_map, node_ids, issue_states, repo_node_id, owner_id, api_base, token,
                     token_scopes, budget, rest_get, evidence_dir, write_delay=0.8, rate_wait=60.0, settle=5.0, tries=4, delay=3.0):
    """Restaura os Projects do backup. Devolve o resultado da classe (status OK, PARTIAL, FAILED ou NOT_VERIFIED)."""
    if token_scopes is not None and "project" not in token_scopes:
        return {"status": "NOT_VERIFIED", "reason": f"o token de escrita não tem o escopo project (escopos: {', '.join(token_scopes) or 'nenhum'})"}
    if not owner_id or not repo_node_id:
        return {"status": "NOT_VERIFIED", "reason": "não foi possível obter o id do dono ou do repositório restaurado"}
    plans = plan_projects(source, projects, items, issues, issue_map, node_ids)
    total = sum(p["writes"] for p in plans)
    if total > budget:
        return {"status": "NOT_VERIFIED", "reason": f"os Projects exigiriam cerca de {total} escritas, acima do que resta do teto ({max(budget, 0)}); nada foi escrito"}
    writer = Writer(api_base, token, write_delay, rate_wait)
    graph = pr.Graph(api_base, token)
    ctx = {"owner_id": owner_id, "repo_node_id": repo_node_id, "settle": settle, "tries": tries, "delay": delay, "issue_state": issue_states}
    results, notes = [], []
    try:
        for plan in plans:
            result = restore_one(writer, graph, rest_get, plan, ctx, notes)
            results.append(result)
            if result["status"] == "FAILED":
                break
    except ProjectRestoreError as exc:
        results.append({"status": "FAILED", "error": str(exc)})
    except pr.GraphqlError as exc:
        results.append({"status": "FAILED", "error": f"leitura de conferência: {exc}"})
    statuses = [r["status"] for r in results]
    status = "FAILED" if "FAILED" in statuses else ("PARTIAL" if "PARTIAL" in statuses or notes else "OK")
    out = {"status": status, "projects": results, "writes": writer.writes, "planned_writes": total, "notes": notes[:10]}
    if status == "FAILED":
        failed = next(r for r in results if r["status"] == "FAILED")
        out["reason"] = failed.get("error") or f"{len(failed.get('mismatches', []))} divergência(s) na leitura de volta do Project {failed.get('source_number')}"
    path = pathlib.Path(evidence_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / "RESTAURAR-PROJETO.txt").write_text(checklist_text(results), encoding="utf-8")
    return out
