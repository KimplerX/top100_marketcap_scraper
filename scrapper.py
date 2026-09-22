#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрапер капіталізації топ-100 компаній світу за роками + іконки компаній.

Джерело: https://companiesmarketcap.com/  (перша сторінка рейтингу = топ-100
компаній за поточною капіталізацією; сайт статичний, серверно-рендерений).

Алгоритм:
    1. Завантажуємо головну сторінку рейтингу -> отримуємо топ-100 компаній
       (назва, тикер, поточна капіталізація, URL сторінки компанії).
    2. Для кожної компанії переходимо на її сторінку
       (https://companiesmarketcap.com/{slug}/marketcap/) і парсимо таблицю
       "Market cap history" -- капіталізацію компанії за кожен рік.
    3. На тій же сторінці компанії знаходимо URL її іконки (логотипу) і
       завантажуємо саме зображення у папку logos/ (один файл на компанію).
    4. Зберігаємо результат у CSV та JSON. У КОЖНОМУ JSON-записі (кожен рік
       капіталізації) є посилання на іконку компанії: і оригінальний URL
       (image_url), і шлях до вже завантаженого локального файлу
       (image_local_path).

Запуск:
    pip install requests beautifulsoup4 lxml
    python3 top100_marketcap_scraper.py
"""

import os
import re
import csv
import json
import time
import logging
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------------------------
# Налаштування
# ------------------------------------------------------------------------------------

SOURCE_URL = "https://companiesmarketcap.com/"
TOP_N = 100                     # скільки компаній беремо з головної сторінки
REQUEST_TIMEOUT = 15
DELAY_BETWEEN_REQUESTS = 1.0    # секунда -- ввічливий скрапінг

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 EduScraperBot/1.0"
    )
}

OUTPUT_CSV = "top100_marketcap_by_year.csv"
OUTPUT_JSON = "top100_marketcap_by_year.json"

IMAGES_DIR = "logos"    # папка, куди зберігаються завантажені іконки компаній

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("top100_scraper")

COMPANY_LINK_RE = re.compile(r"^https?://companiesmarketcap\.com/([^/]+)/marketcap/?$")


# ------------------------------------------------------------------------------------
# Моделі даних
# ------------------------------------------------------------------------------------

@dataclass
class Company:
    rank: Optional[int]
    name: str
    ticker: str
    marketcap_now: str
    url: str


@dataclass
class MarketcapYear:
    company_name: str
    ticker: str
    year: str
    marketcap: str
    change_pct: str
    image_url: str = ""           # посилання на іконку компанії (оригінальний URL)
    image_local_path: str = ""    # шлях до вже завантаженого локального файлу іконки


# ------------------------------------------------------------------------------------
# Допоміжні функції завантаження сторінок
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
# Крок 1. Парсинг топ-100 компаній з головної сторінки
# ------------------------------------------------------------------------------------

def parse_top_companies(html: str, base_url: str, top_n: int) -> List[Company]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        log.warning("Таблицю рейтингу не знайдено.")
        return []

    companies: List[Company] = []

    for tr in table.find_all("tr"):
        link = None
        for a in tr.find_all("a", href=True):
            if COMPANY_LINK_RE.match(urljoin(base_url, a["href"])):
                link = a
                break
        if link is None:
            continue  # службовий рядок (заголовок таблиці, реклама тощо)

        company_url = urljoin(base_url, link["href"])
        full_text = link.get_text(strip=True)          # напр. "NVIDIA NVDA"
        parts = full_text.rsplit(" ", 1)
        name, ticker = (parts[0], parts[1]) if len(parts) == 2 else (full_text, "")

        cell_texts = [td.get_text(strip=True) for td in tr.find_all("td")]
        rank = next((int(t) for t in cell_texts if t.isdigit()), None)
        marketcap_now = next((t for t in cell_texts if t.startswith("$")), "")

        companies.append(
            Company(rank=rank, name=name, ticker=ticker, marketcap_now=marketcap_now, url=company_url)
        )

        if len(companies) >= top_n:
            break

    log.info(f"Знайдено компаній у списку: {len(companies)}")
    return companies


# ------------------------------------------------------------------------------------
# Крок 2. Парсинг капіталізації за роками зі сторінки конкретної компанії
# ------------------------------------------------------------------------------------

def parse_marketcap_by_year(html: str, company: Company) -> List[MarketcapYear]:
    """
    Шукає на сторінці компанії таблицю "Market cap history" зі стовпцями
    Year | Marketcap | Change і повертає список записів (рік -> капіталізація).
    Поля image_url / image_local_path тут ще порожні -- їх заповнює main()
    після виклику download_company_logo() (див. нижче), щоб не завантажувати
    сторінку компанії двічі.
    """
    soup = BeautifulSoup(html, "lxml")
    results: List[MarketcapYear] = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            first_row = table.find("tr")
            headers = [td.get_text(strip=True).lower() for td in first_row.find_all("td")] if first_row else []

        # шукаємо саме таблицю "Year / Marketcap / ..." (не таблицю конкурентів)
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
                results.append(
                    MarketcapYear(
                        company_name=company.name,
                        ticker=company.ticker,
                        year=year,
                        marketcap=marketcap,
                        change_pct=change,
                    )
                )
            break

    return results


# ------------------------------------------------------------------------------------
# Крок 3. Іконка (логотип) компанії -- пошук URL і завантаження у файл
# ------------------------------------------------------------------------------------

def extract_logo_url(html: str, page_url: str) -> Optional[str]:
    """
    Шукає на сторінці компанії URL її іконки (логотипу). На цьому сайті
    логотипи компаній мають src з підрядком "company-logos" -- прапори
    країн, службові піктограми тощо цього підрядка не містять, тож фільтр
    надійно відрізняє саме логотип компанії від решти зображень сторінки.
    """
    soup = BeautifulSoup(html, "lxml")
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src and "company-logos" in src:
            return urljoin(page_url, src)
    return None


def download_image(url: str, dest_dir: str, filename: str, session: requests.Session) -> Optional[str]:
    """
    Завантажує зображення за URL і зберігає його у файл dest_dir/filename.
    Повертає шлях до збереженого файлу або None, якщо завантажити не вдалося
    (мережева помилка, помилка запису на диск тощо).
    """
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
            for chunk in resp.iter_content(8192):    # пишемо частинами -- економія пам'яті на великих файлах
                f.write(chunk)
        return dest_path
    except OSError as e:
        log.warning(f"  Не вдалося зберегти файл {dest_path}: {e}")
        return None


def download_company_logo(
    html: str, company: Company, images_dir: str, session: requests.Session
) -> "tuple[str, str]":
    """
    Об'єднує пошук URL іконки компанії та її завантаження у файл в один
    крок. Ім'я файлу будується з тикера компанії (напр. "NVDA.png").
    Повертає пару (image_url, image_local_path) -- саме ці рядки далі
    записуються у кожен JSON-запис цієї компанії. Якщо іконку не знайдено
    або не вдалося завантажити -- відповідне значення буде порожнім рядком.
    """
    logo_url = extract_logo_url(html, company.url)
    if logo_url is None:
        log.warning(f"  Іконку компанії не знайдено на сторінці {company.url}")
        return "", ""

    ext = os.path.splitext(urlparse(logo_url).path)[1] or ".png"
    safe_name = (company.ticker or company.name).replace(" ", "_").replace("/", "_")
    filename = f"{safe_name}{ext}"

    local_path = download_image(logo_url, images_dir, filename, session)
    return logo_url, (local_path or "")


# ------------------------------------------------------------------------------------
# Збереження результатів
# ------------------------------------------------------------------------------------

def save_csv(rows: List[MarketcapYear], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["company_name", "ticker", "year", "marketcap", "change_pct", "image_url", "image_local_path"]
        )
        for r in rows:
            writer.writerow(
                [r.company_name, r.ticker, r.year, r.marketcap, r.change_pct, r.image_url, r.image_local_path]
            )
    log.info(f"Збережено: {path}")


def save_json(rows: List[MarketcapYear], path: str) -> None:
    # asdict() перетворює кожен MarketcapYear (разом з image_url/image_local_path) у dict,
    # тож посилання на іконку компанії опиняється у КОЖНОМУ записі JSON-файлу.
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)
    log.info(f"Збережено: {path}")


# ------------------------------------------------------------------------------------
# Головна функція
# ------------------------------------------------------------------------------------

def main():
    session = requests.Session()

    # Створюємо окрему папку для зображень (іконок компаній) ЗАВЖДИ, на самому
    # старті програми -- незалежно від того, скільком компаніям вдасться
    # завантажити іконку. Так папка існує навіть при 0 успішних завантаженнях,
    # і в неї, а не кудись інше, потраплятимуть усі файли з download_image().
    os.makedirs(IMAGES_DIR, exist_ok=True)
    log.info(f"Папка для зображень: {os.path.abspath(IMAGES_DIR)}")

    # 1. Топ-100 компаній
    log.info(f"Завантаження списку компаній: {SOURCE_URL}")
    main_html = fetch_html(SOURCE_URL, session)
    if main_html is None:
        raise RuntimeError("Не вдалося завантажити головну сторінку джерела.")

    companies = parse_top_companies(main_html, SOURCE_URL, TOP_N)

    # 2. Для кожної компанії -- капіталізація за роками + іконка (логотип)
    all_rows: List[MarketcapYear] = []
    logos_downloaded = 0

    for i, company in enumerate(companies, start=1):
        log.info(f"[{i}/{len(companies)}] {company.name} ({company.ticker}) -> {company.url}")
        html = fetch_html(company.url, session)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        if html is None:
            log.warning(f"  Пропущено: {company.url}")
            continue

        rows = parse_marketcap_by_year(html, company)
        log.info(f"  Знайдено {len(rows)} річних записів капіталізації")

        # одне завантаження іконки на компанію -- і посилання додається у ВСІ
        # рядки цієї компанії (кожен рік капіталізації)
        image_url, image_local_path = download_company_logo(html, company, IMAGES_DIR, session)
        if image_local_path:
            logos_downloaded += 1
            log.info(f"  Іконку збережено: {image_local_path}")

        for r in rows:
            r.image_url = image_url
            r.image_local_path = image_local_path

        all_rows.extend(rows)

    # 3. Збереження
    save_csv(all_rows, OUTPUT_CSV)
    save_json(all_rows, OUTPUT_JSON)
    log.info(
        f"Готово. Усього записів: {len(all_rows)}; іконок завантажено: "
        f"{logos_downloaded}/{len(companies)}"
    )


if __name__ == "__main__":
    main()