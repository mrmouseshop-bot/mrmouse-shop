"""
build_offline_snapshot.py — собирает offline.html: полностью самодостаточную
версию сайта с "зашитыми" внутрь данными каталога и фото (photos.js
инлайнится прямо в файл). Не делает ни одного обращения к NocoDB при
открытии — можно скачать этот один файл и открыть его позже даже совсем
без интернета (кроме шрифтов Google Fonts — они подгружаются с сервера,
и Telegram Web App SDK, если сайт открыт вне Telegram — без него сайт
тоже прекрасно работает, просто чуть проще выглядит без этих двух вещей).

Что работает офлайн после скачивания:
  - Весь каталог (названия, цены, описания, фото — включая фото, которых
    ещё нет в photos.js на момент сборки, "просядут" до плейсхолдера).
  - Корзина, фильтры, поиск, расчёт стоимости.
  - Переход в Telegram/WhatsApp/MAX по ссылке и копирование заказа в
    буфер обмена — всё это работает локально, без сервера.

Что НЕ будет работать (и не может, это не баг):
  - "Живые" изменения цен/наличия после скачивания файла — снепшот
    актуален на момент генерации, дальше не обновляется сам.
  - Промокоды, добавленные в NocoDB ПОСЛЕ скачивания.
  - Автоматический подбор ПВЗ СДЭК (и в обычной версии сайта работает
    так же — просто ссылка на официальный поиск СДЭК).

Установка: pip install requests

Использование:
  NOCODB_TOKEN=ваш_токен python build_offline_snapshot.py

Создаст offline.html рядом с index.html. Рекомендуется коммитить его
в репозиторий и обновлять по расписанию (как photos.js) — см.
build-offline.yml.
"""

import datetime
import json
import os
import re
import sys
import time

import requests

NOCODB_URL = "https://app.nocodb.com"
TABLE_ID = "mzyn24rg2qoo8xs"
TOKEN = os.environ.get("NOCODB_TOKEN", "")

INDEX_FILE = os.path.join(os.path.dirname(__file__), "index.html")
PHOTOS_FILE = os.path.join(os.path.dirname(__file__), "photos.js")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "offline.html")

PAGE_DELAY = 0.6
MAX_RETRIES = 6


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


def main():
    if not TOKEN:
        print("Не задан NOCODB_TOKEN", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(INDEX_FILE):
        print(f"Не найден {INDEX_FILE} — запускайте скрипт из папки с index.html", file=sys.stderr)
        sys.exit(1)

    print("Загружаю товары из NocoDB...")
    records = fetch_all_records()
    print(f"  товаров: {len(records)}")

    print("Читаю index.html...")
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    photos_js = ""
    if os.path.exists(PHOTOS_FILE):
        with open(PHOTOS_FILE, "r", encoding="utf-8") as f:
            photos_js = f.read()
        print(f"  photos.js найден, {len(photos_js) / 1024:.0f} КБ")
    else:
        print("  photos.js не найден — офлайн-версия будет без встроенных миниатюр", file=sys.stderr)

    generated_at = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    snapshot_json = json.dumps(records, ensure_ascii=False)

    # 1) Инлиним photos.js прямо в файл вместо внешней ссылки на него —
    # офлайн-версия должна быть одним самодостаточным файлом
    old_photos_tag = '<script src="photos.js" onerror="window.PHOTO_CACHE = window.PHOTO_CACHE || {}"></script>'
    new_photos_tag = f"<script>\n{photos_js}\n</script>" if photos_js else \
        '<script>window.PHOTO_CACHE = {};</script>'
    if old_photos_tag not in html:
        print("Не нашёл строку подключения photos.js в index.html — возможно, файл изменился."
              " Проверьте вручную.", file=sys.stderr)
        sys.exit(1)
    html = html.replace(old_photos_tag, new_photos_tag, 1)

    # 2) Добавляем снепшот каталога сразу за инлайненным photos.js —
    # loadCatalog() в index.html уже умеет проверять window.OFFLINE_SNAPSHOT
    # и, если он есть, использовать его без единого обращения к NocoDB
    snapshot_tag = (
        f"<script>window.OFFLINE_SNAPSHOT = "
        f'{{records: {snapshot_json}, generatedAt: "{generated_at}"}};</script>'
    )
    html = html.replace(new_photos_tag, new_photos_tag + "\n" + snapshot_tag, 1)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    size_mb = os.path.getsize(OUTPUT_FILE) / 1024 / 1024
    print(f"\nГотово: {OUTPUT_FILE}")
    print(f"  товаров в снепшоте: {len(records)}")
    print(f"  размер файла: {size_mb:.1f} МБ")
    print(f"  сгенерирован: {generated_at}")


if __name__ == "__main__":
    main()
