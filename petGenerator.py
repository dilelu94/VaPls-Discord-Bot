"""Procedural fantasy pet generator — deterministic ASCII pets from a seed.

Port of petGenerator.js.  Same seed always produces the same creature.
"""

from __future__ import annotations

import json
import logging
import os
import time

import config

log = logging.getLogger("petGenerator")

# ── Seeded RNG (mulberry32, JS‑compatible) ──────────────────────────────────

def str_to_seed(s: str) -> int:
    h = 0
    for ch in s:
        h = ((31 * h + ord(ch)) & 0xFFFFFFFF)
        if h >= 0x80000000:
            h -= 0x100000000
    return h & 0xFFFFFFFF


def _s32(x: int) -> int:
    x = x & 0xFFFFFFFF
    if x >= 0x80000000:
        x -= 0x100000000
    return x


def _imul(a: int, b: int) -> int:
    return _s32((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF))


def mulberry32(seed: int):
    """Generator yielding [0,1) floats, matching JS mulberry32."""
    seed = _s32(seed)
    while True:
        seed = _s32(seed + 0x6d2b79f5)
        a = seed ^ ((seed & 0xFFFFFFFF) >> 15)
        t = _imul(a, 1 | seed)
        old_t = t
        a2 = t ^ ((t & 0xFFFFFFFF) >> 7)
        t = _s32(old_t + _imul(a2, 61 | old_t))
        t = _s32(t ^ old_t)
        yield (_s32(t ^ ((t & 0xFFFFFFFF) >> 14)) & 0xFFFFFFFF) / 4294967296.0


# ── Part data ────────────────────────────────────────────────────────────────

PARTS = {
    "ears": [
        {"s": ["∧", "∧"], "name": "orejas puntiagudas", "r": 1},
        {"s": ["≋", "≋"], "name": "orejas de aleta", "r": 2},
        {"s": ["╤", "╤"], "name": "cuernos cortos", "r": 2},
        {"s": ["╥", "╥"], "name": "cuernos dobles", "r": 3},
        {"s": ["Ψ", " "], "name": "tridente izquierdo", "r": 4},
        {"s": ["Ω", "Ω"], "name": "cuernos espiral", "r": 4},
        {"s": ["~", "~"], "name": "antenas", "r": 2},
        {"s": ["§", "§"], "name": "tentáculos cefálicos", "r": 3},
    ],
    "eyes": [
        {"s": ["·", "·"], "name": "puntitos", "r": 1},
        {"s": [">", "<"], "name": "entrecejos", "r": 1},
        {"s": ["o", "o"], "name": "redondos", "r": 1},
        {"s": ["@", "@"], "name": "espirales", "r": 2},
        {"s": ["◈", "◈"], "name": "cristal", "r": 3},
        {"s": ["Θ", "Θ"], "name": "divinos", "r": 4},
        {"s": ["×", "×"], "name": "calavera", "r": 2},
        {"s": ["◉", "◉"], "name": "objetivo", "r": 3},
        {"s": ["♦", "♦"], "name": "gema", "r": 3},
    ],
    "mouth": [
        {"s": "_", "name": "línea", "r": 1},
        {"s": "ω", "name": "feliz", "r": 1},
        {"s": "∇", "name": "triangular", "r": 2},
        {"s": "≋", "name": "tentáculo", "r": 3},
        {"s": "⊂⊃", "name": "pinzas", "r": 3},
        {"s": "◇", "name": "gema", "r": 4},
        {"s": "∞", "name": "infinito", "r": 4},
        {"s": "v", "name": "colmillos", "r": 2},
    ],
    "body": [
        {"template": lambda el, er, ey, em: f"  {el}   {er}  \n ({ey[0]} {em} {ey[1]}) \n  |   |  \n  \\___/  ", "name": "redondo", "r": 1},
        {"template": lambda el, er, ey, em: f" {el}     {er} \n[{ey[0]}  {em}  {ey[1]}]\n |     | \n \\____/ ", "name": "bloque", "r": 2},
        {"template": lambda el, er, ey, em: f"  {el} {er}  \n< {ey[0]} {em} {ey[1]} >\n  |   |  \n  |   |  ", "name": "angular", "r": 2},
        {"template": lambda el, er, ey, em: f"{el}       {er}\n({ey[0]}  {em}  {ey[1]})\n /|   |\\ \n/ |___| \\", "name": "ancho", "r": 2},
        {"template": lambda el, er, ey, em: f"  {el} {er}  \n{{{ey[0]} {em} {ey[1]}}}\n  )   (  \n (_____)  ", "name": "blob", "r": 3},
        {"template": lambda el, er, ey, em: f" {el}   {er} \n╔{ey[0]}═{em}═{ey[1]}╗\n║  ◆  ║\n╚═════╝", "name": "meca", "r": 4},
    ],
    "legs": [
        {"s": "∪  ∪", "name": "redondeadas", "r": 1},
        {"s": "|  |", "name": "rectas", "r": 1},
        {"s": "/  \\", "name": "abiertas", "r": 1},
        {"s": "≈  ≈", "name": "onduladas", "r": 2},
        {"s": "§  §", "name": "enroscadas", "r": 3},
        {"s": "⌇  ⌇", "name": "tentáculos", "r": 3},
        {"s": "", "name": "sin patas", "r": 2},
    ],
    "accessory": [
        {"s": "", "name": "nada", "r": 1},
        {"s": "✦ ", "name": "brillo", "r": 2},
        {"s": "~ ", "name": "aura", "r": 2},
        {"s": "⚡ ", "name": "rayo", "r": 3},
        {"s": "♾ ", "name": "infinito", "r": 3},
        {"s": "☄ ", "name": "cometa", "r": 3},
        {"s": "✨✨", "name": "destellos", "r": 4},
        {"s": "👁 ", "name": "ojo etéreo", "r": 4},
    ],
}

PREFIXES = ["Zor", "Myx", "Vel", "Dra", "Nar", "Thyx", "Qua", "Blix", "Fen", "Orb", "Aex", "Gly"]
SUFFIXES = ["oth", "ixis", "ara", "mund", "elos", "ynn", "ath", "ovar", "rix", "uun", "esk", "orn"]
EPITHETS = ["del Abismo", "Eterno", "de Cristal", "Sombrío", "del Vacío", "Arcano", "Primordial", "de las Estrellas"]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _rarity_weight(item: dict) -> int:
    return 5 - item["r"]


def pick(arr: list, rng, weight_fn=None):
    if weight_fn is None:
        return arr[int(next(rng) * len(arr))]
    weights = [weight_fn(item) for item in arr]
    total = sum(weights)
    r = next(rng) * total
    for i, w in enumerate(weights):
        r -= w
        if r <= 0:
            return arr[i]
    return arr[-1]


# ── Generation ───────────────────────────────────────────────────────────────

def calc_rarity(parts: dict) -> str:
    total = (
        parts["ear"]["r"]
        + parts["eyes"]["r"]
        + parts["mouth"]["r"]
        + parts["body"]["r"]
        + parts["legs"]["r"]
        + parts["acc"]["r"]
    )
    if total >= 18:
        return "legendario"
    if total >= 14:
        return "épico"
    if total >= 10:
        return "raro"
    return "común"


def generate_pet(seed: int) -> dict:
    rng = mulberry32(seed)

    ear = pick(PARTS["ears"], rng, _rarity_weight)
    eyes = pick(PARTS["eyes"], rng, _rarity_weight)
    mouth = pick(PARTS["mouth"], rng, _rarity_weight)
    body = pick(PARTS["body"], rng, _rarity_weight)
    legs = pick(PARTS["legs"], rng, _rarity_weight)
    acc = pick(PARTS["accessory"], rng, _rarity_weight)

    ascii_art = body["template"](ear["s"][0], ear["s"][1], eyes["s"], mouth["s"])
    if legs["s"]:
        ascii_art += "\n  " + legs["s"] + "   "
    if acc["s"]:
        ascii_art += "\n  " + acc["s"]

    name = (
        PREFIXES[int(next(rng) * len(PREFIXES))]
        + SUFFIXES[int(next(rng) * len(SUFFIXES))]
        + " "
        + EPITHETS[int(next(rng) * len(EPITHETS))]
    )

    parts = {
        "ear": {"name": ear["name"], "r": ear["r"]},
        "eyes": {"name": eyes["name"], "r": eyes["r"], "s": eyes["s"]},
        "mouth": {"name": mouth["name"], "r": mouth["r"]},
        "body": {"name": body["name"], "r": body["r"]},
        "legs": {"name": legs["name"], "r": legs["r"]},
        "acc": {"name": acc["name"], "r": acc["r"]},
    }

    return {
        "seed": seed,
        "ascii": ascii_art,
        "name": name,
        "rarity": calc_rarity(parts),
        "parts": parts,
        "stats": {
            "atk": int(next(rng) * 99) + 1,
            "def": int(next(rng) * 99) + 1,
            "mag": int(next(rng) * 99) + 1,
            "spd": int(next(rng) * 99) + 1,
        },
    }


# ── Persistence (data/pets.json) ─────────────────────────────────────────────

_PETS_PATH = config.PETS_PATH


def _load_pets() -> dict:
    path = _PETS_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pets(data: dict) -> None:
    path = _PETS_PATH
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def save_pet(user_id: str, pet: dict) -> None:
    pets = _load_pets()
    pets[user_id] = pet
    _save_pets(pets)


def get_or_create_pet(user_id: str) -> dict:
    pets = _load_pets()
    if user_id in pets:
        _backfill_missing_eye_character(pets[user_id]["parts"])
        return pets[user_id]
    seed = str_to_seed(user_id)
    pet = generate_pet(seed)
    pet["original_seed"] = seed
    pet["evolution_level"] = 0
    pet["user_id"] = user_id
    pet["created_at"] = time.time()
    pets[user_id] = pet
    _save_pets(pets)
    log.info("Created pet for user=%s seed=%s rarity=%s", user_id, seed, pet["rarity"])
    return pet


def get_pet(user_id: str) -> dict | None:
    pets = _load_pets()
    pet = pets.get(user_id)
    if pet:
        _backfill_missing_eye_character(pet["parts"])
    return pet


def _rarity_sum(parts: dict) -> int:
    return (
        parts["ear"]["r"]
        + parts["eyes"]["r"]
        + parts["mouth"]["r"]
        + parts["body"]["r"]
        + parts["legs"]["r"]
        + parts["acc"]["r"]
    )



def _backfill_missing_eye_character(parts: dict) -> None:
    if "s" in parts.get("eyes", {}):
        return
    for eye_entry in PARTS["eyes"]:
        if eye_entry["name"] == parts["eyes"]["name"]:
            parts["eyes"]["s"] = eye_entry["s"]
            return
    log.warning("Could not backfill eyes.s for name=%s", parts.get("eyes", {}).get("name"))

def derive_evolution_seed(original_seed: int, level: int) -> int:
    return (original_seed * 6364136223 + level) & 0xFFFFFFFF


def evolve_pet(pet: dict) -> dict:
    original_seed = pet.get("original_seed", pet["seed"])
    current_level = pet.get("evolution_level", 0)
    current_sum = _rarity_sum(pet["parts"])
    level = current_level + 1
    while True:
        cand_seed = derive_evolution_seed(original_seed, level)
        cand = generate_pet(cand_seed)
        cand_sum = _rarity_sum(cand["parts"])
        if cand_sum > current_sum:
            cand["original_seed"] = original_seed
            cand["evolution_level"] = level
            cand["user_id"] = pet.get("user_id", "")
            cand["created_at"] = pet.get("created_at", time.time())
            log.info(
                "Evolved pet uid=%s lvl=%s->%s seed=%s rarity=%s->%s",
                pet.get("user_id", "?"), current_level, level,
                original_seed, pet.get("rarity", "?"), cand["rarity"],
            )
            return cand
        level += 1


def revert_pet(pet: dict) -> dict | None:
    current_level = pet.get("evolution_level", 0)
    if current_level <= 0:
        return None
    new_level = current_level - 1
    original_seed = pet.get("original_seed", pet["seed"])
    if new_level == 0:
        cand = generate_pet(original_seed)
    else:
        cand_seed = derive_evolution_seed(original_seed, new_level)
        cand = generate_pet(cand_seed)
    cand["original_seed"] = original_seed
    cand["evolution_level"] = new_level
    cand["user_id"] = pet.get("user_id", "")
    cand["created_at"] = pet.get("created_at", time.time())
    log.info(
        "Reverted pet uid=%s lvl=%s->%s seed=%s",
        pet.get("user_id", "?"), current_level, new_level, original_seed,
    )
    return cand


def rebuild_evolution_chain(original_seed: int, up_to_level: int) -> list[dict]:
    chain = []
    for level in range(up_to_level + 1):
        if level == 0:
            pet = generate_pet(original_seed)
        else:
            s = derive_evolution_seed(original_seed, level)
            pet = generate_pet(s)
        chain.append({
            "level": level,
            "seed": pet["seed"],
            "name": pet["name"],
            "rarity": pet["rarity"],
            "ascii": pet["ascii"],
            "parts": pet["parts"],
            "stats": pet["stats"],
        })
    return chain


def format_name(name: str, rarity: str) -> str:
    if rarity == "raro":
        return f"~ {name} ~"
    if rarity == "épico":
        return f"·:·{name}·:·"
    if rarity == "legendario":
        return f"═══〔 {name.upper()} 〕═══"
    return name
