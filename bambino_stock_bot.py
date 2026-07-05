#!/usr/bin/env python3
"""
bambino_stock_bot.py
---------------------
Vigila la pagina de Windhorn KD (Sage Bambino Plus reacondicionadas) y avisa
por Telegram en cuanto alguna unidad pasa de "agotado" a "disponible".

La tienda es Shopware 6. Cuando una variante no tiene stock, la pagina
muestra literalmente los textos "Product is sold out" / "Currently not
available" junto al indicador visual (el circulo rojo/verde que ves en el
navegador). Este script usa esos mismos textos para decidir si hay stock,
en vez de depender de una clase CSS concreta que podria cambiar con el tema.

Uso:
    python bambino_stock_bot.py                  # comprobacion normal
    python bambino_stock_bot.py --test-telegram   # manda un mensaje de prueba
    python bambino_stock_bot.py --dump-html       # guarda el HTML para inspeccionarlo

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
# CONFIGURACION -- rellena esto o exporta las variables de entorno del mismo
# nombre (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) antes de ejecutar el script.
# --------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PON_AQUI_TU_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PON_AQUI_TU_CHAT_ID")

URL = "https://www.windhornkd.de/en/Refurbished-Devices/SAGE/Refurbish-Devices-EU/Espresso/SES500-Bambino-Plus/"

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "stock_state.json"
DEBUG_HTML_FILE = BASE_DIR / "debug_page.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Frases que usa Shopware cuando una unidad NO tiene stock. Si algun dia
# cambian el texto de la tienda, es aqui donde tocaria ajustarlo.
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
log = logging.getLogger("bambino_bot")


# --------------------------------------------------------------------------
# Descarga y parseo
# --------------------------------------------------------------------------
def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def get_max_page(soup: BeautifulSoup) -> int:
    """Busca enlaces tipo '?p=2' para saber cuantas paginas de resultados hay."""
    max_p = 1
    for a in soup.select("a[href*='?p=']"):
        m = re.search(r"[?&]p=(\d+)", a.get("href", ""))
        if m:
            max_p = max(max_p, int(m.group(1)))
    return max_p


def parse_variants(soup: BeautifulSoup) -> list[dict]:
    """
    Devuelve una lista de variantes: [{"name", "url", "price", "in_stock"}, ...]

    Shopware 6 suele pintar cada tarjeta de producto con la clase 'product-box'.
    Si esta tienda usa un tema distinto y no se encuentra ninguna tarjeta, cae
    a un plan B que trata la pagina entera como un unico bloque (menos preciso,
    pero sigue detectando si "algo" cambio).
    """
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
            name = (img.get("alt") if img else None) or link.get_text(strip=True) or "Bambino Plus"

            price_match = PRICE_RE.search(text)
            price = price_match.group(0) if price_match else "—"

            low = text.lower()
            in_stock = not any(marker in low for marker in OUT_OF_STOCK_MARKERS)

            variants.append({"name": name.strip(), "url": url, "price": price, "in_stock": in_stock})
    else:
        text = soup.get_text(" ", strip=True)
        low = text.lower()
        in_stock = not any(marker in low for marker in OUT_OF_STOCK_MARKERS)
        variants.append({"name": "SES500 Bambino Plus", "url": URL, "price": "—", "in_stock": in_stock})

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
# Estado (para no avisar dos veces de lo mismo)
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
        lines = ["🟢 <b>¡Ha vuelto el stock de la Bambino Plus!</b>", ""]
        for v in newly_available:
            lines.append(f"<b>{v['name']}</b>\n{v['price']}\n{v['url']}\n")
        send_telegram("\n".join(lines))
        log.info("Aviso enviado por Telegram (%d variante/s).", len(newly_available))


def test_telegram() -> None:
    send_telegram("✅ El bot de la Bambino Plus esta bien configurado.")
    log.info("Mensaje de prueba enviado.")


def dump_html() -> None:
    DEBUG_HTML_FILE.write_text(fetch(URL), encoding="utf-8")
    log.info("HTML guardado en %s", DEBUG_HTML_FILE)


def main():
    parser = argparse.ArgumentParser(description="Vigila el stock de la Bambino Plus en Windhorn KD.")
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
