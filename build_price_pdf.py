"""
build_price_pdf.py — собирает price-list.pdf: краткий прайс-лист товаров
в наличии, сгруппированных по категориям (миниатюра + название + цена +
срок годности, БЕЗ полного описания — как меню).

Ключевое архитектурное решение: цена товара считается по довольно сложным
правилам (три режима — weight/approxPiece/flat, старые текстовые товары и
новые с чекбоксами, кг vs 100г для мясных деликатесов...). Чтобы не
дублировать эту логику ещё раз на Python и не рисковать тем, что она
разъедется с сайтом при будущих правках — этот скрипт запускает НАСТОЯЩИЙ
JS-код из index.html через Node.js (тот же приём, которым в разработке
многократно тестировался сам сайт) и просто форматирует уже готовый
результат в PDF. Один источник истины для расчёта цены — сам сайт.

Установка: pip install requests reportlab
Требуется Node.js в PATH (на GitHub Actions ubuntu-latest он есть по умолчанию).
"""

import datetime
import io
import json
import os
import re
import subprocess
import sys
import time

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

NOCODB_URL = "https://app.nocodb.com"
TABLE_ID = "mzyn24rg2qoo8xs"
TOKEN = os.environ.get("NOCODB_TOKEN", "")
PAGE_DELAY = 0.6
MAX_RETRIES = 6

INDEX_FILE = os.path.join(os.path.dirname(__file__), "index.html")
PHOTOS_FILE = os.path.join(os.path.dirname(__file__), "photos.js")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "price-list.pdf")

# Фирменные цвета сайта
WINE = colors.HexColor("#6B2737")
GOLD = colors.HexColor("#C9A84C")
CREAM = colors.HexColor("#F7F3EC")
DARK = colors.HexColor("#1C1712")
GRAY = colors.HexColor("#8a8378")

# Реальный порядок категорий, как на сайте — без псевдо-категорий
# (Все/Акции/NEW/Успейте — это сквозные фильтры, не настоящие разделы)
CATEGORY_ORDER = [
    "Сыры", "Хамон/Нарезки", "Фуэт/Колбасы", "Фуа Гра",
    "Бакалея", "Подарочный бокс", "Доп.",
]

DEJAVU_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def request_with_retry(url, **kwargs):
    delay = 2
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, **kwargs)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            print(f"  429 от сервера, жду {wait:.0f}с (попытка {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            delay = min(delay * 2, 30)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def fetch_all_records():
    all_records, offset = [], 0
    while True:
        resp = request_with_retry(
            f"{NOCODB_URL}/api/v2/tables/{TABLE_ID}/records",
            headers={"xc-token": TOKEN},
            params={"limit": 200, "offset": offset},
            timeout=30,
        )
        data = resp.json()
        page = data.get("list", [])
        all_records.extend(page)
        if not page or data.get("pageInfo", {}).get("isLastPage"):
            break
        offset += len(page)
        time.sleep(PAGE_DELAY)
    return all_records


NODE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

// Минимальные заглушки браузерного окружения — верхнеуровневый код
// index.html не должен упасть при загрузке без настоящего DOM
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.navigator = { clipboard: undefined };
const fakeEl = {
  addEventListener: () => {}, classList: { add(){}, remove(){}, toggle(){}, contains: () => false },
  style: {}, value: '', checked: false, textContent: '', innerHTML: '', dataset: {},
  querySelector: () => null, querySelectorAll: () => [], closest: () => null, appendChild(){}, removeChild(){},
};
global.document = {
  getElementById: () => fakeEl, querySelector: () => null, querySelectorAll: () => [],
  addEventListener: () => {}, createElement: () => ({ innerHTML: '', firstElementChild: null }),
  body: fakeEl, documentElement: { style: {} },
};
global.window = global;
global.window.addEventListener = () => {};

// photos.js — готовые миниатюры; mapRecord подхватит их через window.PHOTO_CACHE
try {
  vm.runInThisContext(fs.readFileSync(process.argv[3], 'utf-8'));
} catch (e) {
  console.error('photos.js: ' + e.message);
}

// Основной JS сайта (вытащен снаружи, в Python, из index.html)
try {
  vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf-8'));
} catch (e) {
  // Верхнеуровневые ошибки без настоящего DOM ожидаемы — нужные функции
  // (mapRecord, isAvailable) объявлены раньше и уже доступны
}

const rawRecords = JSON.parse(fs.readFileSync(process.argv[4], 'utf-8'));
const withName = rawRecords.filter(rec => (rec.Name || '').toString().trim());
const products = withName.filter(isAvailable).map(mapRecord);
fs.writeFileSync(process.argv[5], JSON.stringify(products));
console.error('Обработано товаров через реальный JS сайта: ' + products.length);
"""


def extract_main_script(html):
    """Вытаскивает последний (основной, самый большой) <script>-блок без
    атрибута src — это и есть вся бизнес-логика сайта."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    if not scripts:
        raise RuntimeError("Не нашёл ни одного inline <script> в index.html")
    return max(scripts, key=len)


def run_node_mapping(raw_records):
    """Прогоняет сырые записи через настоящий mapRecord()/isAvailable() из
    index.html посредством Node.js — один источник истины для цены."""
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    main_js = extract_main_script(html)

    tmp_dir = "_pdf_build_tmp"
    os.makedirs(tmp_dir, exist_ok=True)
    driver_path = os.path.join(tmp_dir, "driver.js")
    script_path = os.path.join(tmp_dir, "main.js")
    raw_path = os.path.join(tmp_dir, "raw.json")
    out_path = os.path.join(tmp_dir, "mapped.json")

    with open(driver_path, "w", encoding="utf-8") as f:
        f.write(NODE_DRIVER)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(main_js)
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(raw_records, f, ensure_ascii=False)

    photos_path = PHOTOS_FILE if os.path.exists(PHOTOS_FILE) else raw_path  # запасной вариант, если photos.js ещё нет
    result = subprocess.run(
        ["node", driver_path, script_path, photos_path, raw_path, out_path],
        capture_output=True, text=True,
    )
    if result.stderr:
        print(result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(f"Node упал с кодом {result.returncode}")

    with open(out_path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_price(p):
    """Тот же формат, что и priceBlockHtml на сайте, только текстом для PDF."""
    price_base_label = "кг" if p.get("priceBaseGrams") == 1000 else "100гр"
    mode = p.get("priceMode")
    if mode == "weight":
        main = f'{p["price"]}₽/{p["unit"]}'
    elif mode == "approxPiece":
        main = f'{p["unitPrice"]}₽/шт (~{p["pieceWeightGrams"]}гр, {p["price"]}₽/{price_base_label})'
    else:
        main = f'{p["unitPrice"]}₽'

    old = None
    if p.get("oldPrice") is not None:
        old_val = p["oldPrice"] if mode == "weight" else p.get("oldUnitPrice")
        if old_val is not None:
            old = f"{old_val}₽"
    return main, old


def decode_thumb(data_uri, size_pt=34):
    """Декодирует base64 data:-URI миниатюры в Image-флоуэбл заданного
    размера. Если фото нет/битое — возвращает None (в PDF просто пустая
    ячейка вместо картинки, без падения всей сборки)."""
    if not data_uri or not data_uri.startswith("data:"):
        return None
    try:
        import base64
        b64 = data_uri.split(",", 1)[1]
        raw = base64.b64decode(b64)
        return Image(io.BytesIO(raw), width=size_pt, height=size_pt)
    except Exception:
        return None


def build_pdf(products):
    pdfmetrics.registerFont(TTFont("DejaVu", DEJAVU_REGULAR))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", DEJAVU_BOLD))

    doc = SimpleDocTemplate(
        OUTPUT_FILE, pagesize=A4,
        topMargin=16 * mm, bottomMargin=14 * mm,
        leftMargin=14 * mm, rightMargin=14 * mm,
    )

    title_style = ParagraphStyle(
        "Title", fontName="DejaVu-Bold", fontSize=20, leading=26, textColor=WINE, spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "Subtitle", fontName="DejaVu", fontSize=9, leading=12, textColor=GRAY, spaceAfter=14,
    )
    cat_style = ParagraphStyle(
        "Cat", fontName="DejaVu-Bold", fontSize=13, leading=16, textColor=colors.white,
        backColor=DARK, borderPadding=(5, 8, 5, 8), spaceBefore=10, spaceAfter=6,
    )
    name_style = ParagraphStyle("Name", fontName="DejaVu-Bold", fontSize=9.5, textColor=DARK, leading=12)
    meta_style = ParagraphStyle("Meta", fontName="DejaVu", fontSize=7.5, textColor=GRAY, leading=10)
    price_style = ParagraphStyle("Price", fontName="DejaVu-Bold", fontSize=10, leading=13, textColor=WINE, alignment=2)
    old_price_style = ParagraphStyle("OldPrice", fontName="DejaVu", fontSize=7.5, leading=10, textColor=GRAY, alignment=2)

    story = []
    story.append(Paragraph("Mr. Mouse — прайс-лист", title_style))
    generated = datetime.datetime.now().strftime("%d.%m.%Y")
    story.append(Paragraph(
        f"Актуально на {generated} · только товары в наличии · цены могут измениться, уточняйте при заказе",
        subtitle_style,
    ))

    by_cat = {}
    for p in products:
        by_cat.setdefault(p.get("cat") or "Без категории", []).append(p)

    ordered_cats = [c for c in CATEGORY_ORDER if c in by_cat]
    ordered_cats += [c for c in by_cat if c not in CATEGORY_ORDER]  # на случай новых категорий

    total_items = 0
    for i, cat in enumerate(ordered_cats):
        items = by_cat[cat]
        if i > 0:
            story.append(PageBreak())
        story.append(Paragraph(cat, cat_style))

        rows = []
        for p in items:
            total_items += 1
            thumb = decode_thumb(p.get("photo"))
            main_price, old_price = format_price(p)

            name_lines = [Paragraph(p.get("name") or "", name_style)]
            meta_bits = []
            if p.get("rawCountry"):
                meta_bits.append(p["rawCountry"])
            specs = [p.get("fat") and f'Жирность {p["fat"]}', p.get("aging") and f'Выдержка {p["aging"]}', p.get("milk")]
            specs = [s for s in specs if s]
            if specs:
                meta_bits.append(" · ".join(specs))
            if p.get("bestBefore"):
                meta_bits.append(f'Годен до {p["bestBefore"]}')
            if meta_bits:
                name_lines.append(Paragraph(" &nbsp;|&nbsp; ".join(meta_bits), meta_style))

            price_lines = []
            if old_price:
                price_lines.append(Paragraph(f'<strike>{old_price}</strike>', old_price_style))
            price_lines.append(Paragraph(main_price, price_style))

            rows.append([thumb, name_lines, price_lines])

        table = Table(rows, colWidths=[38, None, 130])
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#E8E0D0")),
        ]))
        story.append(table)
        story.append(Spacer(1, 4))

    doc.build(story)
    return total_items


def main():
    if not TOKEN:
        print("Не задан NOCODB_TOKEN", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(INDEX_FILE):
        print("Не нашёл index.html рядом со скриптом", file=sys.stderr)
        sys.exit(1)

    print("Загружаю товары из NocoDB...")
    raw_records = fetch_all_records()
    print(f"  товаров всего: {len(raw_records)}")

    print("Считаю цены через настоящий JS сайта (Node.js)...")
    products = run_node_mapping(raw_records)
    print(f"  в наличии, попадёт в прайс: {len(products)}")

    print("Собираю PDF...")
    total = build_pdf(products)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"\nГотово: {OUTPUT_FILE}")
    print(f"  товаров в прайсе: {total}")
    print(f"  размер файла: {size_kb:.0f} КБ")


if __name__ == "__main__":
    main()
