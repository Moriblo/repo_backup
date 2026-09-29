#!/usr/bin/env python3
"""Verificações do dispatcher.yml sobre um push no main.

O QUE É
    Script chamado pelo job `guard` do dispatcher.yml. Recebe o intervalo do push
    (commit anterior -> commit novo) e decide, de forma fail-closed, se o push é
    legítimo e quais linhas novas do commands.log devem ser despachadas.

ONDE SE ENCAIXA NO FLUXO
    Passo 5 do "Governed command flow" (BACKUP_REPOSITORY.md): o dispatcher é
    disparado pelo push no commands.log, lê só as linhas novas, valida e chama o
    workflow do Mnemonic por mapeamento fixo.

O QUE VERIFICA (nesta ordem)
    1. Intervalo do push é sensato (commit anterior existe e é ancestral do novo).
    2. Controle de escrita no main (detectivo): cada commit da linha principal que
       toca arquivos ALÉM do commands.log precisa vir de um pull request mergeado.
       Escrita direta só é aceita no commands.log, e sozinho no commit.
    3. Se o commands.log mudou: a mudança é somente ACRÉSCIMO (append-only).
    4. Cada linha nova é JSON válido e cumpre commands-log-line.schema.yaml.
    5. request_id é único (contra o log anterior e dentro do próprio push).
    6. O Mnemonic da linha está no mapeamento fixo E coerente com capabilities.yaml.

SAÍDAS
    Código 0  : push aceito (pode não haver nada a despachar).
    Código 30 : violação; NADA é despachado (fail-closed, o push inteiro é recusado).
    Código 64 : uso incorreto / variável de ambiente ausente.
    Em GITHUB_OUTPUT: `bkp_repo_matrix` (JSON para strategy.matrix) e `bkp_repo_count`.

VARIÁVEIS DE AMBIENTE
    BEFORE, AFTER        commit anterior e novo do push (github.event.before / github.sha)
    GITHUB_REPOSITORY    dono/nome, para consultar a API de pull requests
    GH_TOKEN             token do GITHUB_TOKEN (permissão pull-requests: read)
    BASE_BRANCH          branch protegida (padrão: main)

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - Nunca despacha uma linha que não passou em TODAS as verificações.
    - Nunca altera nada: só lê o repositório e a API.
    - Não imprime o conteúdo das linhas (podem trazer textos de restrições); só IDs.
"""
import json
import os
import pathlib
import subprocess
import sys
import urllib.request

import jsonschema
import yaml

# Raiz do repositório (este arquivo fica em backup/scripts/).
ROOT = pathlib.Path(__file__).resolve().parents[2]

# Arquivo do log de comandos, relativo à raiz.
LOG = "commands.log"

# Commit "zero" que o GitHub envia em `before` quando o push cria a branch.
ZEROS = "0" * 40

# Mapeamento FIXO Mnemonic -> workflow. É a única forma de um comando executar algo.
# Deve refletir os jobs do dispatcher.yml e o `command_workflow.path` do capabilities.yaml.
# Para novo Mnemonic: acrescente aqui, um job no dispatcher.yml e o registro no capabilities.yaml.
MNEMONIC_WORKFLOWS = {"BKP_REPO": ".github/workflows/backup-repository.yml"}

# Código de saída para violações (distinto de erros de uso).
VIOLATION = 30


def violation(message):
    """Registra uma violação como erro do Actions e encerra com saída 30 (fail-closed)."""
    print(f"::error::{message}")
    sys.exit(VIOLATION)


def git(*args):
    """Executa `git` e devolve a saída em bytes. Falha do git vira violação (não ignoramos)."""
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if proc.returncode != 0:
        violation(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def git_text(*args):
    """Como `git`, mas devolve texto sem espaços nas pontas."""
    return git(*args).decode().strip()


def commit_has_merged_pr(sha):
    """True se o commit pertence a um pull request mergeado na branch base.

    Usa GET /repos/{repo}/commits/{sha}/pulls. Isolada em uma função para poder
    ser substituída nos testes locais (não há API do GitHub fora do Actions).
    """
    repo = os.environ["GITHUB_REPOSITORY"]
    base = os.environ.get("BASE_BRANCH", "main")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/commits/{sha}/pulls",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        pulls = json.load(resp)
    return any(p.get("merged_at") and p["base"]["ref"] == base for p in pulls)


def load_yaml(path):
    """Lê um YAML relativo à raiz do repositório."""
    with open(ROOT / path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_range(before, after):
    """Verifica que o intervalo do push é utilizável. Devolve a lista de arquivos alterados."""
    if before == ZEROS:
        # Sem commit anterior não há como saber o que é "novo": recusa em vez de adivinhar.
        violation("push without a previous commit (branch creation); cannot determine new command lines.")
    # `merge-base --is-ancestor` devolve código != 0 se NÃO for ancestral (ex.: force push).
    anc = subprocess.run(["git", "merge-base", "--is-ancestor", before, after], cwd=ROOT)
    if anc.returncode != 0:
        violation("previous commit is not an ancestor of the new head (force push / history rewrite).")
    changed = git_text("diff", "--name-only", before, after).splitlines()
    return changed


def check_direct_writes(before, after):
    """Controle de escrita no main: só o commands.log pode ser escrito direto.

    Percorre os commits da LINHA PRINCIPAL do push (um merge de PR conta como um
    commit). Para cada um, compara com o primeiro pai:
      - só commands.log alterado          -> escrita de comando (verificada adiante)
      - commands.log + outros arquivos    -> violação (mistura comando com código)
      - só outros arquivos                -> exige pull request mergeado
    """
    for sha in git_text("rev-list", "--first-parent", f"{before}..{after}").splitlines():
        files = set(git_text("diff", "--name-only", f"{sha}^1", sha).splitlines())
        others = files - {LOG}
        if LOG in files and others:
            violation(f"commit {sha[:12]} mixes {LOG} with other files ({', '.join(sorted(others))}); "
                      f"only {LOG} may be written directly to main, and alone.")
        if others and not commit_has_merged_pr(sha):
            violation(f"commit {sha[:12]} changed files other than {LOG} without a merged pull request "
                      f"({', '.join(sorted(others))}); code changes must go through pull request.")


def new_lines(before, after):
    """Devolve (ids_antigos, linhas_novas) do commands.log, exigindo apenas acréscimo."""
    # Se o arquivo não existia no commit anterior, o log antigo é vazio.
    exists = subprocess.run(["git", "cat-file", "-e", f"{before}:{LOG}"], cwd=ROOT).returncode == 0
    old = git("show", f"{before}:{LOG}") if exists else b""
    new = git("show", f"{after}:{LOG}")

    # Append-only: o conteúdo antigo deve ser prefixo exato do novo.
    if not new.startswith(old):
        violation(f"{LOG} was modified or truncated; the log is append-only.")
    if old and not old.endswith(b"\n"):
        violation(f"{LOG} previous content does not end with a newline; refusing to append to a partial line.")
    added = new[len(old):]
    if not added:
        violation(f"{LOG} changed but has no new lines.")
    if not added.endswith(b"\n"):
        violation("last new line does not end with a newline (incomplete write).")

    lines = added.decode("utf-8", errors="strict").split("\n")[:-1]
    if any(not line.strip() for line in lines):
        violation("blank line found among new command lines.")

    # IDs já usados no log anterior (linhas antigas ilegíveis são ignoradas aqui, pois
    # já foram aceitas ou rejeitadas em pushes anteriores).
    old_ids = set()
    for raw in old.decode("utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "request_id" in obj:
                old_ids.add(obj["request_id"])
        except json.JSONDecodeError:
            pass
    return old_ids, lines


def validate_lines(old_ids, lines):
    """Valida cada linha nova (JSON, schema, id único, Mnemonic mapeado) e as devolve como objetos."""
    schema = load_yaml("backup/schemas/commands-log-line.schema.yaml")
    validator = jsonschema.Draft202012Validator(schema)
    caps = load_yaml("backup/capabilities.yaml")
    # Coerência com o registro: Mnemonic -> workflow declarado em capabilities.yaml.
    registry = {c["mnemonic"]: c["command_workflow"]["path"] for c in caps["capabilities"] if "mnemonic" in c}

    seen, accepted = set(old_ids), []
    for number, raw in enumerate(lines, start=1):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            violation(f"new line {number} is not valid JSON: {exc.msg}")
        errors = sorted(validator.iter_errors(obj), key=str)
        if errors:
            # Mostra o caminho e a regra violada, sem ecoar valores da linha.
            details = "; ".join(f"{'/'.join(str(p) for p in e.absolute_path) or '<line>'}: {e.validator}" for e in errors)
            violation(f"new line {number} violates commands-log-line schema ({details}).")
        rid, mnemonic = obj["request_id"], obj["mnemonic"]
        if rid in seen:
            violation(f"request_id {rid} was already used; authorization is not reusable.")
        seen.add(rid)
        if mnemonic not in MNEMONIC_WORKFLOWS:
            violation(f"mnemonic {mnemonic} has no fixed workflow mapping in the dispatcher.")
        if registry.get(mnemonic) != MNEMONIC_WORKFLOWS[mnemonic]:
            violation(f"mnemonic {mnemonic}: dispatcher mapping and capabilities.yaml disagree "
                      f"({MNEMONIC_WORKFLOWS[mnemonic]} vs {registry.get(mnemonic)}).")
        accepted.append(obj)
    return accepted


def build_matrix(accepted, mnemonic):
    """Monta o `strategy.matrix` (formato {"include": [...]}) das linhas de um Mnemonic.

    `preflight_decision` vai como TEXTO JSON (string) porque inputs de workflow_call
    só aceitam string/boolean/number; o workflow chamado faz o parse.
    """
    include = []
    for obj in accepted:
        if obj["mnemonic"] != mnemonic:
            continue
        p = obj["params"]
        include.append({
            "request_id": obj["request_id"],
            "source_repository": p["source_repository"],
            "destination": p["destination"],
            "preflight_decision": json.dumps(p["preflight_decision"], sort_keys=True) if "preflight_decision" in p else "",
        })
    return {"include": include}


def set_output(key, value):
    """Grava `key=value` em $GITHUB_OUTPUT (ou imprime, se rodando fora do Actions)."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"[output] {key}={value}")


def main():
    """Executa as verificações em ordem e publica a matriz de despacho."""
    try:
        before, after = os.environ["BEFORE"], os.environ["AFTER"]
    except KeyError as exc:
        print(f"missing environment variable: {exc}")
        sys.exit(64)

    changed = check_range(before, after)
    check_direct_writes(before, after)

    if LOG not in changed:
        # Push legítimo (ex.: merge de PR) que não mexeu no log: nada a despachar.
        print("commands.log unchanged; nothing to dispatch.")
        set_output("bkp_repo_matrix", json.dumps({"include": []}))
        set_output("bkp_repo_count", "0")
        return

    old_ids, lines = new_lines(before, after)
    accepted = validate_lines(old_ids, lines)
    matrix = build_matrix(accepted, "BKP_REPO")
    set_output("bkp_repo_matrix", json.dumps(matrix, sort_keys=True))
    set_output("bkp_repo_count", str(len(matrix["include"])))
    # Só IDs no log: o conteúdo das linhas não é ecoado.
    print("DISPATCH_ACCEPTED " + json.dumps({"request_ids": [o["request_id"] for o in accepted]}))


if __name__ == "__main__":
    main()
