#!/usr/bin/env python3
"""Spike S4: cifra no navegador (Chromium headless) com a libsodium e abre em Python com PyNaCl.

PyNaCl é o que o onedrive.py já usa para gravar secrets (SealedBox), e o GitHub decifra com a libsodium: se o
PyNaCl abre o que o navegador cifrou, o formato é o mesmo. Só usa um texto de prova fictício; nenhum segredo real.
Uso: python3 docs/app/spikes/s4/test_s4.py   (precisa de pynacl e do Node com playwright)
"""
import base64, json, pathlib, subprocess, sys
from nacl import encoding, public

here = pathlib.Path(__file__).resolve().parent
sk = public.PrivateKey.generate()
pub_b64 = sk.public_key.encode(encoding.Base64Encoder()).decode()
plain = "valor-de-prova-fictício-ç~ã-" + "x" * 200      # com acento e tamanho maior que um bloco
out = subprocess.run(["node", str(here / "run_s4.mjs"), pub_b64, plain], capture_output=True, text=True, check=True).stdout
res = json.loads(out)
box = public.SealedBox(sk)
opened = box.decrypt(base64.b64decode(res["sealed_b64"])).decode()
ok = opened == plain
print(json.dumps({"S4": "OK" if ok else "FALHOU", "browser": res["browser"], "sealed_len": len(base64.b64decode(res["sealed_b64"])),
                  "roundtrip_equal": ok, "sodium_version": res["sodium_version"]}, ensure_ascii=False))
sys.exit(0 if ok else 1)
