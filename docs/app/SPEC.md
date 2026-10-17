# repo_backup como aplicativo: especificação da v1 (rascunho)

Versão: 16/10/2026, revisada em 17/10/2026 com os resultados dos spikes (PR A). Status: decisão de rumo tomada pelo HITL; nada implementado. **Aplicativo pausado até o HITL fechar as pendências do projeto.** Código só entra por PR.

## 1. Objetivo
Transformar o repo_backup num aplicativo que qualquer pessoa possa copiar do próprio repositório e usar, sem depender de uma sessão do Claude. O app passa a ser o engine: apresenta o menu, monta o ECR, recebe o GO e grava a linha no commands.log. O Dispatcher e os workflows continuam como estão.

## 2. Decisões do HITL (16/10/2026)
- Forma: web estático no GitHub Pages da cópia de cada usuário (HTML/JS no próprio repositório, sem servidor).
- Papel do Claude: opcional. O app opera sozinho; o Claude continua possível como engine alternativo pelo protocolo (seção I.0, issue #5).
- Escopo da v1: menu + ECR + GO, acompanhar runs e assistente de instalação. Ver a evidência no OneDrive fica para depois.

## 3. O que não muda (regras do protocolo)
- O GO continua explícito, para o pedido exato, não reutilizável. No app, o GO é digitado (a palavra GO) depois de o usuário ver o ECR completo e a linha exata. Um clique sozinho não autoriza.
- A única escrita direta no main continua sendo uma linha no commands.log, depois do GO. Código só por PR.
- A origem é sempre somente leitura. Falta de evidência nunca é sucesso.
- Nenhum segredo aparece na tela, no log, no repositório ou no armazenamento do navegador.

## 4. Fluxos da v1
### 4.1 Menu, ECR e GO
1. O app lê do main da cópia: backup/capabilities.yaml (menu), o workflow da capability escolhida (inputs do workflow_call = ECR), o ramo do commands-log-line.schema.yaml e o commands.log (ids usados).
2. O usuário escolhe a capability e preenche os campos. O app valida contra o schema.
3. O app calcula o próximo request_id pela data UTC e pelos ids do log, e mostra o ECR e a linha exata.
4. Para BKP_REPO, o app conduz as duas linhas: preflight BLOCKED com as restrições e a continuação com preflight_decision e as restrições aceitas uma a uma, com novo GO.
5. O usuário digita GO. O app grava a linha pela API de conteúdo do GitHub (PUT /contents/commands.log com o sha atual; em conflito, relê, recalcula o id, mostra de novo e pede novo GO).
6. NO-GO ou fechar a página: nada é gravado.
### 4.2 Acompanhar runs
- Depois da gravação, o app acha o run do Dispatcher pelo commit e mostra jobs, passos e conclusão.
- O resultado (BKP_RESULT, RESTORE_SUMMARY, RESTORE_TIMINGS) está no log do job; ler o log pelo navegador depende do spike S2.
- O app não oferece novo comando enquanto houver run do Dispatcher em andamento (o refresh token do OneDrive gira a cada uso).
### 4.3 Assistente de instalação
1. Copiar o repositório (template ou fork) e ligar Pages e Actions.
2. Criar o environment onedrive-backup, restrito à main.
3. Registrar o app no Microsoft Entra (contas pessoais, cliente público, Files.ReadWrite.AppFolder e offline_access) e gravar ONEDRIVE_CLIENT_ID.
4. Autorizar o OneDrive **fora do navegador** e gravar ONEDRIVE_REFRESH_TOKEN: workflow manual com onedrive_authorize.py, ou script local (spike S1: o endpoint devicecode da Microsoft não libera CORS, então o navegador não consegue pedir o código).
5. Criar SOURCE_READ_TOKEN, PROJECTS_READ_TOKEN e RESTORE_WRITE_TOKEN com escopos mínimos e gravá-los como secrets (cifrar com a chave pública do repositório, com a libsodium versionada no próprio repositório e carregada com `<script integrity>`, sem CDN; spike S4).
6. Opcional: GitHub App Writer para a rotação do refresh token.
7. Proteção da main com o check "Registry traceability".
8. Teste final: um BKP_ISSUES pequeno de um repositório do próprio usuário.

## 5. Credencial do app no navegador
- Token fine-grained só para a própria cópia do repo_backup: Contents read/write, Actions read e, só na instalação, Secrets, Environments e Administration conforme o passo.
- O token fica só na memória da página (nunca localStorage, nunca na URL).
- O app só fala com api.github.com. Sem servidor próprio, sem terceiros. A Microsoft não é chamada pelo navegador (spike S1): a autorização do OneDrive é feita fora dele.
- A gravação no main com o check exigido funciona porque o dono pode contornar a regra (sem "Do not allow bypassing"); o app explica isso na instalação.

## 6. Restrições conhecidas
1. Plano Free: segredos de environment só funcionam em repositório público; o log do Actions fica público (sem títulos). O app avisa antes da instalação.
2. O app no Pages é público, mas não contém dados: lê tudo com o token do usuário.
3. Instalação pesada: o assistente é o que torna o projeto usável pela comunidade.

## 7. Spikes
- S1: o device code da Microsoft (autoridade consumers) funciona chamado do navegador (CORS)? Se não: workflow manual com onedrive_authorize.py, ou script local.
  - **Resultado (17/10/2026): não funciona.** O endpoint devicecode não devolve CORS; vale o fallback (autorização do OneDrive fora do navegador). Detalhes em docs/app/SPIKES.md.
- S2: o log de um job do Actions pode ser lido do navegador (o download redireciona para outro domínio)? Se não: publicar um resumo sem dados sensíveis num lugar que a API sirva com CORS.
  - **Resultado (17/10/2026): pendente de ação do HITL** (parcial: a API responde 302 com CORS; o domínio de destino não pôde ser testado).
- S3: gravar no main pela API de conteúdo, com a proteção atual, funciona para o dono com token fine-grained? Se não: linha via PR automático que o usuário mergeia.
  - **Resultado (17/10/2026): pendente de ação do HITL** (repositório de teste e token; não testado).
- S4: cifrar secrets no navegador (sealed box libsodium) funciona? Se não: o assistente mostra os comandos gh secret set.
  - **Resultado (17/10/2026): funciona.** Mudança de rumo: em vez de biblioteca de CDN fixada por versão e hash, o bundle da libsodium (libsodium-wrappers 0.8.4) é **versionado no repositório** (docs/app/spikes/vendor/sodium-bundle.js) e carregado com `<script integrity>`, porque SRI não vale para `import` de módulo de CDN.

## 8. Entregas (PRs)
A: spikes S1 a S4. B: app só leitura (menu, ECR, linha exata, sem gravar). C: gravação com GO digitado e acompanhamento de runs. D: assistente de instalação e README para a comunidade. Depois: seção do app como engine no BACKUP_REPOSITORY.md e registro do app no capabilities.yaml.

## 9. Fora da v1
Evidência no OneDrive pelo navegador, restauração guiada pela lista de backups, várias contas, outros destinos, tradução para inglês.
