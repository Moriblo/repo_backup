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
