#!/usr/bin/env bash
# =============================================================================
# montar_repo_teste_issues.sh - cria um repositório de TESTE com issues e um quadro
# =============================================================================
# O QUE É
#   Cria, na SUA conta do GitHub, um repositório privado de teste com conteúdo de
#   issues e de Projects (quadro) conhecido e sempre igual. Serve para desenvolver e
#   provar o backup de issues, labels, milestones e projects SEM mexer em dados reais.
#   Usa o programa GitHub CLI (`gh`), que usa o seu login do GitHub: nenhum token é
#   digitado, colado ou gravado por este script.
#
# O QUE O SCRIPT CRIA (o backup deverá preservar tudo isto)
#   Repositório   <seu-usuario>/<nome informado> (privado), com um README.
#   Etiquetas     3 etiquetas novas: teste-backup, prioridade-alta, documentacao-teste.
#   Marco         1 marco (milestone): "Marco de teste".
#   Issues        6 issues, com texto com acentuação, bloco de código e lista; 2 delas com
#                 etiquetas e marco; 1 editada depois de criada; 1 fechada com comentário.
#   Comentários   5 comentários no total, espalhados pelas issues.
#   Quadro        1 Project (quadro) do usuário, ligado ao repositório, com:
#                 - o campo padrão "Status" (Todo / In Progress / Done), preenchido nos itens;
#                 - um campo novo "Prioridade" (Alta / Média / Baixa), preenchido;
#                 - 6 itens que são as issues e 1 item de rascunho (sem issue).
#
# COMO USAR (no Git Bash do Windows, ou em qualquer bash com o gh instalado)
#   1. Instale o gh e faça o login:   gh auth login
#      Libere a permissão de quadros: gh auth refresh -s project
#   2. Rode:  bash backup/scripts/montar_repo_teste_issues.sh backup_teste_issues
#      (o argumento é só o NOME do repositório novo; ele é criado na sua conta)
#
# SEGURANÇA
#   - RECUSA continuar se o repositório já existir (nunca escreve em um existente).
#   - Só cria conteúdo de teste sem significado; nenhum dado real é lido ou copiado.
#   - Não grava segredo em lugar nenhum.
#   - Para APAGAR depois: gh repo delete <usuario>/<nome> --yes, e apague o quadro em
#     github.com > seu perfil > Projects (o gh project delete também serve).
#
# CÓDIGOS DE SAÍDA
#   0 criado   1 falha (mensagem na tela)   64 uso incorreto
# =============================================================================
set -euo pipefail

# ---- 1. Argumento: nome do repositório novo --------------------------------------------
if [[ $# -ne 1 || "$1" == */* ]]; then
  echo "Uso: bash montar_repo_teste_issues.sh <NOME do repositório novo, sem o dono; ex.: backup_teste_issues>"
  exit 64
fi
NOME="$1"

# ---- 2. Pré-requisitos: gh instalado e logado, com permissão de quadros ------------------
command -v gh >/dev/null 2>&1 || { echo "Falta o GitHub CLI (gh). Instale: winget install --id GitHub.cli"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Você não está logado no gh. Rode: gh auth login"; exit 1; }
# `gh project list` falha se a permissão "project" não foi liberada.
gh project list --owner "@me" >/dev/null 2>&1 || { echo "Falta a permissão de quadros. Rode: gh auth refresh -s project"; exit 1; }

DONO="$(gh api user --jq .login)"
REPO="$DONO/$NOME"

# ---- 3. O repositório NÃO pode existir ----------------------------------------------------
if gh repo view "$REPO" >/dev/null 2>&1; then
  echo "O repositório $REPO JÁ existe. Escolha outro nome (o script só cria repositórios novos)."
  exit 1
fi

echo "Criando o repositório de teste $REPO (privado)..."
gh repo create "$REPO" --private --add-readme \
  --description "Repositório de TESTE do backup de issues e projects (pode apagar)." >/dev/null

# ---- 4. Etiquetas e marco ------------------------------------------------------------------
echo "Criando etiquetas e marco..."
gh label create "teste-backup" --repo "$REPO" --color "0E8A16" --description "Etiqueta de teste do backup" >/dev/null
gh label create "prioridade-alta" --repo "$REPO" --color "B60205" --description "Prioridade alta (teste)" >/dev/null
gh label create "documentacao-teste" --repo "$REPO" --color "1D76DB" --description "Documentação (teste)" >/dev/null
gh api "repos/$REPO/milestones" -f title="Marco de teste" \
  -f description="Marco usado para testar o backup de milestones." >/dev/null

# Função auxiliar: cria uma issue e devolve o NÚMERO (a última parte da URL que o gh imprime).
criar_issue() {            # criar_issue "titulo" "corpo" [argumentos extras do gh]
  local titulo="$1" corpo="$2"; shift 2
  local url
  url="$(gh issue create --repo "$REPO" --title "$titulo" --body "$corpo" "$@")"
  echo "${url##*/}"
}

# ---- 5. Issues e comentários ---------------------------------------------------------------
echo "Criando issues e comentários..."
N1="$(criar_issue "Erro ao salvar a configuração" $'## Descrição\n\nAo salvar a configuração aparece uma mensagem de erro com acentuação: **não foi possível gravar**.\n\n```bash\ngit status --short\n```\n\n- passo 1: abrir a tela\n- passo 2: salvar\n' --label "bug,teste-backup" --milestone "Marco de teste")"
N2="$(criar_issue "Melhorar a documentação inicial" $'Texto curto de **documentação**.\n\n1. revisar o README\n2. incluir exemplos\n' --label "documentacao-teste")"
N3="$(criar_issue "Atender chamado urgente" $'Chamado de prioridade alta: _ação imediata_.\n' --label "prioridade-alta,teste-backup" --milestone "Marco de teste")"
N4="$(criar_issue "Ideia: exportar relatório" $'Ideia sem etiqueta e sem marco. Inclui emoji: ✔ e símbolo © para testar caracteres especiais.\n')"
N5="$(criar_issue "Revisar a política de senhas" $'Issue **que será editada** depois de criada.\n')"
N6="$(criar_issue "Tarefa já concluída" $'Esta issue será **fechada** com um comentário.\n')"

gh issue comment "$N1" --repo "$REPO" --body "Primeiro comentário na issue 1, com acentuação: ação, coração, órgão." >/dev/null
gh issue comment "$N1" --repo "$REPO" --body $'Segundo comentário na issue 1.\n\n> citação em bloco' >/dev/null
gh issue comment "$N2" --repo "$REPO" --body "Comentário na issue 2." >/dev/null
gh issue comment "$N3" --repo "$REPO" --body "Comentário na issue 3: atendido hoje." >/dev/null
gh issue edit "$N5" --repo "$REPO" --body $'Issue editada depois de criada (versão 2 do texto).\n' >/dev/null
gh issue close "$N6" --repo "$REPO" --reason completed --comment "Concluída: fechando esta issue de teste." >/dev/null

# ---- 6. Quadro (Project v2) -----------------------------------------------------------------
echo "Criando o quadro e os itens..."
PROJ="$(gh project create --owner "$DONO" --title "Quadro de teste ($NOME)" --format json --jq .number)"
gh project link "$PROJ" --owner "$DONO" --repo "$NOME" >/dev/null

# Campo novo de seleção única: Prioridade.
gh project field-create "$PROJ" --owner "$DONO" --name "Prioridade" \
  --data-type SINGLE_SELECT --single-select-options "Alta,Média,Baixa" >/dev/null

# Identificadores internos do quadro, do campo Status e do campo Prioridade, lidos do próprio gh
# (o filtro --jq é do gh: o Git for Windows NÃO traz o programa jq, então não dependemos dele).
PROJ_ID="$(gh project view "$PROJ" --owner "$DONO" --format json --jq .id)"
campo_id() {               # campo_id <nome do campo>
  gh project field-list "$PROJ" --owner "$DONO" --format json --jq ".fields[] | select(.name==\"$1\") | .id"
}
opcao_id() {               # opcao_id <nome do campo> <nome da opção>
  gh project field-list "$PROJ" --owner "$DONO" --format json \
    --jq ".fields[] | select(.name==\"$1\") | .options[] | select(.name==\"$2\") | .id"
}
STATUS_ID="$(campo_id Status)"
PRIO_ID="$(campo_id Prioridade)"

# Função auxiliar: define o valor de um campo de seleção única em um item do quadro.
definir() {                # definir <id do item> <id do campo> <id da opção>
  gh project item-edit --id "$1" --project-id "$PROJ_ID" --field-id "$2" --single-select-option-id "$3" >/dev/null
}

# Adiciona as 6 issues como itens e preenche Status e Prioridade.
adicionar() {              # adicionar <número da issue> <status> <prioridade>
  local item
  item="$(gh project item-add "$PROJ" --owner "$DONO" --url "https://github.com/$REPO/issues/$1" --format json --jq .id)"
  definir "$item" "$STATUS_ID" "$(opcao_id Status "$2")"
  definir "$item" "$PRIO_ID"   "$(opcao_id Prioridade "$3")"
}
adicionar "$N1" "In Progress" "Alta"
adicionar "$N2" "Todo"        "Baixa"
adicionar "$N3" "In Progress" "Alta"
adicionar "$N4" "Todo"        "Média"
adicionar "$N5" "Todo"        "Média"
adicionar "$N6" "Done"        "Baixa"

# Item de rascunho (draft): existe só no quadro, sem issue.
RASC="$(gh project item-create "$PROJ" --owner "$DONO" --title "Rascunho sem issue" --body "Item de rascunho do quadro de teste." --format json --jq .id)"
definir "$RASC" "$STATUS_ID" "$(opcao_id Status "Todo")"

# ---- 7. Resumo para conferência --------------------------------------------------------------
echo
echo "=========================================================================="
echo "Repositório de teste criado: https://github.com/$REPO"
echo "Quadro: https://github.com/users/$DONO/projects/$PROJ"
echo "Esperado no backup:"
echo "  6 issues (5 abertas, 1 fechada; a de número $N5 foi editada)"
echo "  5 comentários, 3 etiquetas novas, 1 marco (\"Marco de teste\")"
echo "  Quadro número $PROJ: 7 itens (6 issues + 1 rascunho), campos Status e Prioridade"
echo "Para apagar depois: gh repo delete $REPO --yes   e   gh project delete $PROJ --owner $DONO"
echo "=========================================================================="
