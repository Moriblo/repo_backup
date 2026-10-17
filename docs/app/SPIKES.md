# Spikes do aplicativo (PR A)

Data: 17/10/2026. Referência: `docs/app/SPEC.md`, seção 7. Nenhum workflow, fluxo ou `commands.log` foi alterado.

**Decisões do HITL (17/10/2026):** S1 usa o fallback (autorização do OneDrive fora do navegador); S4 versiona o bundle da libsodium no repositório com `<script integrity>`, sem CDN; S2 e S3 ficam **pendentes de ação do HITL**; o aplicativo fica **pausado** até o HITL fechar as pendências do projeto. A `SPEC.md` foi atualizada com essas mudanças.

Resumo honesto: **S4 está provado; S1 tem resposta forte sem ação do HITL (não funciona direto do navegador); S2 e S3 ficam parciais e precisam de uma ação do HITL (token) para serem concluídos.** O ambiente onde rodei os testes só alcança uma lista fixa de domínios (não alcança `productionresultssa8.blob.core.windows.net`, nem CDNs como jsdelivr e cdnjs, e recusa `OPTIONS` em `api.github.com`), e isso limitou S2 e S3.

| Spike | Resultado | Falta |
|---|---|---|
| S1 device code (CORS) | **Não funciona direto do navegador** (endpoint `devicecode` sem CORS) | Opcional: confirmar no navegador real com o `client_id` verdadeiro (`s1.html`) |
| S2 log do job | **Parcial**: a API responde com `302` e CORS liberado; o domínio de destino não pôde ser testado | Ação do HITL: rodar `s2.html` com um token |
| S3 gravar no main | **Não testado** | Ação do HITL: repositório de teste e token |
| S4 sealed box | **Funciona** | Nada |

## S1: o device code da Microsoft funciona chamado do navegador?

**Teste mínimo.** `spikes/s1_cors_probe.sh` chama os dois endpoints da autoridade `consumers` com `Origin` de uma página do GitHub Pages e um `client_id` fictício (só os cabeçalhos importam; nada é autorizado). `spikes/s1.html` repete a chamada em um navegador de verdade.

**Resultado (17/10/2026).**

| Endpoint | Preflight `OPTIONS` | Resposta do `POST` |
|---|---|---|
| `.../consumers/oauth2/v2.0/devicecode` | `200`, **sem** `Access-Control-Allow-Origin` | `400` (client_id fictício), **sem** `Access-Control-Allow-Origin` |
| `.../consumers/oauth2/v2.0/token` | `200`, `Access-Control-Allow-Origin: *`, métodos `POST, OPTIONS` | `400`, `Access-Control-Allow-Origin: *` |

**Conclusão.** O passo que **pede** o código (`devicecode`) não libera CORS, então uma página no GitHub Pages **não consegue ler a resposta** (o `user_code` e o `device_code`). O `token` libera, mas sem o `device_code` não há o que consultar. Pelo plano da SPEC: **usar o fallback** (workflow manual com `onedrive_authorize.py`, ou script local).

**Limite do teste.** Medi com `curl`, não com um navegador, e com um `client_id` fictício; a resposta de sucesso com o `client_id` real não foi vista. O comportamento do navegador segue as regras de CORS (sem o cabeçalho, a leitura é bloqueada), mas se quiser fechar com certeza, abra `s1.html` e informe o `ONEDRIVE_CLIENT_ID` (não é segredo).

## S2: o log de um job do Actions pode ser lido do navegador?

**Teste mínimo.** `spikes/s2.html` chama `GET /repos/{repo}/actions/jobs/{id}/logs` com `fetch` (que segue o redirecionamento sozinho) e procura `BKP_RESULT`, `RESTORE_SUMMARY` e `RESTORE_TIMINGS` no texto.

**O que foi medido (17/10/2026, job `116768072648`, RST_REPO `req-20261016-003`, run 38836929546).**
- A API responde `302` com `Access-Control-Allow-Origin: *` e expõe o cabeçalho `Location` (cabeçalho citado em `Access-Control-Expose-Headers`).
- O `Location` aponta para `productionresultssa8.blob.core.windows.net/actions-results/...` (URL assinada, de vida curta).
- **Não consegui testar o destino:** o ambiente bloqueou o acesso a esse domínio. Sem isso, não sei se o armazenamento devolve `Access-Control-Allow-Origin` para o `GET`.

**Conclusão.** Indefinida. Ação do HITL: abrir `s2.html` (servida por `python3 -m http.server` na pasta `docs/app/spikes`, ou direto do Pages), informar um token fine-grained com **Actions: Read** e clicar. O resultado mostra se o log foi lido e se os três marcadores aparecem. O token fica só na memória da página.

**Fallback já previsto na SPEC (se falhar):** publicar um resumo sem dados sensíveis num lugar que a API sirva com CORS.

## S3: gravar no main pela API de conteúdo, com a proteção atual

**Teste mínimo.** `spikes/s3.html` faz `PUT /contents/spike-s3.txt` na `main` de um repositório de teste e depois `DELETE` do mesmo arquivo.

**Resultado.** **Não testado.** Por regra do protocolo (única escrita direta no `main` é o `commands.log`, depois do GO), não gravei nada na `main` do `Moriblo/repo_backup`; e o teste exige um token com escrita. Ação do HITL: criar um repositório de teste **descartável** com a mesma proteção da main (check obrigatório "Registry traceability", dono podendo contornar), gerar um token fine-grained só dele com **Contents: Read and write** e rodar `s3.html`. Também não consegui medir o preflight de CORS do `PUT` (o ambiente recusa `OPTIONS` em `api.github.com`).

**Fallback já previsto na SPEC (se falhar):** a linha entra por PR automático que o usuário mergeia.

## S4: cifrar secrets no navegador (sealed box)

**Teste mínimo.** `spikes/s4/test_s4.py` gera um par de chaves com PyNaCl, manda a chave pública (base64, o formato da API de secrets) para o Chromium headless, que cifra um texto de prova **fictício** com `crypto_box_seal` da libsodium (`spikes/s4/s4.html`), e abre o resultado com `SealedBox` do PyNaCl. É o mesmo par de bibliotecas do caso real: o `onedrive.py` cifra com PyNaCl e o GitHub decifra com a libsodium.

**Resultado (17/10/2026).** `{"S4": "OK", "roundtrip_equal": true}` com Chromium 141 e libsodium 1.0.22 (`libsodium-wrappers` 0.8.4); texto com acento e mais de 200 caracteres; saída de 279 bytes.

**Achado que muda a SPEC.** Uma tag `<script integrity="sha384-...">` aceita hash (SRI), mas um `import` de módulo ES de CDN **não** aceita. Por isso o plano "biblioteca de CDN fixada por versão e hash" fica mais seguro e mais simples assim: **versionar o bundle no próprio repositório** (`spikes/vendor/sodium-bundle.js`, 424 KB, wasm embutido, gerado por `spikes/entry-sodium.mjs` com `esbuild`) e carregá-lo com `<script integrity>`. Hash SHA-384 do bundle: `sha384-H0Hc8Xn8t2jh+Pk6fSn0i6FNVTsi2Sx2wHB13uVdmYl4p482i1IZ19KMO86QLpXV`. Não testei carregamento por CDN (o ambiente bloqueia jsdelivr e cdnjs).

**Não provado:** cifrar com a chave pública **real** do repositório e gravar o secret (exige token com Secrets: write), e o limite de tamanho do valor.

## Como reproduzir
- S1: `bash docs/app/spikes/s1_cors_probe.sh` (ou abrir `s1.html`).
- S4: `pip install pynacl` e `PLAYWRIGHT_MODULE=<caminho do playwright> python3 docs/app/spikes/s4/test_s4.py`.
- S2 e S3: servir `docs/app/spikes` (`python3 -m http.server`) e abrir `s2.html` / `s3.html` no navegador, com o token digitado na hora.
