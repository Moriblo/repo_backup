#!/usr/bin/env bash
# =============================================================================
# montar_repo_teste.sh - monta e envia um repositório de TESTE para o backup
# =============================================================================
# O QUE É
#   Cria, em uma pasta temporária do SEU computador, um repositório git com os casos
#   que o backup ainda não provou em execução real, e o envia para um repositório
#   VAZIO que você criou no GitHub. Depois você pede um backup desse repositório pelo
#   fluxo normal (menu, ECR, GO) e confere o resultado.
#
# O QUE O REPOSITÓRIO DE TESTE CONTÉM (e o que cada item testa no backup)
#   arquivo-grande.dat       ~25 MiB, arquivo comum  -> envio ao OneDrive EM BLOCOS (acima de
#                            4 MiB o envio deixa de ser simples)
#   lfs-6mb.bin              ~6 MiB, Git LFS         -> detecção/verificação do LFS e o
#                            lfs-objects.tar do pacote
#   modulos/exemplo          submódulo (gitlink)     -> detecção de submódulos
#                            + .gitmodules
#   vazio.txt                arquivo de 0 bytes      -> envio de arquivo vazio
#   acentuação/ação.txt      nomes com acento        -> nomes não ASCII no pacote e no zip
#   branches: main e feature/exemplo; tags: v1 (leve) e v2 (anotada)
#
# COMO USAR (no Git Bash do Windows, ou em qualquer bash)
#   1. No GitHub, crie um repositório VAZIO e PRIVADO (sem README, licença ou .gitignore),
#      por exemplo Moriblo/backup_teste.
#   2. Rode:  bash backup/scripts/montar_repo_teste.sh https://github.com/Moriblo/backup_teste.git
#   3. Se o git pedir login, use o seu login do GitHub (ou o token pessoal como senha).
#      NUNCA cole o token em chat, arquivo ou log.
#
# SEGURANÇA
#   - O script RECUSA enviar se o repositório de destino já tiver qualquer branch ou tag
#     (só aceita repositório vazio).
#   - Não altera a configuração global do git: usa só configuração local da pasta temporária.
#   - Não grava segredo em lugar nenhum. Os arquivos são dados aleatórios sem significado.
#
# CÓDIGOS DE SAÍDA
#   0 enviado   1 falha (mensagem na tela)   64 uso incorreto
# =============================================================================
set -euo pipefail

# ---- 1. Argumento: URL do repositório vazio ---------------------------------------------
if [[ $# -ne 1 ]]; then
  echo "Uso: bash montar_repo_teste.sh <URL do repositório VAZIO, ex.: https://github.com/Moriblo/backup_teste.git>"
  exit 64
fi
DESTINO="$1"

# ---- 2. Ferramentas necessárias ---------------------------------------------------------
for ferramenta in git head mktemp; do
  command -v "$ferramenta" >/dev/null 2>&1 || { echo "Falta a ferramenta: $ferramenta"; exit 1; }
done
git lfs version >/dev/null 2>&1 || { echo "Falta o git-lfs (o Git for Windows já o traz; no Linux: instale o pacote git-lfs)."; exit 1; }

# ---- 3. O destino precisa estar VAZIO ---------------------------------------------------
# `git ls-remote` lista branches e tags do destino; saída vazia = repositório vazio.
if [[ -n "$(git ls-remote "$DESTINO" 2>/dev/null)" ]]; then
  echo "O repositório de destino NÃO está vazio. Crie um repositório novo e vazio e tente de novo."
  exit 1
fi

# ---- 4. Pasta temporária e identidade local ---------------------------------------------
PASTA="$(mktemp -d)"
echo "Montando o repositório de teste em: $PASTA"
cd "$PASTA"
git init -q -b main
# Identidade só para estes commits de teste (configuração local, não global).
git config user.name "Teste Backup"
git config user.email "teste-backup@example.invalid"

# ---- 5. Git LFS: todo arquivo *.bin vai para o LFS --------------------------------------
git lfs install --local >/dev/null
git lfs track "*.bin" >/dev/null     # grava a regra em .gitattributes

# ---- 6. Conteúdo do commit 1 ------------------------------------------------------------
cat > README.md <<'EOF'
# Repositório de teste do backup

Conteúdo sem significado, criado por montar_repo_teste.sh para testar o backup:
arquivo grande, Git LFS, submódulo, arquivo vazio, nomes com acento, branches e tags.
EOF
head -c $((25 * 1024 * 1024)) /dev/urandom > arquivo-grande.dat   # ~25 MiB: força envio em blocos
head -c $((6 * 1024 * 1024)) /dev/urandom > lfs-6mb.bin           # ~6 MiB: vai para o Git LFS
: > vazio.txt                                                      # arquivo de 0 bytes
mkdir -p "acentuação"
echo "conteúdo com acentuação" > "acentuação/ação.txt"
git add .gitattributes README.md arquivo-grande.dat lfs-6mb.bin vazio.txt "acentuação"
git commit -q -m "Commit inicial: arquivo grande, LFS, arquivo vazio e nomes com acento"

# ---- 7. Submódulo (gitlink) sem depender de outro repositório ---------------------------
# Um submódulo é uma entrada de modo 160000 apontando para um commit de OUTRO repositório,
# mais o arquivo .gitmodules com a URL. Aqui criamos a entrada diretamente (sem clonar nada):
# é exatamente o que o backup detecta (gitlinks no histórico e .gitmodules).
SHA_ALVO="$(git rev-parse HEAD)"
git update-index --add --cacheinfo "160000,${SHA_ALVO},modulos/exemplo"
cat > .gitmodules <<'EOF'
[submodule "modulos/exemplo"]
	path = modulos/exemplo
	url = https://github.com/exemplo/modulo-de-teste.git
EOF
git add .gitmodules
git commit -q -m "Adiciona um submódulo (gitlink) de exemplo"

# ---- 8. Tags e branch -------------------------------------------------------------------
git tag v1                                   # tag leve no commit atual
git tag -a v2 -m "Tag anotada de teste"      # tag anotada
git checkout -q -b feature/exemplo
echo "conteúdo da branch de exemplo" > exemplo-da-branch.txt
git add exemplo-da-branch.txt
git commit -q -m "Commit na branch feature/exemplo"
git checkout -q main

# ---- 9. Envio ---------------------------------------------------------------------------
git remote add origin "$DESTINO"
# O git-lfs envia os objetos LFS automaticamente durante o push (hook pre-push).
git push -u origin main feature/exemplo
git push origin v1 v2

# ---- 10. Resumo para conferência --------------------------------------------------------
echo
echo "=========================================================================="
echo "Repositório de teste enviado para: $DESTINO"
echo "Refs esperadas no backup: refs/heads/main, refs/heads/feature/exemplo,"
echo "                          refs/tags/v1, refs/tags/v2 (4 refs; sem pull requests)"
echo "Objetos LFS: 1 (lfs-6mb.bin)   Submódulos: 1 (modulos/exemplo)"
echo "Arquivo grande: arquivo-grande.dat (~25 MiB, envio em blocos)"
echo "Pasta temporária (pode apagar): $PASTA"
echo "=========================================================================="
