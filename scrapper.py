#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрапер топ-100 компаній світу за капіталізацією.

=====================================================================
1. ДЖЕРЕЛО ДАНИХ (відкрите, статичне):
       https://companiesmarketcap.com/

   Головна сторінка -- це список ПІДРОЗДІЛІВ (топ-100 компаній за
   капіталізацією), кожна компанія має власний URL:
       https://companiesmarketcap.com/{slug}/marketcap/
   На сторінці кожної компанії є список ОБ'ЄКТІВ -- капіталізація
   компанії за кожен рік (таблиця "Market cap history"), а також
   зображення -- іконка (логотип) компанії.
=====================================================================

Відповідність пунктів завдання:
    2. step2_download_and_print()      -- завантаження + друк у консоль + перевірка статичності
    3. parse_top_companies()           -- список компаній+URL -> companies.txt, companies.xml
    4. parse_marketcap_by_year()       -- капіталізація за роками  -> stock_records.txt, stock_records.json
    5. extract_logo_url()/download_*() -- логотипи -> output/images/, images.txt, images.csv
    6. save_to_database()              -- усі дані -> output/stocks_data.db (SQLite)

Запуск:
    pip install requests beautifulsoup4 lxml
    python3 full_scraper.py
"""

import os
import re
import csv
import json
import time
import sqlite3
import logging
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

# ------------------------------------------------------------------------------------
# Налаштування
# ------------------------------------------------------------------------------------

SOURCE_URL = "https://companiesmarketcap.com/"
TOP_N = 100
REQUEST_TIMEOUT = 15
DELAY_BETWEEN_REQUESTS = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 EduScraperBot/1.0"
    )
}

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("full_scraper")

COMPANY_LINK_RE = re.compile(r"^https?://companiesmarketcap\.com/([^/]+)/marketcap/?$")


# ------------------------------------------------------------------------------------
# Моделі даних
# ------------------------------------------------------------------------------------

@dataclass
class Company:
    id: int
    rank: Optional[int]
    name: str
    ticker: str
    marketcap_now: str
    url: str


@dataclass
class StockRecord:
    id: int
    company_id: int
    year: str
    marketcap: str
    change_pct: str


@dataclass
class ImageItem:
    id: int
    company_id: int
    image_url: str
    local_path: Optional[str]


# ------------------------------------------------------------------------------------
# Допоміжна функція завантаження сторінок
# ------------------------------------------------------------------------------------

def fetch_html(url: str, session: requests.Session) -> Optional[str]:
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except requests.RequestException as e:
        log.warning(f"Не вдалося завантажити {url}: {e}")
        return None


# ------------------------------------------------------------------------------------
# КРОК 2. Завантаження сторінки джерела, перевірка статичності, друк у консоль
# ------------------------------------------------------------------------------------

def check_page_is_static(html: str, expected_markers: List[str]) -> bool:
    """
    Якщо очікувані текстові маркери присутні у "сирому" HTML, отриманому
    звичайним requests.get() без виконання JavaScript -- сторінка є
    статичною (рендериться на сервері), а не довантажується скриптами
    в браузері.
    """
    return all(marker in html for marker in expected_markers)


def step2_download_and_print(url: str, session: requests.Session) -> str:
    log.info(f"Крок 2: завантаження сторінки джерела: {url}")
    html = fetch_html(url, session)
    if html is None:
        raise RuntimeError("Не вдалося завантажити сторінку джерела даних.")

    is_static = check_page_is_static(html, ["Market Cap", "Rank"])
    print("=" * 90)
    print(f"URL: {url}")
    print(f"Статус: сторінка {'СТАТИЧНА (SSR/HTML)' if is_static else 'ІМОВІРНО ДИНАМІЧНА (потребує JS)'}")
    print(f"Розмір HTML: {len(html)} символів")
    print("=" * 90)
    print(html[:3000])
    print("\n... [вивід обрізано для консолі; повний HTML -- у змінній 'html'] ...\n")
    print("=" * 90)
    return html


# ------------------------------------------------------------------------------------
# КРОК 3. Список підрозділів (топ-100 компаній) та їх URL -> TXT + XML
# ------------------------------------------------------------------------------------

def parse_top_companies(html: str, base_url: str, top_n: int) -> List[Company]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        log.warning("Таблицю рейтингу не знайдено.")
        return []

    companies: List[Company] = []
    comp_id = 1

    for tr in table.find_all("tr"):
        link = None
        for a in tr.find_all("a", href=True):
            if COMPANY_LINK_RE.match(urljoin(base_url, a["href"])):
                link = a
                break
        if link is None:
            continue  # службовий рядок (заголовок, реклама тощо)

        company_url = urljoin(base_url, link["href"])
        full_text = link.get_text(strip=True)
        parts = full_text.rsplit(" ", 1)
        name, ticker = (parts[0], parts[1]) if len(parts) == 2 else (full_text, "")

        cell_texts = [td.get_text(strip=True) for td in tr.find_all("td")]
        rank = next((int(t) for t in cell_texts if t.isdigit()), None)
        marketcap_now = next((t for t in cell_texts if t.startswith("$")), "")

        companies.append(
            Company(id=comp_id, rank=rank, name=name, ticker=ticker, marketcap_now=marketcap_now, url=company_url)
        )
        comp_id += 1
        if len(companies) >= top_n:
            break

    log.info(f"Знайдено компаній (підрозділів): {len(companies)}")
    return companies


def save_companies_txt(companies: List[Company], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("Список підрозділів (топ-100 компаній за капіталізацією)\n")
        f.write(f"Джерело: {SOURCE_URL}\n")
        f.write("=" * 90 + "\n\n")
        for c in companies:
            f.write(f"[{c.id}] #{c.rank} {c.name} ({c.ticker})  MCap={c.marketcap_now}\n")
            f.write(f"      URL: {c.url}\n")
    log.info(f"Збережено: {path}")


def save_companies_xml(companies: List[Company], path: str) -> None:
    root = Element("companies", attrib={"source": SOURCE_URL})
    for c in companies:
        el = SubElement(root, "company", attrib={"id": str(c.id)})
        SubElement(el, "rank").text = str(c.rank) if c.rank is not None else ""
        SubElement(el, "name").text = c.name
        SubElement(el, "ticker").text = c.ticker
        SubElement(el, "marketcap_now").text = c.marketcap_now
        SubElement(el, "url").text = c.url

    xml_str = minidom.parseString(tostring(root, encoding="utf-8")).toprettyxml(indent="  ")
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml_str)
    log.info(f"Збережено: {path}")


# ------------------------------------------------------------------------------------
# КРОК 4. Капіталізація за роками з кожної сторінки компанії -> TXT + JSON
# ------------------------------------------------------------------------------------

def parse_marketcap_by_year(html: str, company: Company) -> List[StockRecord]:
    """
    Шукає таблицю "Year | Marketcap | Change" на сторінці компанії -- це і є
    "список об'єктів" (пункт 1 завдання) на сторінці підрозділу.
    """
    soup = BeautifulSoup(html, "lxml")
    records: List[StockRecord] = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            first_row = table.find("tr")
            headers = [td.get_text(strip=True).lower() for td in first_row.find_all("td")] if first_row else []

        if headers[:2] == ["year", "marketcap"]:
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                year = cells[0].get_text(strip=True)
                if not re.match(r"^\d{4}$", year):
                    continue
                marketcap = cells[1].get_text(strip=True)
                change = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                records.append(StockRecord(id=0, company_id=company.id, year=year, marketcap=marketcap, change_pct=change))
            break

    return records


def step4_scrape_stock_records(
    companies: List[Company], session: requests.Session, pages_cache: dict, limit: Optional[int] = None
) -> List[StockRecord]:
    """
    pages_cache: {company_id: html} -- заповнюється тут і повторно
    використовується кроком 5, щоб не завантажувати сторінку компанії двічі.
    """
    all_records: List[StockRecord] = []
    rec_id = 1
    subset = companies[:limit] if limit else companies

    for c in subset:
        log.info(f"Компанія [{c.id}] {c.name} ({c.ticker}) -> {c.url}")
        html = fetch_html(c.url, session)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        if html is None:
            log.warning(f"  Пропущено (сторінка не завантажилась): {c.url}")
            continue
        pages_cache[c.id] = html

        records = parse_marketcap_by_year(html, c)
        for r in records:
            r.id = rec_id
            rec_id += 1
        log.info(f"  Знайдено річних записів капіталізації: {len(records)}")
        all_records.extend(records)

    return all_records


def save_stock_records_txt(records: List[StockRecord], companies: List[Company], path: str) -> None:
    comp_by_id = {c.id: c for c in companies}
    with open(path, "w", encoding="utf-8") as f:
        f.write("Капіталізація компаній за роками (результат скрапінгу)\n")
        f.write("=" * 90 + "\n\n")
        current = None
        for r in records:
            if r.company_id != current:
                current = r.company_id
                c = comp_by_id.get(current)
                header = f"{c.name} ({c.ticker})" if c else f"Компанія #{current}"
                f.write(f"\n{header}  ({c.url if c else ''})\n" + "-" * 60 + "\n")
            f.write(f"  {r.year}: {r.marketcap}  ({r.change_pct})\n")
    log.info(f"Збережено: {path}")


def save_stock_records_json(records: List[StockRecord], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in records], f, ensure_ascii=False, indent=2)
    log.info(f"Збережено: {path}")


# ------------------------------------------------------------------------------------
# КРОК 5. Зображення (іконки компаній) -> окрема папка + TXT + CSV
# ------------------------------------------------------------------------------------

def extract_logo_url(html: str, page_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "lxml")
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src and "company-logos" in src:
            return urljoin(page_url, src)
    return None


def download_image(url: str, dest_dir: str, filename: str, session: requests.Session) -> Optional[str]:
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"  Не вдалося завантажити зображення {url}: {e}")
        return None

    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    try:
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return dest_path
    except OSError as e:
        log.warning(f"  Не вдалося зберегти файл {dest_path}: {e}")
        return None


def step5_scrape_images(
    companies: List[Company], session: requests.Session, images_dir: str, pages_cache: dict, limit: Optional[int] = None
) -> List[ImageItem]:
    os.makedirs(images_dir, exist_ok=True)
    all_images: List[ImageItem] = []
    img_id = 1
    subset = companies[:limit] if limit else companies

    for c in subset:
        # використовуємо вже завантажену на кроці 4 сторінку (з кешу), а не тягнемо її знову
        html = pages_cache.get(c.id)
        if html is None:
            html = fetch_html(c.url, session)
            time.sleep(DELAY_BETWEEN_REQUESTS)
        if html is None:
            continue

        logo_url = extract_logo_url(html, c.url)
        if logo_url is None:
            log.warning(f"  Іконку не знайдено для {c.name}")
            continue

        ext = os.path.splitext(urlparse(logo_url).path)[1] or ".png"
        filename = f"{(c.ticker or c.name).replace(' ', '_').replace('/', '_')}{ext}"
        local_path = download_image(logo_url, images_dir, filename, session)

        all_images.append(ImageItem(id=img_id, company_id=c.id, image_url=logo_url, local_path=local_path))
        img_id += 1

    return all_images


def save_images_txt(images: List[ImageItem], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("Список зображень (іконок компаній)\n")
        f.write("=" * 90 + "\n\n")
        for im in images:
            f.write(f"[{im.id}] company={im.company_id}  {im.image_url}  ->  {im.local_path}\n")
    log.info(f"Збережено: {path}")


def save_images_csv(images: List[ImageItem], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "company_id", "image_url", "local_path"])
        for im in images:
            writer.writerow([im.id, im.company_id, im.image_url, im.local_path or ""])
    log.info(f"Збережено: {path}")


# ------------------------------------------------------------------------------------
# КРОК 6. Збереження усіх даних до бази даних (SQLite)
# ------------------------------------------------------------------------------------

def save_to_database(
    companies: List[Company], records: List[StockRecord], images: List[ImageItem], db_path: str
) -> None:
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute(
        """CREATE TABLE companies (
            id INTEGER PRIMARY KEY, rank INTEGER, name TEXT NOT NULL,
            ticker TEXT, marketcap_now TEXT, url TEXT NOT NULL)"""
    )
    cur.execute(
        """CREATE TABLE stock_records (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL,
            year TEXT NOT NULL, marketcap TEXT, change_pct TEXT,
            FOREIGN KEY (company_id) REFERENCES companies(id))"""
    )
    cur.execute(
        """CREATE TABLE images (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL,
            image_url TEXT NOT NULL, local_path TEXT,
            FOREIGN KEY (company_id) REFERENCES companies(id))"""
    )

    cur.executemany(
        "INSERT INTO companies (id, rank, name, ticker, marketcap_now, url) VALUES (?, ?, ?, ?, ?, ?)",
        [(c.id, c.rank, c.name, c.ticker, c.marketcap_now, c.url) for c in companies],
    )
    cur.executemany(
        "INSERT INTO stock_records (id, company_id, year, marketcap, change_pct) VALUES (?, ?, ?, ?, ?)",
        [(r.id, r.company_id, r.year, r.marketcap, r.change_pct) for r in records],
    )
    cur.executemany(
        "INSERT INTO images (id, company_id, image_url, local_path) VALUES (?, ?, ?, ?)",
        [(i.id, i.company_id, i.image_url, i.local_path) for i in images],
    )

    conn.commit()
    conn.close()
    log.info(f"Збережено базу даних: {db_path}")


# ------------------------------------------------------------------------------------
# Головна функція
# ------------------------------------------------------------------------------------

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    session = requests.Session()

    # Крок 2
    html = step2_download_and_print(SOURCE_URL, session)

    # Крок 3
    companies = parse_top_companies(html, SOURCE_URL, TOP_N)
    save_companies_txt(companies, os.path.join(OUTPUT_DIR, "companies.txt"))
    save_companies_xml(companies, os.path.join(OUTPUT_DIR, "companies.xml"))

    # Кеш HTML-сторінок компаній, щоб крок 5 не завантажував їх повторно
    pages_cache: dict = {}

    # Крок 4
    records = step4_scrape_stock_records(companies, session, pages_cache)
    save_stock_records_txt(records, companies, os.path.join(OUTPUT_DIR, "stock_records.txt"))
    save_stock_records_json(records, os.path.join(OUTPUT_DIR, "stock_records.json"))

    # Крок 5
    images = step5_scrape_images(companies, session, IMAGES_DIR, pages_cache)
    save_images_txt(images, os.path.join(OUTPUT_DIR, "images.txt"))
    save_images_csv(images, os.path.join(OUTPUT_DIR, "images.csv"))

    # Крок 6
    save_to_database(companies, records, images, os.path.join(OUTPUT_DIR, "stocks_data.db"))

    log.info("Готово. Усі результати -- у папці 'output/'.")


if __name__ == "__main__":
    main()