#!/usr/bin/env python3
"""Acesso ao OneDrive pessoal (BKP_REPO, BKP_PROJ, RST_REPO): sessão OAuth, rotação do secret, destino, upload e download.

O QUE É
    Biblioteca e linha de comando usadas pelo workflow backup-repository.yml para
    gravar o backup no OneDrive pessoal, dentro do AppFolder (Apps/<nome do registro>/).
    Usa OAuth DELEGADO (escopo Files.ReadWrite.AppFolder), com app público: sem client
    secret e sem OIDC. A credencial durável é o refresh token, obtido uma vez pelo HITL
    com onedrive_authorize.py e guardado no secret ONEDRIVE_REFRESH_TOKEN.

O QUE FAZ
    1. Troca o refresh token por um access token de curta duração.
    2. ROTAÇÃO: a Microsoft devolve um refresh token novo a cada uso. O novo valor é
       gravado no secret do repositório, por um GitHub App Writer (só Secrets: write),
       ANTES de qualquer outro uso do OneDrive. Se a gravação falhar, tudo para.
    3. Valida o destino: escreve, lê de volta e apaga um arquivo de teste no AppFolder.
    4. Envia arquivos (upload simples ou em blocos) e confere o hash de cada um.
    5. Baixa arquivos (restauração) e confere tamanho e hash de cada um contra o que o OneDrive informa.

SUBCOMANDOS (linha de comando)
    upload   --prefix <pasta> --result <arquivo.json> --files <arq1> [<arq2> ...]
             Envia os arquivos para <pasta>/ no AppFolder e grava o resultado em JSON.
    download --prefix <pasta> --dest <pasta local> --names <arq1> [<arq2> ...] [--result <arquivo.json>]
             Baixa <pasta>/<nome> do AppFolder para <pasta local>/<nome>, conferindo tamanho e hash.
             O arquivo inteiro é lido em memória: repositórios muito grandes são um limite conhecido.
    cleanup  Apaga o arquivo de estado local (com o refresh token corrente).

CÓDIGOS DE SAÍDA
    0  ok
    22 falha de Graph, upload ou hash (GRAPH_ERROR, UPLOAD_FAILED, HASH_MISMATCH, PROBE_FAILED)
    23 falha de configuração, autenticação ou rotação do secret (CONFIG_MISSING, AUTH_FAILED,
       SECRET_ROTATION_FAILED)
    64 uso incorreto

VARIÁVEIS DE AMBIENTE
    ONEDRIVE_CLIENT_ID         Client ID do registro no Entra (variável do repositório)
    ONEDRIVE_REFRESH_TOKEN     refresh token inicial (secret do repositório)
    SECRETS_WRITER_TOKEN       (opcional) token do GitHub App Writer (permissão Secrets: write).
                               Sem ele NÃO há rotação: o secret não é regravado, um aviso é impresso
                               e o refresh token original vale até expirar (cerca de 90 dias);
                               depois disso é preciso repetir o onedrive_authorize.py.
    ONEDRIVE_SECRETS_ENVIRONMENT  (opcional) nome do environment que guarda o secret; vazio = secret
                               do repositório
    GITHUB_REPOSITORY          dono/nome do repositório que guarda o secret
    GITHUB_API_URL             (opcional) base da API do GitHub
    RUNNER_TEMP                pasta temporária do runner (estado local)
    ONEDRIVE_AUTH_BASE         (opcional, testes) base do endpoint OAuth
    ONEDRIVE_GRAPH_BASE        (opcional, testes) base do Microsoft Graph
    ONEDRIVE_CHUNK_SIZE        (opcional, testes) tamanho do bloco de upload, múltiplo de 320 KiB
    ONEDRIVE_RETRY_DELAY       (opcional, testes) espera base entre tentativas, em segundos

REGRAS QUE ESTE SCRIPT NUNCA QUEBRA
    - Nunca imprime access token, refresh token ou corpo de resposta que possa conter token.
    - Nunca escreve na origem: só lê o que o workflow entrega e grava no AppFolder.
    - O estado local (refresh token corrente) fica só na pasta temporária do runner, com
      permissão 600, e é apagado no fim da execução (subcomando cleanup).
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Constantes ---------------------------------------------------------------------

# Autoridade "consumers": contas pessoais da Microsoft (o OneDrive do HITL é pessoal).
DEFAULT_AUTH_BASE = "https://login.microsoftonline.com/consumers"
DEFAULT_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Permissão delegada mínima (só o AppFolder) e offline_access, que gera o refresh token.
SCOPE = "Files.ReadWrite.AppFolder offline_access"
# Nome do secret que guarda o refresh token.
SECRET_NAME = "ONEDRIVE_REFRESH_TOKEN"
# O Graph exige blocos de upload em múltiplos de 320 KiB. Padrão: 10 MiB (32 x 320 KiB).
CHUNK_UNIT = 320 * 1024
DEFAULT_CHUNK = 32 * CHUNK_UNIT
# Até este tamanho usa upload simples (PUT único); acima, sessão de upload em blocos.
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
# Renova o access token quando faltar menos que isto para expirar.
EXPIRY_MARGIN = 300


class OneDriveError(Exception):
    """Erro com um código estável (ver "CÓDIGOS DE SAÍDA") e mensagem segura para log."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# Códigos de erro que viram saída 23 (configuração, autenticação, rotação); os demais viram 22.
AUTH_CODES = {"CONFIG_MISSING", "AUTH_FAILED", "SECRET_ROTATION_FAILED"}


# --- HTTP -----------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Não segue redirecionamentos: o Graph redireciona downloads para uma URL pré-autenticada
    que NÃO pode receber o cabeçalho Authorization. Tratamos o redirecionamento à mão."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(method, url, *, headers=None, data=None, form=None, json_body=None, timeout=120):
    """Faz uma requisição e devolve (status, cabeçalhos, corpo em bytes).

    Repete até 3 vezes em erro de rede ou status 429/5xx (respeitando Retry-After).
    Outros status HTTP são devolvidos ao chamador, que decide o que fazer.
    """
    headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    delay = float(os.environ.get("ONEDRIVE_RETRY_DELAY", "2"))
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                wait = float(exc.headers.get("Retry-After", delay * (2 ** attempt)))
                time.sleep(min(wait, 60))
                last = (exc.code, dict(exc.headers), body)
                continue
            return exc.code, dict(exc.headers), body
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = (0, {}, str(exc).encode())
            if attempt < 2:
                time.sleep(delay * (2 ** attempt))
                continue
    return last


def _json(body):
    """Decodifica o corpo JSON; devolve {} se não for JSON."""
    try:
        return json.loads(body or b"{}")
    except (ValueError, TypeError):
        return {}


def _safe_error(status, body, headers=None):
    """Resumo seguro de um erro HTTP: só código e mensagem do serviço, sem valores de token.

    Erros da API do GitHub trazem `message`; o cabeçalho X-Accepted-GitHub-Permissions (se
    vier em `headers`) diz qual permissão o endpoint exige, o que acelera o diagnóstico de 403.
    """
    err = _json(body)
    if "message" in err and "error" not in err:       # formato da API do GitHub
        perms = ""
        if headers:
            perms = str(dict(headers).get("X-Accepted-GitHub-Permissions")
                        or dict(headers).get("x-accepted-github-permissions") or "")[:120]
        extra = f" (permissão exigida: {perms})" if perms else ""
        return f"HTTP {status}: {str(err['message'])[:160]}{extra}"
    if isinstance(err.get("error"), dict):          # formato do Graph
        return f"HTTP {status} {err['error'].get('code', '')}: {str(err['error'].get('message', ''))[:160]}"
    return f"HTTP {status} {err.get('error', '')}: {str(err.get('error_description', ''))[:160]}".strip()


# --- Hash -----------------------------------------------------------------------------

def quickxor_hash(path, block=160 * 65536):
    """quickXorHash da Microsoft (base64), o hash que o OneDrive pessoal costuma devolver.

    Algoritmo (160 bits): o byte de índice i é combinado por XOR no registrador de 160 bits,
    deslocado de (11 * i) mod 160 bits e "dobrado" (rotação circular); no fim, o tamanho do
    arquivo (64 bits, little-endian) é combinado por XOR nos 8 últimos bytes e o resultado
    (20 bytes) sai em base64. Como 11 e 160 são primos entre si, cada resíduo (i mod 160) tem
    um deslocamento próprio: por isso basta acumular o XOR dos bytes de cada resíduo, o que se
    faz dobrando blocos de 160 bytes (rápido, sem laço por byte).
    """
    width = 160
    acc, size = 0, 0                                       # acc: XOR dos blocos de 160 bytes (1280 bits)
    # `block` deve ser múltiplo de 160: assim o índice global de cada bloco mantém o resíduo.
    assert block % width == 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            size += len(chunk)
            chunk += bytes(-len(chunk) % width)            # completa o último bloco com zeros (não altera o XOR)
            folded = 0
            for i in range(0, len(chunk), width):
                folded ^= int.from_bytes(chunk[i:i + width], "little")
            acc ^= folded
    reg = 0
    mask = (1 << 160) - 1
    for r in range(width):                                 # r = resíduo do índice do byte
        b = (acc >> (8 * r)) & 0xFF
        if b:
            v = b << ((11 * r) % width)
            reg ^= (v & mask) | (v >> 160)                 # rotação circular de 160 bits
    out = bytearray(reg.to_bytes(20, "little"))
    for k, byte in enumerate((size & (2 ** 64 - 1)).to_bytes(8, "little")):
        out[12 + k] ^= byte
    return base64.b64encode(bytes(out)).decode()


def file_hashes(path):
    """(tamanho, sha256, sha1) de um arquivo, lidos em blocos de 1 MiB."""
    sha256, sha1, size = hashlib.sha256(), hashlib.sha1(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha256.update(chunk)
            sha1.update(chunk)
            size += len(chunk)
    return size, sha256.hexdigest(), sha1.hexdigest()


# --- Sessão ---------------------------------------------------------------------------

class Session:
    """Sessão autenticada no OneDrive. Renova o token quando preciso e faz a rotação do secret."""

    def __init__(self, client_id, refresh_token, writer_token, repo):
        self.client_id = client_id
        self.writer_token = writer_token
        self.repo = repo
        self.auth_base = os.environ.get("ONEDRIVE_AUTH_BASE", DEFAULT_AUTH_BASE).rstrip("/")
        self.graph_base = os.environ.get("ONEDRIVE_GRAPH_BASE", DEFAULT_GRAPH_BASE).rstrip("/")
        self.state_file = pathlib.Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "onedrive_state.json"
        # Refresh token vigente: o do estado local (se já houve renovação nesta execução) ou o do secret.
        self._secret_value = refresh_token           # valor que está hoje no secret do GitHub
        self._refresh_token = self._load_state() or refresh_token
        self._access_token = None
        self._expires_at = 0.0
        self._warned = False

    @classmethod
    def from_env(cls):
        """Monta a sessão a partir do ambiente. Sem toda a configuração, falha com CONFIG_MISSING."""
        names = ["ONEDRIVE_CLIENT_ID", "ONEDRIVE_REFRESH_TOKEN", "GITHUB_REPOSITORY"]
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise OneDriveError("CONFIG_MISSING", "configuração ausente: " + ", ".join(missing))
        return cls(os.environ["ONEDRIVE_CLIENT_ID"], os.environ["ONEDRIVE_REFRESH_TOKEN"],
                   os.environ.get("SECRETS_WRITER_TOKEN", ""), os.environ["GITHUB_REPOSITORY"])

    # --- estado local (refresh token corrente) ---
    def _load_state(self):
        try:
            return json.loads(self.state_file.read_text()).get("refresh_token")
        except (OSError, ValueError):
            return None

    def _save_state(self, refresh_token):
        """Grava o refresh token corrente com permissão 600 (só o dono lê)."""
        fd = os.open(self.state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"refresh_token": refresh_token}, f)

    # --- autenticação ---
    def refresh(self):
        """Obtém um access token novo e, se o refresh token mudou, grava-o no secret ANTES de seguir."""
        status, _, body = http("POST", f"{self.auth_base}/oauth2/v2.0/token", form={
            "client_id": self.client_id, "grant_type": "refresh_token",
            "refresh_token": self._refresh_token, "scope": SCOPE})
        data = _json(body)
        if status != 200 or "access_token" not in data:
            raise OneDriveError("AUTH_FAILED", "não foi possível renovar o token: " + _safe_error(status, body))
        new_rt = data.get("refresh_token") or self._refresh_token
        self._save_state(new_rt)
        self._refresh_token = new_rt
        # Rotação: se o valor mudou em relação ao que está no secret, grava agora. Falhou: para tudo.
        if new_rt != self._secret_value:
            if self.writer_token:
                self._write_secret(new_rt)
            elif not self._warned:
                # Sem App Writer não há como regravar o secret: segue com o token original.
                print("AVISO: SECRETS_WRITER_TOKEN ausente; rotação do refresh token DESLIGADA "
                      "(o token original expira em cerca de 90 dias).", file=sys.stderr)
                self._warned = True
            self._secret_value = new_rt
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))

    def token(self):
        """Access token válido (renova se estiver perto de expirar)."""
        if not self._access_token or time.time() > self._expires_at - EXPIRY_MARGIN:
            self.refresh()
        return self._access_token

    def _write_secret(self, value):
        """Grava `value` (criptografado) no secret ONEDRIVE_REFRESH_TOKEN, com o token do App Writer."""
        api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        h = {"Authorization": f"Bearer {self.writer_token}", "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        try:
            from nacl import encoding, public           # PyNaCl: o GitHub exige "sealed box"
        except ImportError:
            raise OneDriveError("SECRET_ROTATION_FAILED", "PyNaCl não está instalado no runner")
        # Secret de environment usa outro caminho da API que o secret de repositório.
        env_name = os.environ.get("ONEDRIVE_SECRETS_ENVIRONMENT", "")
        base = (f"{api}/repos/{self.repo}/environments/{urllib.parse.quote(env_name, safe='')}/secrets" if env_name
                else f"{api}/repos/{self.repo}/actions/secrets")
        st, rh, body = http("GET", f"{base}/public-key", headers=h)
        key = _json(body)
        if st != 200 or "key" not in key:
            raise OneDriveError("SECRET_ROTATION_FAILED", "não foi possível ler a chave pública dos secrets: " + _safe_error(st, body, rh))
        box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
        encrypted = base64.b64encode(box.encrypt(value.encode())).decode()
        st, rh, body = http("PUT", f"{base}/{SECRET_NAME}", headers=h,
                           json_body={"encrypted_value": encrypted, "key_id": key["key_id"]})
        if st not in (201, 204):
            raise OneDriveError("SECRET_ROTATION_FAILED", "não foi possível gravar o novo refresh token no secret: " + _safe_error(st, body, rh))

    # --- Graph ---
    def _auth(self):
        return {"Authorization": f"Bearer {self.token()}"}

    @staticmethod
    def _path(rel):
        """Trecho de URL do Graph para um caminho relativo ao AppFolder: /me/drive/special/approot:/a/b"""
        return "/me/drive/special/approot:/" + urllib.parse.quote(rel.strip("/"), safe="/")

    def graph(self, method, url, **kw):
        headers = {**self._auth(), **kw.pop("headers", {})}
        return http(method, url if url.startswith("http") else self.graph_base + url, headers=headers, **kw)

    def validate_destination(self, prefix):
        """Prova que dá para escrever, ler e apagar dentro de <AppFolder>/<prefix>/."""
        st, _, body = self.graph("GET", "/me/drive/special/approot")
        if st != 200:
            raise OneDriveError("GRAPH_ERROR", "AppFolder inacessível: " + _safe_error(st, body))
        nonce = secrets.token_hex(16)
        probe = f"{prefix.strip('/')}/_probe_{nonce}.txt"
        content = f"probe {nonce}".encode()
        st, _, body = self.graph("PUT", f"{self._path(probe)}:/content", data=content,
                                 headers={"Content-Type": "text/plain"})
        item = _json(body)
        if st not in (200, 201) or "id" not in item:
            raise OneDriveError("PROBE_FAILED", "escrita de teste recusada: " + _safe_error(st, body))
        try:
            st, hdr, body = self.graph("GET", f"{self._path(probe)}:/content")
            if st in (301, 302, 303, 307, 308):          # download pré-autenticado: sem Authorization
                st, _, body = http("GET", hdr.get("Location") or hdr.get("location"))
            if st != 200 or body != content:
                raise OneDriveError("PROBE_FAILED", "leitura de teste não devolveu o conteúdo gravado")
        finally:
            self.graph("DELETE", f"/me/drive/items/{item['id']}")     # limpa o arquivo de teste

    def upload_file(self, local, rel):
        """Envia um arquivo para <AppFolder>/<rel> e confere o hash. Devolve o registro do envio."""
        size, sha256, sha1 = file_hashes(local)
        q = "?%40microsoft.graph.conflictBehavior=replace"     # reenvio da mesma requisição é idempotente
        if size <= SIMPLE_UPLOAD_MAX:
            with open(local, "rb") as f:
                st, _, body = self.graph("PUT", f"{self._path(rel)}:/content{q}", data=f.read(),
                                         headers={"Content-Type": "application/octet-stream"})
            item = _json(body)
            if st not in (200, 201):
                raise OneDriveError("UPLOAD_FAILED", f"envio de {rel} recusado: " + _safe_error(st, body))
        else:
            item = self._upload_session(local, rel, size)
        method, info = self._verify(item, rel, size, sha256, sha1, local)
        return {"remote_path": rel, "bytes": size, "sha256": sha256, "verified_by": method, "item_id": item.get("id"), **info}

    def download_file(self, rel, local):
        """Baixa <AppFolder>/<rel> para `local` e confere tamanho e hash contra o item do OneDrive.

        Devolve {"remote_path", "bytes", "sha256", "verified_by"}. Divergência é HASH_MISMATCH e o
        arquivo parcial é apagado: nunca fica um arquivo não conferido no disco. O download é
        pré-autenticado (redirecionamento do Graph): a segunda requisição NÃO leva Authorization.
        """
        st, _, body = self.graph("GET", self._path(rel))
        item = _json(body)
        if st != 200 or "id" not in item:
            raise OneDriveError("GRAPH_ERROR", f"{rel} não encontrado no OneDrive: " + _safe_error(st, body))
        st, hdr, body = self.graph("GET", f"{self._path(rel)}:/content")
        if st in (301, 302, 303, 307, 308):
            st, _, body = http("GET", hdr.get("Location") or hdr.get("location"))
        if st != 200:
            raise OneDriveError("GRAPH_ERROR", f"download de {rel} recusado: HTTP {st}")
        local = pathlib.Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(body)
        try:
            size, sha256, sha1 = file_hashes(local)
            method, _ = self._verify(item, rel, size, sha256, sha1, local)
        except OneDriveError:
            local.unlink(missing_ok=True)
            raise
        return {"remote_path": rel, "bytes": size, "sha256": sha256, "verified_by": method}

    def _upload_session(self, local, rel, size):
        """Upload em blocos (sessão de upload do Graph). Devolve o driveItem final."""
        st, _, body = self.graph("POST", f"{self._path(rel)}:/createUploadSession",
                                 json_body={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        url = _json(body).get("uploadUrl")
        if st != 200 or not url:
            raise OneDriveError("UPLOAD_FAILED", f"sessão de upload de {rel} recusada: " + _safe_error(st, body))
        chunk = int(os.environ.get("ONEDRIVE_CHUNK_SIZE", DEFAULT_CHUNK))
        if chunk % CHUNK_UNIT:
            raise OneDriveError("UPLOAD_FAILED", "ONEDRIVE_CHUNK_SIZE deve ser múltiplo de 320 KiB")
        sent = 0
        with open(local, "rb") as f:
            while sent < size:
                data = f.read(chunk)
                end = sent + len(data) - 1
                # A uploadUrl é pré-autenticada: NÃO leva o cabeçalho Authorization.
                st, _, body = http("PUT", url, data=data, headers={
                    "Content-Length": str(len(data)), "Content-Range": f"bytes {sent}-{end}/{size}"})
                sent += len(data)
                if sent < size and st != 202:
                    raise OneDriveError("UPLOAD_FAILED", f"bloco de {rel} recusado: " + _safe_error(st, body))
                if sent >= size and st not in (200, 201):
                    raise OneDriveError("UPLOAD_FAILED", f"bloco final de {rel} recusado: " + _safe_error(st, body))
        return _json(body)

    def _verify(self, item, rel, size, sha256, sha1, local):
        """Confere tamanho e hash do item enviado.

        Devolve (método, info). Método: sha256, sha1, quickXorHash ou size-only. `info` traz os
        NOMES dos hashes que o OneDrive devolveu (nunca valores sensíveis) para diagnóstico.

        quickXorHash: o OneDrive pessoal o devolve (confirmado em run real: 21 arquivos conferidos,
        incluindo envios em blocos). Divergência é erro (HASH_MISMATCH), como nos demais hashes.
        """
        hashes = {}
        for attempt in range(3):
            hashes = (item.get("file") or {}).get("hashes") or {}
            if hashes or attempt == 2:
                break
            # O Graph às vezes calcula o hash depois do envio: consulta o item de novo.
            time.sleep(float(os.environ.get("ONEDRIVE_RETRY_DELAY", "2")))
            st, _, body = self.graph("GET", f"/me/drive/items/{item['id']}")
            item = _json(body) if st == 200 else item
        info = {"remote_hash_algorithms": sorted(hashes)}
        if int(item.get("size", -1)) != size:
            raise OneDriveError("HASH_MISMATCH", f"tamanho de {rel} no OneDrive ({item.get('size')}) difere do local ({size})")
        if hashes.get("sha256Hash"):
            if hashes["sha256Hash"].lower() != sha256:
                raise OneDriveError("HASH_MISMATCH", f"sha256 de {rel} difere do local")
            return "sha256", info
        if hashes.get("sha1Hash"):
            if hashes["sha1Hash"].lower() != sha1:
                raise OneDriveError("HASH_MISMATCH", f"sha1 de {rel} difere do local")
            return "sha1", info
        if hashes.get("quickXorHash"):
            if hashes["quickXorHash"] != quickxor_hash(local):
                raise OneDriveError("HASH_MISMATCH", f"quickXorHash de {rel} difere do local")
            return "quickXorHash", info
        return "size-only", info          # sem hash conferido: só o tamanho foi conferido


# --- Linha de comando -----------------------------------------------------------------

def cmd_upload(args):
    """Envia os arquivos para <prefix>/ e grava a lista dos envios em JSON."""
    session = Session.from_env()
    results = []
    for path in args.files:
        rel = f"{args.prefix.strip('/')}/{pathlib.Path(path).name}"
        results.append(session.upload_file(path, rel))
        print(f"ONEDRIVE_UPLOAD {json.dumps({k: results[-1][k] for k in ('remote_path', 'bytes', 'verified_by', 'remote_hash_algorithms')}, sort_keys=True)}", flush=True)
    pathlib.Path(args.result).write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")


def cmd_download(args):
    """Baixa os arquivos de <prefix>/ para a pasta local e grava a lista dos downloads em JSON (opcional)."""
    session = Session.from_env()
    results = []
    for name in args.names:
        results.append(session.download_file(f"{args.prefix.strip('/')}/{name}", pathlib.Path(args.dest) / name))
        print(f"ONEDRIVE_DOWNLOAD {json.dumps({k: results[-1][k] for k in ('remote_path', 'bytes', 'verified_by')}, sort_keys=True)}", flush=True)
    if args.result:
        pathlib.Path(args.result).write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")


def cmd_cleanup(_args):
    """Apaga o estado local com o refresh token corrente."""
    state = pathlib.Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "onedrive_state.json"
    if state.exists():
        state.unlink()
    print("estado local do OneDrive removido.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload")
    up.add_argument("--prefix", required=True)
    up.add_argument("--result", required=True)
    up.add_argument("--files", nargs="+", required=True)
    up.set_defaults(func=cmd_upload)
    down = sub.add_parser("download")
    down.add_argument("--prefix", required=True)
    down.add_argument("--dest", required=True)
    down.add_argument("--names", nargs="+", required=True)
    down.add_argument("--result")
    down.set_defaults(func=cmd_download)
    sub.add_parser("cleanup").set_defaults(func=cmd_cleanup)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        sys.exit(64 if exc.code else 0)
    try:
        args.func(args)
    except OneDriveError as exc:
        print(f"::error::OneDrive {exc.code}: {exc.message}")
        sys.exit(23 if exc.code in AUTH_CODES else 22)


if __name__ == "__main__":
    main()
