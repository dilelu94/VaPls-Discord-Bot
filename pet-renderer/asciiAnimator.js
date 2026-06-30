// asciiAnimator.js — Genera frames ASCII animados para una mascota
// Los frames pueden renderizarse como GIF o enviarse como texto.

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Genera frames { ascii, delayMs } a partir del objeto pet
function generateFrames(pet) {
  const base = pet.ascii;
  const lines = base.split("\n");
  const frames = [];
  const [lEye, rEye] = pet.parts.eyes.s;

  function pushFrame(ascii, delayMs) {
    frames.push({ ascii, delayMs });
  }

  // ── helpers ──────────────────────────────────────────────────────────────
  function replaceEyes(ascii, rep) {
    let s = ascii;
    s = s.replace(new RegExp(escapeRegex(lEye), "g"), rep);
    s = s.replace(new RegExp(escapeRegex(rEye), "g"), rep);
    return s;
  }

  // ── idle ─────────────────────────────────────────────────────────────────
  pushFrame(base, 2000);

  // ── blink: open → closed → open ─────────────────────────────────────────
  const closed = replaceEyes(base, "-");
  pushFrame(closed, 100);
  pushFrame(base, 1500);

  // ── idle ─────────────────────────────────────────────────────────────────
  pushFrame(base, 1500);

  // ── breathe: expand → contract → normal ──────────────────────────────────
  if (lines.length >= 3) {
    const mid = Math.floor(lines.length / 2);

    const expanded = [...lines];
    expanded.splice(mid, 0, " ");
    pushFrame(expanded.join("\n"), 600);

    if (lines.length > 2) {
      const contracted = [...expanded];
      contracted.splice(mid, 1);
      pushFrame(contracted.join("\n"), 600);
    }
    pushFrame(base, 1200);
  }

  // ── float: up → down → normal ────────────────────────────────────────────
  frames.push({ ascii: base, delayMs: 300, yOffset: -10 });
  frames.push({ ascii: base, delayMs: 300, yOffset: 10 });
  pushFrame(base, 1000);

  // ── particles for legendary/epic ─────────────────────────────────────────
  if (pet.rarity === "legendario" || pet.rarity === "épico") {
    for (let i = 0; i < 3; i++) {
      const spark = i % 2 === 0 ? " ✦" : " ✨";
      pushFrame(base + "\n " + spark, 300);
    }
    pushFrame(base, 1000);
  }

  return frames;
}

module.exports = { generateFrames };
