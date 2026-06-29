#!/usr/bin/env node
// render-cli.js — Interfaz CLI para ser llamada desde Python via subprocess.
//
// Uso:
//   echo '<json>' | node render-cli.js [--gif]
//
// Entrada (stdin): JSON con el objeto pet (igual a petGenerator.generate_pet)
//   + "formattedName" (opcional, se genera si no se pasa)
//
// Salida (stdout): buffer de imagen PNG (o GIF con --gif)
//
// Flags:
//   --gif   Genera GIF animado (usa asciiAnimator)

const { renderPetImage, renderPetGif } = require("./petRenderer");
const { generateFrames } = require("./asciiAnimator");

function formatName(name, rarity) {
  if (rarity === "raro") return `~ ${name} ~`;
  if (rarity === "épico") return `·:·${name}·:·`;
  if (rarity === "legendario") return `═══〔 ${name.toUpperCase()} 〕═══`;
  return name;
}

async function main() {
  const isGif = process.argv.includes("--gif");
  const raw = await new Promise((resolve) => {
    let d = "";
    process.stdin.on("data", (c) => (d += c));
    process.stdin.on("end", () => resolve(d));
  });

  let pet;
  try {
    pet = JSON.parse(raw);
  } catch (e) {
    process.stderr.write("render-cli: invalid JSON on stdin\n");
    process.exit(1);
  }

  const title = pet.formattedName || formatName(pet.name, pet.rarity);

  let buf;
  if (isGif) {
    const frames = generateFrames(pet);
    buf = await renderPetGif(pet, title, frames);
  } else {
    buf = renderPetImage(pet, title);
  }

  process.stdout.write(buf);
}

main().catch((e) => {
  process.stderr.write(`render-cli error: ${e.message}\n`);
  process.exit(1);
});
