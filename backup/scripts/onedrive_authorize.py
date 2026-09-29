#!/usr/bin/env python3
"""Login único do HITL no OneDrive pessoal (device code): entrega o refresh token.

O QUE É
    Roda UMA VEZ, no computador do HITL (não no GitHub Actions). Faz o login na conta
    pessoal da Microsoft pelo fluxo "device code" e imprime o REFRESH TOKEN no terminal.
    O HITL o cola no secret ONEDRIVE_REFRESH_TOKEN do repositório. Depois disso o
    workflow renova sozinho e faz a rotação do secret a cada backup.

POR QUE EXISTE
    O OneDrive é pessoal: exige permissão DELEGADA (um login real, com consentimento).
    O workflow roda sem ninguém presente e não consegue fazer esse login.

COMO USAR
    1. Tenha o Client ID do registro no Entra (variável ONEDRIVE_CLIENT_ID).
    2. Rode:  python3 backup/scripts/onedrive_authorize.py --client-id <CLIENT_ID>
    3. Abra o endereço mostrado, digite o código e entre com a conta pessoal (outlook.com).
    4. Copie o refresh token impresso e grave-o em Settings > Secrets and variables >
       Actions > New repository secret, com o nome ONEDRIVE_REFRESH_TOKEN.
    5. Limpe o terminal. Repita só se o token expirar por falta de uso.

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - O refresh token vai SÓ para o terminal (saída padrão). Nunca é gravado em arquivo,
      nem em log, nem enviado a lugar nenhum além do terminal de quem rodou.
    - Não usa client secret: o app é público.
    - Pede só as permissões mínimas: Files.ReadWrite.AppFolder e offline_access.

CÓDIGOS DE SAÍDA
    0 login concluído   1 falha ou login recusado/expirado   64 uso incorreto

VARIÁVEIS DE AMBIENTE (opcionais)
    ONEDRIVE_CLIENT_ID    alternativa ao argumento --client-id
    ONEDRIVE_AUTH_BASE    (testes) base do endpoint OAuth; padrão: autoridade "consumers"
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Autoridade "consumers": contas pessoais. O escopo é o mínimo necessário.
DEFAULT_AUTH_BASE = "https://login.microsoftonline.com/consumers"
SCOPE = "Files.ReadWrite.AppFolder offline_access"


def post(url, form):
    """POST de formulário. Devolve (status, JSON). Erros HTTP viram (status, JSON do erro)."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Login único no OneDrive pessoal (device code).")
    parser.add_argument("--client-id", default=os.environ.get("ONEDRIVE_CLIENT_ID"),
                        help="Client ID do registro no Entra (ou variável ONEDRIVE_CLIENT_ID)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        sys.exit(64 if exc.code else 0)
    if not args.client_id:
        print("Informe o Client ID com --client-id ou na variável ONEDRIVE_CLIENT_ID.")
        sys.exit(64)
    base = os.environ.get("ONEDRIVE_AUTH_BASE", DEFAULT_AUTH_BASE).rstrip("/")

    # 1) Pede o código de dispositivo.
    status, dev = post(f"{base}/oauth2/v2.0/devicecode", {"client_id": args.client_id, "scope": SCOPE})
    if status != 200 or "device_code" not in dev:
        print(f"Falha ao iniciar o login: {dev.get('error', status)} {str(dev.get('error_description', ''))[:200]}")
        sys.exit(1)
    print()
    print(dev.get("message") or f"Abra {dev['verification_uri']} e digite o código {dev['user_code']}")
    print("Entre com a sua conta PESSOAL da Microsoft (a do OneDrive).")
    print()

    # 2) Espera o HITL concluir o login no navegador (consulta a cada `interval` segundos).
    interval = int(dev.get("interval", 5))
    deadline = time.time() + int(dev.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        status, tok = post(f"{base}/oauth2/v2.0/token", {
            "client_id": args.client_id, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": dev["device_code"]})
        if status == 200 and "refresh_token" in tok:
            # 3) Entrega o refresh token SÓ no terminal.
            print("=" * 70)
            print("REFRESH TOKEN (copie o valor abaixo e grave no secret ONEDRIVE_REFRESH_TOKEN):")
            print(tok["refresh_token"])
            print("=" * 70)
            print("Depois de gravar o secret, limpe este terminal. Este valor não foi salvo em arquivo.")
            return
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        print(f"Login não concluído: {err} {str(tok.get('error_description', ''))[:200]}")
        sys.exit(1)
    print("O código expirou antes de o login ser concluído. Rode o script de novo.")
    sys.exit(1)


if __name__ == "__main__":
    main()
