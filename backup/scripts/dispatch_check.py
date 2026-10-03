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
    Em GITHUB_OUTPUT, por Mnemonic: `<mnemonic>_matrix` (JSON para strategy.matrix) e
    `<mnemonic>_count`, em minúsculas (hoje bkp_repo_* e bkp_proj_*).

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
# Deve refletir os jobs do dispatcher.yml e o caminho que o registro de artefatos do capabilities.yaml
# declara para o `command_workflow.artifact_id` do Mnemonic (a conferência é feita em validate_lines).
# Para novo Mnemonic: acrescente aqui, um job no dispatcher.yml e o registro no capabilities.yaml.
MNEMONIC_WORKFLOWS = {
    "BKP_REPO": ".github/workflows/backup-repository.yml",
    "BKP_PROJ": ".github/workflows/backup-projects.yml",
    "RST_REPO": ".github/workflows/restore-repository.yml",
}

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
    # Consulta a API do GitHub: 'a que pull requests este commit pertence?'
    repo = os.environ["GITHUB_REPOSITORY"]
    base = os.environ.get("BASE_BRANCH", "main")
    # Requisição autenticada com o GITHUB_TOKEN (permissão pull-requests: read); nenhum valor sensível é impresso.
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
    # Vale como PR legítimo só se foi MERGEADO e o destino do merge foi a branch protegida.
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
    # Código != 0 significa que `before` NÃO é ancestral de `after`: histórico reescrito ou force push.
    anc = subprocess.run(["git", "merge-base", "--is-ancestor", before, after], cwd=ROOT)
    if anc.returncode != 0:
        violation("previous commit is not an ancestor of the new head (force push / history rewrite).")
    # Arquivos alterados em TODO o push (usado adiante para saber se o commands.log mudou).
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
    # Só a linha principal do push: um merge de PR conta como UM commit (não inspecionamos os commits internos do PR).
    for sha in git_text("rev-list", "--first-parent", f"{before}..{after}").splitlines():
        # Arquivos que este commit mudou em relação ao primeiro pai.
        files = set(git_text("diff", "--name-only", f"{sha}^1", sha).splitlines())
        # Arquivos alterados além do commands.log.
        others = files - {LOG}
        # Comando misturado com código no mesmo commit: violação, mesmo que venha de um PR.
        if LOG in files and others:
            violation(f"commit {sha[:12]} mixes {LOG} with other files ({', '.join(sorted(others))}); "
                      f"only {LOG} may be written directly to main, and alone.")
        # Código fora do commands.log só é aceito se vier de um pull request mergeado.
        if others and not commit_has_merged_pr(sha):
            violation(f"commit {sha[:12]} changed files other than {LOG} without a merged pull request "
                      f"({', '.join(sorted(others))}); code changes must go through pull request.")


def new_lines(before, after):
    """Devolve (ids_antigos, linhas_novas) do commands.log, exigindo apenas acréscimo."""
    # Se o arquivo não existia no commit anterior, o log antigo é vazio.
    # O arquivo pode não existir no commit anterior (primeiro uso): nesse caso o log antigo é vazio.
    exists = subprocess.run(["git", "cat-file", "-e", f"{before}:{LOG}"], cwd=ROOT).returncode == 0
    # Conteúdo do log ANTES do push, em bytes (a comparação é exata).
    old = git("show", f"{before}:{LOG}") if exists else b""
    # Conteúdo do log DEPOIS do push.
    new = git("show", f"{after}:{LOG}")

    # Append-only: o conteúdo antigo deve ser prefixo exato do novo.
    # Append-only: o conteúdo antigo precisa ser prefixo exato do novo (senão houve edição ou truncamento).
    if not new.startswith(old):
        violation(f"{LOG} was modified or truncated; the log is append-only.")
    if old and not old.endswith(b"\n"):
        violation(f"{LOG} previous content does not end with a newline; refusing to append to a partial line.")
    # Só o trecho acrescentado: são as linhas NOVAS a validar; as antigas não são revalidadas nem redespachadas.
    added = new[len(old):]
    if not added:
        violation(f"{LOG} changed but has no new lines.")
    if not added.endswith(b"\n"):
        violation("last new line does not end with a newline (incomplete write).")

    # Uma linha JSON por item; o último elemento do split é vazio (o texto termina com \n) e é descartado.
    lines = added.decode("utf-8", errors="strict").split("\n")[:-1]
    if any(not line.strip() for line in lines):
        violation("blank line found among new command lines.")

    # IDs já usados no log anterior (linhas antigas ilegíveis são ignoradas aqui, pois
    # já foram aceitas ou rejeitadas em pushes anteriores).
    # request_ids já usados no log anterior (para recusar reutilização).
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
    # Schema oficial da linha; a mesma definição é revalidada dentro do workflow do Mnemonic.
    schema = load_yaml("backup/schemas/commands-log-line.schema.yaml")
    validator = jsonschema.Draft202012Validator(schema)
    # O registro é lido para conferir a coerência do Mnemonic com o workflow declarado.
    caps = load_yaml("backup/capabilities.yaml")
    # Coerência com o registro: Mnemonic -> workflow declarado em capabilities.yaml.
    # Mapa Mnemonic -> caminho do workflow, como o registro declara. O caminho NÃO fica na
    # capability: vem do registro de artefatos, pelo `command_workflow.artifact_id` (fonte única).
    paths = {a["artifact_id"]: a["location"]["path"] for a in caps["implementation_artifacts"]}
    registry = {c["mnemonic"]: paths.get(c["command_workflow"]["artifact_id"])
                for c in caps["capabilities"] if "mnemonic" in c}

    # seen: ids já vistos (log antigo + linhas deste push); accepted: linhas aprovadas nesta rodada.
    seen, accepted = set(old_ids), []
    # Cada linha nova passa por: JSON, schema, id único, Mnemonic mapeado e coerência com o registro.
    for number, raw in enumerate(lines, start=1):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            violation(f"new line {number} is not valid JSON: {exc.msg}")
        # Erros de schema desta linha.
        errors = sorted(validator.iter_errors(obj), key=str)
        if errors:
            # Mostra o caminho e a regra violada, sem ecoar valores da linha.
            details = "; ".join(f"{'/'.join(str(p) for p in e.absolute_path) or '<line>'}: {e.validator}" for e in errors)
            violation(f"new line {number} violates commands-log-line schema ({details}).")
        # Campos já validados pelo schema (existem e têm o formato certo).
        rid, mnemonic = obj["request_id"], obj["mnemonic"]
        # Autorização não reutilizável: request_id repetido é recusado.
        if rid in seen:
            violation(f"request_id {rid} was already used; authorization is not reusable.")
        seen.add(rid)
        # Mnemonic sem workflow no mapeamento fixo do dispatcher não é despachado.
        if mnemonic not in MNEMONIC_WORKFLOWS:
            violation(f"mnemonic {mnemonic} has no fixed workflow mapping in the dispatcher.")
        # Mapeamento do dispatcher e capabilities.yaml divergem: configuração inconsistente, recusa por segurança.
        if registry.get(mnemonic) != MNEMONIC_WORKFLOWS[mnemonic]:
            violation(f"mnemonic {mnemonic}: dispatcher mapping and capabilities.yaml disagree "
                      f"({MNEMONIC_WORKFLOWS[mnemonic]} vs {registry.get(mnemonic)}).")
        accepted.append(obj)
    return accepted


def matrix_entry(obj):
    """Inputs do workflow de UMA linha aprovada (só os params autorizados; nada é executado como comando).

    `preflight_decision` e `scope_identifiers` vão como TEXTO JSON (string) porque inputs de
    workflow_call só aceitam string/boolean/number; o workflow chamado faz o parse."""
    p = obj["params"]
    entry = {"request_id": obj["request_id"], "destination": p.get("destination", ""),
             "preflight_decision": json.dumps(p["preflight_decision"], sort_keys=True) if "preflight_decision" in p else ""}
    if obj["mnemonic"] == "BKP_REPO":
        entry["source_repository"] = p["source_repository"]
    elif obj["mnemonic"] == "BKP_PROJ":
        entry["scope"] = p["scope"]
        entry["scope_identifiers"] = json.dumps(p["scope_identifiers"], sort_keys=True)
    else:  # RST_REPO: não tem `destination` (o destino da evidência sai do backup_path)
        entry = {"request_id": obj["request_id"], "backup_path": p["backup_path"], "target_name": p.get("target_name", ""),
                 "preflight_decision": entry["preflight_decision"]}
    return entry


def build_matrix(accepted, mnemonic):
    """Monta o `strategy.matrix` (formato {"include": [...]}) das linhas de um Mnemonic."""
    # Uma entrada da matriz por linha aprovada deste Mnemonic.
    return {"include": [matrix_entry(o) for o in accepted if o["mnemonic"] == mnemonic]}


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

    # 1) O intervalo do push é utilizável?
    changed = check_range(before, after)
    # 2) Controle de escrita direta no main (detectivo): só o commands.log pode ser escrito direto.
    check_direct_writes(before, after)

    # 3) Push sem mudança no commands.log (ex.: merge de PR): aceito, mas nada a despachar.
    if LOG not in changed:
        # Push legítimo (ex.: merge de PR) que não mexeu no log: nada a despachar.
        print("commands.log unchanged; nothing to dispatch.")
        for mnemonic in MNEMONIC_WORKFLOWS:
            set_output(f"{mnemonic.lower()}_matrix", json.dumps({"include": []}))
            set_output(f"{mnemonic.lower()}_count", "0")
        return

    # 4) Extrai as linhas NOVAS exigindo apenas acréscimo.
    old_ids, lines = new_lines(before, after)
    # 5) Valida cada linha nova; qualquer falha encerra com saída 30 e nada é despachado.
    accepted = validate_lines(old_ids, lines)
    # 6) Monta a matriz de cada Mnemonic (job `run-<mnemonic>` no dispatcher.yml); um Mnemonic novo precisa de job próprio.
    for mnemonic in MNEMONIC_WORKFLOWS:
        matrix = build_matrix(accepted, mnemonic)
        set_output(f"{mnemonic.lower()}_matrix", json.dumps(matrix, sort_keys=True))
        set_output(f"{mnemonic.lower()}_count", str(len(matrix["include"])))
    # Só IDs no log: o conteúdo das linhas não é ecoado.
    print("DISPATCH_ACCEPTED " + json.dumps({"request_ids": [o["request_id"] for o in accepted]}))


if __name__ == "__main__":
    main()
