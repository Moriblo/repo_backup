// Ponto de entrada para gerar vendor/sodium-bundle.js (libsodium-wrappers 0.8.4, wasm embutido).
// Regerar: npm i libsodium-wrappers@0.8.4 esbuild && npx esbuild entry-sodium.mjs --bundle --format=iife --minify --outfile=vendor/sodium-bundle.js
import sodium from "libsodium-wrappers";
window.sodium = sodium;
