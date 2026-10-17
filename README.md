# repo_backup

Repositório de **execução e controle** do backup de repositórios do GitHub para o OneDrive pessoal.
A origem é sempre somente leitura; o OneDrive usa OAuth delegado (`Files.ReadWrite.AppFolder`), sem OIDC.

## Por onde começar
- **`BACKUP_REPOSITORY.md`**: protocolo (em inglês) e, no final, o *Guia operacional* (em português) com players, fluxo, arquivos, segredos e códigos de saída.
- **`docs/contexto/STATUS_ATUAL.md`**: onde o projeto está, próximos passos e registro de decisões do HITL. Leia primeiro numa sessão nova. As instruções de continuidade estão em `docs/contexto/INSTRUCOES_DO_PROJETO.md`.
- **`backup/capabilities.yaml`**: registro das capabilities, do Mnemonic (`backup_repository` → `BKP_REPO`) e dos artefatos.

## Como funciona, em uma frase
O engine apresenta o menu, o HITL escolhe e dá o GO, o engine grava uma linha no `commands.log`, o *Dispatcher* valida e chama o workflow `BKP_REPO`, que faz o preflight, a leitura da origem e a evidência.

## Regras
- Mudanças de código só por pull request.
- Escrita direta no `main` somente no `commands.log` e somente após GO.
- Acompanhamento dos ajustes em andamento: issue #7.

## Limites conhecidos
Para quem for usar o projeto. O detalhe técnico está em `BACKUP_REPOSITORY.md` (seção I.14).

- **Nome do destino:** o `destination` aceita só letras sem acento, números e os caracteres `_ . - /` (relativo à pasta do app no OneDrive, sem `/` inicial e sem `..`). O schema recusa acento (decisão de 17/10/2026).
- **Repositórios muito grandes:** o job do `BKP_REPO` e o da restauração têm limite de 120 minutos (os do `BKP_PROJ` e do `BKP_ISSUES`, 60), e o disco do runner é finito. O comportamento com repositórios grandes não foi medido.
- **Submódulos:** só o gitlink é preservado, não o conteúdo do submódulo.
- **Reações em issues e Projects de organização:** não provados em run real.
- **Projects:** as colunas visíveis das views podem não ser lidas (erro interno do GitHub, reproduzido em 16 e 17/10/2026 nos Projects do MIT); itens, campos e valores são preservados.
- **Restauração:** as refs `refs/pull/*` não voltam (o GitHub não aceita enviá-las); número, autor e datas originais das issues não voltam; as regras dos workflows dos Projects precisam ser refeitas à mão (a API não as expõe).
- **Repositório executor público no plano Free:** nesse plano os segredos de environment só funcionam em repositório público. O log do Actions e o `commands.log` mostram nomes de repositórios e branches, nunca conteúdo, títulos ou segredos.
- **GitHub Actions (classe `actions`):** logs e artifacts só existem enquanto durar a retenção do repositório (por padrão, 90 dias): o que já expirou antes do backup não pode ser recuperado, e fica registrado como expirado. Até 5 GiB de logs e artifacts por backup (os mais antigos primeiro). É registro: execuções, logs e artifacts não são restaurados. Logs de repositório privado podem conter dados sensíveis que o GitHub não mascarou; o pacote vai só ao OneDrive.
- **Acesso ao OneDrive:** expira com cerca de 90 dias sem nenhum run. Para renovar, rode `backup/scripts/onedrive_authorize.py`.
