#!/usr/bin/env bash
# Spike S1 (parte sem ação do HITL): o que a Microsoft (autoridade consumers) devolve de cabeçalhos CORS nos
# endpoints do device code. Usa um client_id FICTÍCIO: só os cabeçalhos importam, nada é autorizado.
# O navegador só consegue ler a resposta se ela trouxer Access-Control-Allow-Origin.
ORIGIN="${ORIGIN:-https://moriblo.github.io}"
B=https://login.microsoftonline.com/consumers/oauth2/v2.0
CID=00000000-0000-0000-0000-000000000000
show() { grep -i -E '^HTTP/[0-9.]+ [0-9]+ [A-Z]|^access-control-allow-(origin|methods|headers)' | grep -v 'Connection Established'; }
echo "== devicecode: preflight OPTIONS";  curl -sS -i -X OPTIONS "$B/devicecode" -H "Origin: $ORIGIN" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: content-type" | tr -d '\r' | show
echo "== devicecode: POST (resposta);";   curl -sS -i -X POST "$B/devicecode" -H "Origin: $ORIGIN" -d "client_id=$CID&scope=Files.ReadWrite.AppFolder offline_access" | tr -d '\r' | show
echo "== token: preflight OPTIONS";       curl -sS -i -X OPTIONS "$B/token" -H "Origin: $ORIGIN" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: content-type" | tr -d '\r' | show
echo "== token: POST (resposta)";         curl -sS -i -X POST "$B/token" -H "Origin: $ORIGIN" -d "grant_type=urn:ietf:params:oauth:grant-type:device_code&client_id=$CID&device_code=x" | tr -d '\r' | show
