"""
Строит photos.js — файл с уже сжатыми миниатюрами всех товаров, «зашитыми»
прямо в код (base64). Сама страница index.html подключает photos.js и
использует его как самый первый (мгновенный, без сети) вариант картинки для
каждого товара; если товар новый и его ещё нет в кэше — сработает как раньше,
запрос к NocoDB за фото.

Запускать:
  1) вручную:  NOCODB_TOKEN=... python build_photos.py
  2) автоматически по расписанию — см. .github/workflows/build-photos.yml,
     который запускает этот же скрипт и сам коммитит обновлённый photos.js.

Токен передаётся через переменную окружения NOCODB_TOKEN, а не хардкодится
в файле — так безопаснее хранить его в GitHub Secrets.
"""

import base64
import io
import json
import os
import sys
import time

import requests
from PIL import Image

NOCODB_URL = "https://app.nocodb.com"
TABLE_ID = "mzyn24rg2qoo8xs"
TOKEN = os.environ.get("NOCODB_TOKEN", "")

# Размер и качество миниатюры — подобраны так, чтобы весить по паре килобайт
# на фото (сумма на ~900 товаров укладывается в единицы мегабайт).
THUMB_SIZE = 72       # пикселей по большей стороне (карточка на сайте — 52x52,
                       # берём чуть больше для чёткости на retina-экранах)
JPEG_QUALITY = 60

# Пауза между запросами, чтобы не упираться в лимит запросов NocoDB (429)
PAGE_DELAY = 0.6
IMAGE_DELAY = 0.15
MAX_RETRIES = 6

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "photos.js")


def request_with_retry(url, **kwargs):
    """GET с повторными попытками при 429 (слишком много запросов) —
    ждём дольше с каждой попыткой (экспоненциальная пауза)."""
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
    # последняя попытка — пусть падает с понятной ошибкой, если так и не вышло
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


def best_photo_url(photo_field):
    """Берём самую лёгкую доступную ссылку: сначала готовое превью NocoDB,
    потом полноразмерное фото."""
    if not photo_field:
        return None
    f = photo_field[0]
    th = f.get("thumbnails") or {}
    return (
        (th.get("tiny") or {}).get("signedUrl")
        or (th.get("small") or {}).get("signedUrl")
        or (th.get("card_cover") or {}).get("signedUrl")
        or f.get("signedUrl")
        or f.get("url")
    )


def make_data_uri(url):
    resp = request_with_retry(url, timeout=20)
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    img.thumbnail((THUMB_SIZE, THUMB_SIZE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def main():
    if not TOKEN:
        print("Не задан NOCODB_TOKEN", file=sys.stderr)
        sys.exit(1)

    records = fetch_all_records()
    print(f"Загружено записей: {len(records)}")

    cache = {}
    errors = 0
    for i, rec in enumerate(records):
        rec_id = rec.get("Id")
        if rec_id is None:
            continue
        url = best_photo_url(rec.get("Photo"))
        if not url:
            continue
        try:
            cache[str(rec_id)] = make_data_uri(url)
        except Exception as e:  # не роняем весь прогон из-за одного битого фото
            errors += 1
            print(f"  пропуск Id={rec_id}: {e}", file=sys.stderr)
        time.sleep(IMAGE_DELAY)
        if (i + 1) % 100 == 0:
            print(f"  обработано {i + 1}/{len(records)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("// Автоматически сгенерировано build_photos.py — не редактировать руками\n")
        f.write("window.PHOTO_CACHE = ")
        json.dump(cache, f, ensure_ascii=False)
        f.write(";\n")

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"Готово: {len(cache)} фото, пропущено с ошибкой: {errors}, размер файла: {size_kb:.0f} КБ")


if __name__ == "__main__":
    main()
