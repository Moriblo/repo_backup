#!/usr/bin/env python3
"""Validação de rastreabilidade do registro de artefatos (backup/capabilities.yaml).

O QUE É
    Confere o registro `implementation_artifacts` contra o que existe de fato no
    repositório, e mantém atualizados os SHAs de blob Git registrados. Implementa
    as regras "Integridade referencial", "Detecção de artefato órfão" e "Análise de
    impacto" do protocolo (BACKUP_REPOSITORY.md, seção II.10).

MODOS
    --check   (padrão) Só confere. Sai com 1 se achar qualquer problema.
              É o que o workflow traceability.yml roda em todo pull request.
    --update  Reescreve as linhas `current_sha` dos artefatos MATERIALIZED com
              sha_policy TRACKED, preservando todos os comentários do arquivo.
              Rode localmente depois de alterar qualquer arquivo registrado.

O QUE CONFERE (código de cada problema entre parênteses)
    1. Artefato MATERIALIZED cujo arquivo não existe              (BROKEN_REFERENCE)
    2. Arquivo existente registrado como NOT_MATERIALIZED         (STATE_MISMATCH)
    3. current_sha ausente ou diferente do SHA real do arquivo    (SHA_MISSING, SHA_MISMATCH)
    4. Arquivo em pasta gerenciada sem artifact_id registrado     (ORPHAN_ARTIFACT)
    5. Coerência de caminhos do Mnemonic (SA-04)                  (PATH_MISMATCH)
    6. `implemented_classes` fora de `includes`                   (CLASS_MISMATCH)
    7. VALIDATED sem evidência (run + commit) e sem scope         (VALIDATION_EVIDENCE)
    8. Valores fora dos vocabulários, relações inválidas, ids repetidos (REGISTRY_INVALID)

SAÍDA
    Uma linha `ERRO <CÓDIGO> <sujeito>: <mensagem>` por problema e, no fim, uma linha
    `TRACEABILITY_RESULT {json}`. Código de saída: 0 = ok, 1 = problemas, 64 = uso incorreto.

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - Só lê arquivos, exceto no modo --update, que só altera linhas `current_sha`.
    - Não usa rede nem o comando git: o SHA de blob é calculado como o Git faz.
"""
import hashlib
import importlib.util
import json
import pathlib
import re
import sys

import yaml

# Raiz do repositório (este arquivo fica em backup/scripts/).
ROOT = pathlib.Path(__file__).resolve().parents[2]

# Registro e pastas onde todo arquivo de implementação precisa ter artifact_id.
REGISTRY = "backup/capabilities.yaml"
MANAGED_DIRS = [".github/workflows", "backup/scripts", "backup/schemas"]

# Vocabulários aceitos (espelham `artifact_traceability` no capabilities.yaml).
SHA_POLICIES = {"TRACKED", "SELF_EXEMPT", "MUTABLE_DATA"}
LIFECYCLES = {"ACTIVE", "TEMPORARY", "TO_BE_REMOVED"}
MATERIALIZATIONS = {"MATERIALIZED", "NOT_MATERIALIZED"}
VALIDATIONS = {"VALIDATED", "NOT_VALIDATED", "REVALIDATION_REQUIRED"}
RELATIONSHIPS = {"EXECUTES", "VALIDATES", "SCHEMATIZES", "CONFIGURES", "SUPPORTS"}

# Formato do SHA completo de um commit (40 hexadecimais) e de uma URL de run do Actions.
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/actions/runs/\d+$")


def blob_sha(path):
    """SHA de blob Git de um arquivo: sha1 de "blob <tamanho>\\0" + conteúdo (igual ao `git hash-object`)."""
    data = (ROOT / path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def load_registry():
    """Lê e devolve o capabilities.yaml."""
    with open(ROOT / REGISTRY, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_dispatch_mapping():
    """Devolve o MNEMONIC_WORKFLOWS do dispatch_check.py (a lista de permissão fixa do dispatcher)."""
    spec = importlib.util.spec_from_file_location("dispatch_check", ROOT / "backup/scripts/dispatch_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MNEMONIC_WORKFLOWS


def check(reg):
    """Executa todas as conferências e devolve a lista de problemas (código, sujeito, mensagem)."""
    errors = []

    def err(code, subject, message):
        errors.append((code, subject, message))

    artifacts = reg.get("implementation_artifacts", [])
    capabilities = {c["id"]: c for c in reg.get("capabilities", [])}

    # --- 8. Estrutura: ids únicos --------------------------------------------------
    by_id, registered_paths = {}, set()
    for a in artifacts:
        aid = a.get("artifact_id", "<sem id>")
        if aid in by_id:
            err("REGISTRY_INVALID", aid, "artifact_id repetido")
        by_id[aid] = a
        registered_paths.add(a["location"]["path"])

    for a in artifacts:
        aid = a["artifact_id"]
        path = a["location"]["path"]
        status = a["materialization"]["status"]
        integ = a.get("integrity", {})
        policy = integ.get("sha_policy", "TRACKED")
        exists = (ROOT / path).is_file()

        # --- 8. Vocabulários -------------------------------------------------------
        if status not in MATERIALIZATIONS:
            err("REGISTRY_INVALID", aid, f"materialization.status inválido: {status}")
        if policy not in SHA_POLICIES:
            err("REGISTRY_INVALID", aid, f"sha_policy inválido: {policy}")
        if a.get("lifecycle", "ACTIVE") not in LIFECYCLES:
            err("REGISTRY_INVALID", aid, f"lifecycle inválido: {a.get('lifecycle')}")
        if a["validation"]["status"] not in VALIDATIONS:
            err("REGISTRY_INVALID", aid, f"validation.status inválido: {a['validation']['status']}")
        if not a.get("relationships"):
            err("REGISTRY_INVALID", aid, "artefato sem nenhuma relação com capability")
        for rel in a.get("relationships", []):
            if rel.get("capability_id") not in capabilities:
                err("REGISTRY_INVALID", aid, f"capability inexistente na relação: {rel.get('capability_id')}")
            if rel.get("relationship") not in RELATIONSHIPS:
                err("REGISTRY_INVALID", aid, f"tipo de relação inválido: {rel.get('relationship')}")

        # --- 1 e 2. Existência x estado de materialização --------------------------
        if status == "MATERIALIZED" and not exists:
            err("BROKEN_REFERENCE", aid, f"registrado como MATERIALIZED, mas {path} não existe")
        if status == "NOT_MATERIALIZED" and exists:
            err("STATE_MISMATCH", aid, f"registrado como NOT_MATERIALIZED, mas {path} existe")

        # --- 3. SHA de blob ---------------------------------------------------------
        if status == "MATERIALIZED" and exists and policy == "TRACKED":
            recorded = integ.get("current_sha")
            if not recorded:
                err("SHA_MISSING", aid, f"sem current_sha (rode --update); SHA real de {path}: {blob_sha(path)}")
            elif recorded != blob_sha(path):
                err("SHA_MISMATCH", aid, f"current_sha {recorded[:12]} difere do real {blob_sha(path)[:12]} (rode --update)")

        # --- 7. Regra de VALIDATED (SA-06) -------------------------------------------
        val = a["validation"]
        if val["status"] == "VALIDATED":
            evidence = val.get("evidence") or []
            if not evidence:
                err("VALIDATION_EVIDENCE", aid, "VALIDATED exige validation.evidence com run e commit")
            for ev in evidence:
                if not RUN_RE.match(str(ev.get("run", ""))):
                    err("VALIDATION_EVIDENCE", aid, f"evidence.run fora do formato de URL de run: {ev.get('run')}")
                if not COMMIT_RE.match(str(ev.get("commit", ""))):
                    err("VALIDATION_EVIDENCE", aid, f"evidence.commit deve ser o SHA completo (40 hex): {ev.get('commit')}")
            if not str(val.get("scope", "")).strip():
                err("VALIDATION_EVIDENCE", aid, "VALIDATED exige validation.scope (o que foi e o que não foi provado)")

    # --- 4. Órfãos em pastas gerenciadas ----------------------------------------------
    for d in MANAGED_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            rel = f.relative_to(ROOT).as_posix()
            # __pycache__ e bytecode não são artefatos de implementação.
            if f.is_file() and "__pycache__" not in rel and not rel.endswith(".pyc") and rel not in registered_paths:
                err("ORPHAN_ARTIFACT", rel, "arquivo em pasta gerenciada sem artifact_id registrado")

    # --- 5. Coerência de caminhos do Mnemonic (SA-04) ---------------------------------
    mapping = load_dispatch_mapping()
    dispatcher = yaml.safe_load((ROOT / ".github/workflows/dispatcher.yml").read_text(encoding="utf-8"))
    dispatcher_uses = {j.get("uses") for j in dispatcher.get("jobs", {}).values() if j.get("uses")}
    for cap in capabilities.values():
        mnemonic = cap.get("mnemonic")
        if not mnemonic:
            continue
        cw = cap.get("command_workflow", {})
        art = by_id.get(cw.get("artifact_id"))
        if art is None:
            err("PATH_MISMATCH", cap["id"], f"command_workflow.artifact_id não existe no registro: {cw.get('artifact_id')}")
            continue
        path = art["location"]["path"]
        if "path" in cw:
            err("PATH_MISMATCH", cap["id"], "command_workflow.path não deve existir: o caminho vem do registro de artefatos")
        if mapping.get(mnemonic) != path:
            err("PATH_MISMATCH", mnemonic, f"MNEMONIC_WORKFLOWS ({mapping.get(mnemonic)}) difere do registro ({path})")
        if f"./{path}" not in dispatcher_uses:
            err("PATH_MISMATCH", mnemonic, f"dispatcher.yml não tem um job com `uses: ./{path}`")
        det = cap.get("deterministic_executor", {})
        if det.get("artifact_id") != cw.get("artifact_id"):
            err("PATH_MISMATCH", cap["id"], "deterministic_executor.artifact_id difere de command_workflow.artifact_id")
        if det.get("reference") != f"github_actions:{pathlib.PurePosixPath(path).name}":
            err("PATH_MISMATCH", cap["id"], f"deterministic_executor.reference ({det.get('reference')}) não corresponde ao arquivo {path}")
        for key in ("path", "line_schema"):
            p = cap.get("command_log", {}).get(key)
            if p and p not in registered_paths:
                err("PATH_MISMATCH", cap["id"], f"command_log.{key} ({p}) não está registrado como artefato")
        unknown = set(mapping) - {c.get("mnemonic") for c in capabilities.values()}
        for m in sorted(unknown):
            err("PATH_MISMATCH", m, "Mnemonic do dispatcher sem capability correspondente no registro")

    # --- 6. implemented_classes dentro de includes, e includes dentro de object_classes ---
    for cap in capabilities.values():
        includes = set(cap.get("includes", []))
        implemented = set(cap.get("implemented_classes", []))
        for cls in sorted(implemented - includes):
            err("CLASS_MISMATCH", cap["id"], f"implemented_classes tem '{cls}' que não está em includes")
        for cls in sorted(includes - set(reg.get("object_classes", {}))):
            err("CLASS_MISMATCH", cap["id"], f"includes tem '{cls}' que não existe em object_classes")

    return errors


def update(reg):
    """Reescreve as linhas `current_sha` dos artefatos TRACKED e MATERIALIZED. Devolve quantos mudaram."""
    wanted = {}
    for a in reg["implementation_artifacts"]:
        policy = a.get("integrity", {}).get("sha_policy", "TRACKED")
        path = a["location"]["path"]
        if a["materialization"]["status"] == "MATERIALIZED" and policy == "TRACKED" and (ROOT / path).is_file():
            wanted[a["artifact_id"]] = blob_sha(path)

    lines = (ROOT / REGISTRY).read_text(encoding="utf-8").split("\n")

    # 1ª passada: quais artefatos JÁ têm uma linha `current_sha` (esses só são reescritos;
    # os outros recebem a linha nova, logo depois de `sha_policy`).
    has_sha, current = set(), None
    for line in lines:
        m = re.match(r"^  - artifact_id: (\S+)", line)
        if m:
            current = m.group(1)
        if re.match(r"^      current_sha: ", line):
            has_sha.add(current)

    # 2ª passada: reescreve ou insere.
    out, current, changed = [], None, 0
    for line in lines:
        m = re.match(r"^  - artifact_id: (\S+)", line)
        if m:
            current = m.group(1)
        if current in wanted and re.match(r"^      current_sha: ", line):
            new = f"      current_sha: {wanted[current]}"
            changed += new != line
            line = new
        out.append(line)
        if current in wanted and current not in has_sha and re.match(r"^      sha_policy: TRACKED", line):
            out.append(f"      current_sha: {wanted[current]}")
            changed += 1
    (ROOT / REGISTRY).write_text("\n".join(out), encoding="utf-8")
    return changed


def main():
    """Interpreta os argumentos e executa o modo escolhido."""
    args = sys.argv[1:]
    if args not in ([], ["--check"], ["--update"]):
        print(__doc__)
        sys.exit(64)
    if args == ["--update"]:
        n = update(load_registry())
        print(f"current_sha atualizado em {n} artefato(s).")
    errors = check(load_registry())
    for code, subject, message in errors:
        print(f"ERRO {code} {subject}: {message}")
    print("TRACEABILITY_RESULT " + json.dumps({"ok": not errors, "errors": len(errors)}, sort_keys=True))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
