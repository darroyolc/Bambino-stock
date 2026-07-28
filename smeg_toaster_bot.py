#!/usr/bin/env python3
"""
smeg_toaster_bot.py
---------------------
Vigila la pagina de Windhorn KD (tostadoras SMEG reacondicionadas) y avisa
por Telegram en cuanto alguna unidad pasa de "agotado" a "disponible".

Mismo enfoque que bambino_stock_bot.py: busca los textos "Product is sold
out" / "Currently not available" en cada tarjeta de producto para decidir
si hay stock.

Uso:
    python smeg_toaster_bot.py                  # comprobacion normal
    python smeg_toaster_bot.py --test-telegram   # manda un mensaje de prueba
    python smeg_toaster_bot.py --dump-html       # guarda el HTML para inspeccionarlo

Dependencias:
    pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# CONFIGURACION -- reutiliza el MISMO bot/chat de Telegram que la Bambino Plus.
# --------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PON_AQUI_TU_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PON_AQUI_TU_CHAT_ID")

URL = "https://www.windhornkd.de/en/Refurbished-Devices/SMEG/Toaster/"
PRODUCT_LABEL = "SMEG Toaster"

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "toaster_state.json"
DEBUG_HTML_FILE = BASE_DIR / "debug_page_toaster.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

OUT_OF_STOCK_MARKERS = [
    "sold out",
    "currently not available",
    "ausverkauft",
    "nicht verfügbar",
]

PRICE_RE = re.compile(r"€\s?[\d.,]+")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("toaster_bot")


# --------------------------------------------------------------------------
# Descarga y parseo (identico a bambino_stock_bot.py)
# --------------------------------------------------------------------------
def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def get_max_page(soup: BeautifulSoup) -> int:
    max_p = 1
    for a in soup.select("a[href*='?p=']"):
        m = re.search(r"[?&]p=(\d+)", a.get("href", ""))
        if m:
            max_p = max(max_p, int(m.group(1)))
    return max_p


def parse_variants(soup: BeautifulSoup) -> list[dict]:
    cards = soup.select(".product-box") or soup.select("[class*='product-box']")
    variants = []

    if cards:
        for card in cards:
            link = card.select_one("a[href]")
            if not link:
                continue
            text = card.get_text(" ", strip=True)
            url = urljoin(URL, link["href"])

            img = card.select_one("img[alt]")
            name = (img.get("alt") if img else None) or link.get_text(strip=True) or PRODUCT_LABEL

            price_match = PRICE_RE.search(text)
            price = price_match.group(0) if price_match else "—"

            low = text.lower()
            in_stock = not any(marker in low for marker in OUT_OF_STOCK_MARKERS)

            variants.append({"name": name.strip(), "url": url, "price": price, "in_stock": in_stock})
    else:
        text = soup.get_text(" ", strip=True)
        low = text.lower()
        in_stock = not any(marker in low for marker in OUT_OF_STOCK_MARKERS)
        variants.append({"name": PRODUCT_LABEL, "url": URL, "price": "—", "in_stock": in_stock})

    return variants


def fetch_all_variants() -> list[dict]:
    html = fetch(URL)
    soup = BeautifulSoup(html, "html.parser")
    all_variants = parse_variants(soup)

    for page in range(2, get_max_page(soup) + 1):
        page_soup = BeautifulSoup(fetch(f"{URL}?p={page}"), "html.parser")
        all_variants.extend(parse_variants(page_soup))

    return all_variants


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def send_telegram(text: str) -> None:
    if "PON_AQUI" in TELEGRAM_BOT_TOKEN or "PON_AQUI" in TELEGRAM_CHAT_ID:
        log.error("Falta configurar TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        api_url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    resp.raise_for_status()


# --------------------------------------------------------------------------
# Logica principal
# --------------------------------------------------------------------------
def check_stock() -> None:
    try:
        variants = fetch_all_variants()
    except requests.RequestException as e:
        log.error("No se pudo descargar la pagina: %s", e)
        return

    previous = load_state()
    new_state = {}
    newly_available = []

    for v in variants:
        key = v["url"]
        new_state[key] = v["in_stock"]
        if v["in_stock"] and not previous.get(key, False):
            newly_available.append(v)

    save_state(new_state)

    in_stock_now = sum(1 for v in variants if v["in_stock"])
    log.info("Comprobado: %d variantes, %d disponibles.", len(variants), in_stock_now)

    if newly_available:
        lines = [f"🟢 <b>¡Ha vuelto el stock de {PRODUCT_LABEL}!</b>", ""]
        for v in newly_available:
            lines.append(f"<b>{v['name']}</b>\n{v['price']}\n{v['url']}\n")
        send_telegram("\n".join(lines))
        log.info("Aviso enviado por Telegram (%d variante/s).", len(newly_available))


def test_telegram() -> None:
    send_telegram(f"✅ El bot de {PRODUCT_LABEL} esta bien configurado.")
    log.info("Mensaje de prueba enviado.")


def dump_html() -> None:
    DEBUG_HTML_FILE.write_text(fetch(URL), encoding="utf-8")
    log.info("HTML guardado en %s", DEBUG_HTML_FILE)


def main():
    parser = argparse.ArgumentParser(description=f"Vigila el stock de {PRODUCT_LABEL} en Windhorn KD.")
    parser.add_argument("--test-telegram", action="store_true", help="Envia un mensaje de prueba y sale.")
    parser.add_argument("--dump-html", action="store_true", help="Guarda el HTML de la pagina para inspeccionarlo.")
    args = parser.parse_args()

    if args.test_telegram:
        test_telegram()
    elif args.dump_html:
        dump_html()
    else:
        check_stock()


if __name__ == "__main__":
    main()
