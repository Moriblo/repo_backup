// Serve docs/app/spikes e roda a função sealSecret em Chromium headless. Uso: node run_s4.mjs <chave_pública_b64> <texto>
import http from "node:http"; import fs from "node:fs"; import path from "node:path"; import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const types = { ".html": "text/html", ".js": "text/javascript" };
const srv = http.createServer((q, r) => {
  const f = path.join(root, decodeURIComponent(q.url.split("?")[0]));
  if (!f.startsWith(root) || !fs.existsSync(f)) { r.writeHead(404).end(); return; }
  r.writeHead(200, { "content-type": types[path.extname(f)] || "application/octet-stream" }).end(fs.readFileSync(f));
}).listen(0, "127.0.0.1");
await new Promise(res => srv.on("listening", res));
const port = srv.address().port;
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
const page = await browser.newPage();
await page.goto(`http://127.0.0.1:${port}/s4/s4.html`);
const [pub, plain] = process.argv.slice(2);
const sealed_b64 = await page.evaluate(([p, t]) => window.sealSecret(p, t), [pub, plain]);
const sodium_version = await page.evaluate(() => sodium.sodium_version_string());
const out = { sealed_b64, browser: browser.version(), sodium_version };
await browser.close(); srv.close();
process.stdout.write(JSON.stringify(out));
