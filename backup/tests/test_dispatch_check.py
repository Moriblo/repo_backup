#!/usr/bin/env python3
"""Teste local do schema da linha do commands.log e do dispatch_check.py (BKP_REPO, BKP_PROJ, BKP_ISSUES e RST_REPO).

O QUE PROVA
    - o schema aceita linhas válidas dos dois Mnemonics e recusa as inválidas (fail-closed): campo
      desconhecido, params do outro Mnemonic, identificadores errados para o escopo, destino com "..";
    - `preflight_decision` (definição única em $defs) vale nos dois Mnemonics;
    - REGRESSÃO do BKP_REPO: a entrada da matriz continua com os mesmos 4 campos;
    - a matriz do BKP_PROJ traz os identificadores do escopo como texto JSON;
    - cada Mnemonic tem o workflow certo no mapeamento fixo e uma linha repetida é recusada.
    NÃO prova nada contra o GitHub real: isso só um run real do dispatcher prova.

COMO RODAR
    python3 backup/tests/test_dispatch_check.py        (saída 0 = tudo certo)
"""
import contextlib
import io
import json
import pathlib
import sys
import unittest

import jsonschema
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backup" / "scripts"))
import dispatch_check as d  # noqa: E402

SCHEMA = yaml.safe_load((ROOT / "backup/schemas/commands-log-line.schema.yaml").read_text(encoding="utf-8"))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
GO = {"decision": "GO", "authorized_scope": "EXACT_COMMAND_REQUEST", "reusable": False}
DECISION = {"previous_request_id": "req-20261004-001", "hitl_decision": "CONTINUE_WITH_RESTRICTIONS",
            "accepted_restrictions": [{"restriction_id": "RST-projects-ACCESS_PERMISSION_GAP", "object_class": "projects",
                                       "restriction": "projects excluded", "source_gap_classification": "ACCESS_PERMISSION_GAP"}]}


def line(mnemonic, params, rid="req-20261004-002"):
    return {"request_id": rid, "mnemonic": mnemonic, "ts": "2026-10-04T12:00:00Z", "authorization": GO, "params": params}


def repo(**extra):
    return line("BKP_REPO", {"source_repository": "dono/teste", "destination": "backups/teste", **extra})


def proj(scope="PROJECT", ids=None, **extra):
    ids = ids if ids is not None else {"PROJECT": {"owner": "dono", "number": 13}, "OWNER_PROJECT_SET": {"owner": "dono"},
                                       "SOURCE_REPOSITORY": {"source_repository": "dono/teste"}}[scope]
    return line("BKP_PROJ", {"scope": scope, "scope_identifiers": ids, "destination": "backups/projetos", **extra})


def iss(**extra):
    return line("BKP_ISSUES", {"source_repository": "dono/teste", "destination": "backups/teste", **extra})


def rst(**extra):
    return line("RST_REPO", {"backup_path": "backups/teste/req-20260101-001", **extra})


def errors(obj):
    return [e.message for e in VALIDATOR.iter_errors(obj)]


class SchemaTest(unittest.TestCase):
    def test_linhas_validas(self):
        for obj in (repo(), proj("PROJECT"), proj("OWNER_PROJECT_SET"), proj("SOURCE_REPOSITORY"),
                    repo(preflight_decision=DECISION), proj(preflight_decision=DECISION),
                    rst(), rst(target_name="copia-de-teste"), rst(preflight_decision=DECISION),
                    iss(), iss(preflight_decision=DECISION)):
            self.assertEqual(errors(obj), [], obj)

    def test_bkp_proj_identificadores_errados_para_o_escopo(self):
        bad = [proj("PROJECT", {"owner": "dono"}),                                   # falta number
               proj("PROJECT", {"owner": "dono", "number": "13"}),                   # number como texto
               proj("PROJECT", {"owner": "dono", "number": 0}),
               proj("PROJECT", {"owner": "dono", "number": 13, "x": 1}),             # campo a mais
               proj("OWNER_PROJECT_SET", {"owner": "dono", "number": 13}),
               proj("OWNER_PROJECT_SET", {"owner": "../x"}),
               proj("SOURCE_REPOSITORY", {"source_repository": "sem-barra"}),
               proj("SOURCE_REPOSITORY", {"owner": "dono"}),
               proj("TODOS", {"owner": "dono"})]                                     # escopo desconhecido
        for obj in bad:
            self.assertTrue(errors(obj), obj)

    def test_params_do_outro_mnemonic_sao_recusados(self):
        self.assertTrue(errors(line("BKP_REPO", {"scope": "PROJECT", "scope_identifiers": {"owner": "d", "number": 1}, "destination": "b/x"})))
        self.assertTrue(errors(line("BKP_PROJ", {"source_repository": "dono/teste", "destination": "b/x"})))

    def test_bkp_issues_tem_os_params_do_bkp_repo_e_nenhum_outro(self):
        bad = [iss(source_repository="semBarra"), iss(destination="../fora"), iss(destination="/abs"), iss(scope="PROJECT"),
               iss(backup_path="backups/x"), iss(extra_field=1), line("BKP_ISSUES", {"destination": "backups/x"}),
               line("BKP_ISSUES", {"source_repository": "dono/teste"}), line("BKP_ISSUES", {"scope": "PROJECT", "scope_identifiers": {"owner": "d", "number": 1}, "destination": "b/x"})]
        for obj in bad:
            self.assertTrue(errors(obj), obj)
        # E o ramo do BKP_ISSUES não afrouxou o do BKP_REPO nem o do BKP_PROJ.
        self.assertTrue(errors(repo(extra_field=1)))
        self.assertTrue(errors(line("BKP_PROJ", {"source_repository": "dono/teste", "destination": "b/x"})))

    def test_rst_repo_so_aceita_backup_path_e_target_name_validos(self):
        bad = [rst(backup_path="../fora"), rst(backup_path="/abs"), rst(backup_path="a/../b"), rst(target_name="a/b"),
               rst(target_name=".."), rst(target_name="x.git"), rst(target_name=""), rst(destination="backups/x"),
               rst(source_repository="dono/teste"), line("RST_REPO", {}), line("RST_REPO", {"target_name": "x"})]
        for obj in bad:
            self.assertTrue(errors(obj), obj)
        # E os params do RST_REPO não valem para os outros Mnemonics.
        self.assertTrue(errors(line("BKP_REPO", {"backup_path": "backups/x", "destination": "backups/y"})))

    def test_destino_e_campos_desconhecidos(self):
        for dest in ("../fora", "/abs", "a/../b"):
            self.assertTrue(errors(proj("OWNER_PROJECT_SET", destination=dest)), dest)
        self.assertTrue(errors(proj(extra_field=1)))
        self.assertTrue(errors(line("BKP_OUTRO", {"destination": "b/x"})))

    def test_preflight_decision_invalida_nos_dois(self):
        bad = dict(DECISION, hitl_decision="STOP")
        self.assertTrue(errors(repo(preflight_decision=bad)))
        self.assertTrue(errors(proj(preflight_decision=bad)))
        self.assertTrue(errors(proj(preflight_decision={"previous_request_id": "x"})))


class MatrixTest(unittest.TestCase):
    def test_regressao_bkp_repo_mesmos_quatro_campos(self):
        self.assertEqual(d.build_matrix([repo()], "BKP_REPO"),
                         {"include": [{"request_id": "req-20261004-002", "source_repository": "dono/teste",
                                       "destination": "backups/teste", "preflight_decision": ""}]})
        entry = d.build_matrix([repo(preflight_decision=DECISION)], "BKP_REPO")["include"][0]
        self.assertEqual(json.loads(entry["preflight_decision"]), DECISION)

    def test_bkp_proj_identificadores_como_texto_json(self):
        entry = d.build_matrix([proj("PROJECT")], "BKP_PROJ")["include"][0]
        self.assertEqual(set(entry), {"request_id", "scope", "scope_identifiers", "destination", "preflight_decision"})
        self.assertEqual((entry["scope"], json.loads(entry["scope_identifiers"])), ("PROJECT", {"owner": "dono", "number": 13}))
        self.assertIsInstance(entry["scope_identifiers"], str)

    def test_bkp_issues_matriz_igual_a_do_bkp_repo(self):
        entry = d.build_matrix([iss()], "BKP_ISSUES")["include"][0]
        self.assertEqual(entry, {"request_id": "req-20261004-002", "source_repository": "dono/teste",
                                 "destination": "backups/teste", "preflight_decision": ""})
        self.assertEqual(d.build_matrix([iss()], "BKP_REPO"), {"include": []})

    def test_rst_repo_matriz_sem_destination(self):
        entry = d.build_matrix([rst(target_name="copia")], "RST_REPO")["include"][0]
        self.assertEqual(entry, {"request_id": "req-20261004-002", "backup_path": "backups/teste/req-20260101-001",
                                 "target_name": "copia", "preflight_decision": ""})
        self.assertEqual(d.build_matrix([rst()], "RST_REPO")["include"][0]["target_name"], "")
        with_decision = d.build_matrix([rst(preflight_decision=DECISION)], "RST_REPO")["include"][0]
        self.assertEqual(json.loads(with_decision["preflight_decision"]), DECISION)

    def test_cada_mnemonic_so_ve_as_proprias_linhas(self):
        accepted = [repo(), proj("OWNER_PROJECT_SET"), rst(), iss()]
        for m in ("BKP_REPO", "BKP_PROJ", "RST_REPO", "BKP_ISSUES"):
            self.assertEqual(len(d.build_matrix(accepted, m)["include"]), 1, m)


class MappingTest(unittest.TestCase):
    def test_mapeamento_fixo(self):
        self.assertEqual(d.MNEMONIC_WORKFLOWS, {"BKP_REPO": ".github/workflows/backup-repository.yml",
                                                "BKP_PROJ": ".github/workflows/backup-projects.yml",
                                                "BKP_ISSUES": ".github/workflows/backup-issues.yml",
                                                "RST_REPO": ".github/workflows/restore-repository.yml"})
        for path in d.MNEMONIC_WORKFLOWS.values():
            self.assertTrue((ROOT / path).exists(), path)

    def test_validate_lines_aceita_os_dois_e_recusa_id_repetido(self):
        a, b = repo(), proj()
        a["request_id"], b["request_id"] = "req-20261004-010", "req-20261004-011"
        self.assertEqual(len(d.validate_lines(set(), [json.dumps(a), json.dumps(b)])), 2)
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            d.validate_lines({"req-20261004-011"}, [json.dumps(b)])
        self.assertEqual(cm.exception.code, d.VIOLATION)

    def test_validate_lines_recusa_linha_fora_do_schema(self):
        bad = proj("PROJECT", {"owner": "dono"})
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            d.validate_lines(set(), [json.dumps(bad)])
        self.assertEqual(cm.exception.code, d.VIOLATION)

    def test_dispatcher_tem_job_e_saidas_de_cada_mnemonic(self):
        wf = yaml.safe_load((ROOT / ".github/workflows/dispatcher.yml").read_text(encoding="utf-8"))
        uses = {j.get("uses") for j in wf["jobs"].values() if j.get("uses")}
        self.assertEqual(uses, {"./" + p for p in d.MNEMONIC_WORKFLOWS.values()})
        for m in d.MNEMONIC_WORKFLOWS:
            self.assertIn(f"{m.lower()}_matrix", wf["jobs"]["guard"]["outputs"])
            self.assertIn(f"{m.lower()}_count", wf["jobs"]["guard"]["outputs"])

    def test_inputs_do_workflow_batem_com_a_matriz(self):
        # O `with:` de cada job do dispatcher só usa campos que a matriz do Mnemonic produz,
        # e cobre todos os inputs obrigatórios do workflow chamado.
        wf = yaml.safe_load((ROOT / ".github/workflows/dispatcher.yml").read_text(encoding="utf-8"))
        for m, path in d.MNEMONIC_WORKFLOWS.items():
            job = wf["jobs"][f"run-{m.lower().replace('_', '-')}"]
            called = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))[True]["workflow_call"]["inputs"]
            self.assertEqual(set(job["with"]), set(called))
            sample = {"BKP_REPO": repo(), "BKP_PROJ": proj("PROJECT"), "RST_REPO": rst(), "BKP_ISSUES": iss()}[m]
            self.assertEqual(set(d.matrix_entry(sample)), set(called))


if __name__ == "__main__":
    unittest.main()
