// petRenderer.js — Renderiza mascota ASCII como PNG o GIF animado
const { createCanvas } = require("canvas");
const GIFEncoder = require("gifencoder");

const THEME = {
  común: { bg: "#1a1a2e", text: "#c9d1d9", accent: "#8b949e", border: false },
  raro: { bg: "#0d2137", text: "#cdd6f4", accent: "#58a6ff", border: false },
  épico: { bg: "#1a0d37", text: "#e2d9f3", accent: "#bc8cff", border: true },
  legendario: {
    bg: "#2a1000",
    text: "#ffd6b0",
    accent: "#f0883e",
    border: true,
  },
};

const FONT_SIZE = 18;
const LINE_H = 26;
const PAD_X = 32;
const PAD_Y = 28;
const FONT_FACE = `${FONT_SIZE}px 'Courier New', Courier, monospace`;
const NAME_FONT = `bold 14px 'Courier New', Courier, monospace`;

function measureLines(lines) {
  const tmp = createCanvas(1, 1);
  const c = tmp.getContext("2d");
  c.font = FONT_FACE;
  return Math.max(...lines.map((l) => c.measureText(l).width));
}

function drawFrame(ctx, W, H, theme, lines, formattedName, rarity, yOffset = 0) {
  ctx.fillStyle = theme.bg;
  ctx.fillRect(0, 0, W, H);

  if (theme.border) {
    ctx.strokeStyle = theme.accent;
    ctx.lineWidth = 2;
    ctx.strokeRect(6, 6, W - 12, H - 12);
    if (rarity === "legendario") {
      ctx.lineWidth = 1;
      ctx.strokeRect(10, 10, W - 20, H - 20);
    }
  }

  ctx.textAlign = "center";

  ctx.font = NAME_FONT;
  ctx.fillStyle = theme.accent;
  ctx.fillText(formattedName, W / 2, PAD_Y + FONT_SIZE);

  const sepY = PAD_Y + FONT_SIZE + 14;
  ctx.strokeStyle = theme.accent + "55";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(PAD_X, sepY);
  ctx.lineTo(W - PAD_X, sepY);
  ctx.stroke();

  ctx.font = FONT_FACE;
  ctx.fillStyle = theme.text;
  const petStartY = sepY + LINE_H;

  lines.forEach((line, i) => {
    ctx.fillText(line, W / 2, petStartY + i * LINE_H + yOffset);
  });
}

function renderPetImage(pet, formattedName) {
  const theme = THEME[pet.rarity] ?? THEME["común"];
  const lines = pet.ascii.split("\n");
  const allText = [...lines, "", formattedName];

  const maxWidth = measureLines(allText);
  const W = Math.ceil(maxWidth) + PAD_X * 2;
  const H = allText.length * LINE_H + PAD_Y * 2 + 20;

  const canvas = createCanvas(W, H);
  const ctx = canvas.getContext("2d");
  drawFrame(ctx, W, H, theme, lines, formattedName, pet.rarity);

  return canvas.toBuffer("image/png");
}

function _maxFrameSize(frames, formattedName) {
  let maxW = 0,
    maxH = 0;
  for (const frame of frames) {
    const lines = frame.ascii.split("\n");
    const all = [...lines, "", formattedName];
    const w = Math.ceil(measureLines(all)) + PAD_X * 2;
    const h = all.length * LINE_H + PAD_Y * 2 + 20;
    if (w > maxW) maxW = w;
    if (h > maxH) maxH = h;
  }
  return { W: maxW, H: maxH };
}

async function renderPetGif(pet, formattedName, frames) {
  const theme = THEME[pet.rarity] ?? THEME["común"];
  const { W, H } = _maxFrameSize(frames, formattedName);

  const encoder = new GIFEncoder(W, H);
  const stream = encoder.createReadStream();
  const chunks = [];

  stream.on("data", (chunk) => chunks.push(chunk));

  return new Promise((resolve, reject) => {
    stream.on("end", () => resolve(Buffer.concat(chunks)));
    stream.on("error", reject);

    encoder.start();
    encoder.setRepeat(0);
    encoder.setQuality(10);

    for (const frame of frames) {
      const canvas = createCanvas(W, H);
      const ctx = canvas.getContext("2d");
      const fLines = frame.ascii.split("\n");
      drawFrame(ctx, W, H, theme, fLines, formattedName, pet.rarity, frame.yOffset || 0);
      encoder.setDelay(frame.delayMs);
      encoder.addFrame(ctx);
    }

    encoder.finish();
  });
}

module.exports = { renderPetImage, renderPetGif };
