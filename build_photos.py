"""
Строит photos.js (миниатюры, "зашитые" в код base64) и папку photos/ с
полноразмерными фото как обычными файлами — оба варианта из одной и той же
таблицы NocoDB. index.html подключает photos.js как первый (мгновенный, без
сети) вариант миниатюры для карточки товара, а окно "Описание" использует
файлы из photos/ как первый вариант полноразмерного фото — свой, размещённый
на GitHub Pages, а не подписанную ссылку NocoDB (та истекает через 2 часа
по умолчанию — из-за этого раньше "Описание" иногда скатывалось на
размытую миниатюру, если каталог был взят из кэша браузера старше 2 часов).

Запускать:
  1) вручную:  NOCODB_TOKEN=... python build_photos.py
  2) автоматически по расписанию — см. .github/workflows/build-photos.yml,
     который запускает этот же скрипт и сам коммитит обновлённые photos.js
     и папку photos/.

Токен передаётся через переменную окружения NOCODB_TOKEN, а не хардкодится
в файле — так безопаснее хранить его в GitHub Secrets.

Важно про менеджеров: ничего в их процессе не меняется — они как загружали
фото в поле Photo прямо в NocoDB, так и продолжают. Скрипт сам находит
нужное фото по Id записи (его назначает сама NocoDB автоматически) — никаких
особых названий файлов вручную придумывать не нужно.
"""

import base64
import hashlib
import io
import json
import os
import re
import sys
import time

import requests
from PIL import Image

NOCODB_URL = "https://app.nocodb.com"
TABLE_ID = "mzyn24rg2qoo8xs"
TOKEN = os.environ.get("NOCODB_TOKEN", "")

# Размер и качество миниатюры — подобраны так, чтобы весить по паре килобайт
# на фото (сумма на ~900 товаров укладывается в единицы мегабайт).
THUMB_SIZE = 190      # пикселей по большей стороне (карточка на сайте — 92x92
                       # CSS-пикселей; берём с запасом для retina-экранов,
                       # где 1 CSS-пиксель = 2-3 физических)
JPEG_QUALITY = 68

# Полноразмерное фото для окна "Описание" — крупнее миниатюры, но не
# оригинал "как есть" (те бывают по несколько МБ с телефона) — сжимаем до
# разумного размера, этого достаточно для просмотра на экране телефона
FULL_MAX_SIZE = 1000
FULL_JPEG_QUALITY = 82

# Пауза между запросами, чтобы не упираться в лимит запросов NocoDB (429)
PAGE_DELAY = 0.6
IMAGE_DELAY = 0.15
MAX_RETRIES = 6

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "photos.js")
FULL_PHOTOS_DIR = os.path.join(os.path.dirname(__file__), "photos")
INDEX_FILE = os.path.join(os.path.dirname(__file__), "index.html")


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
    """Берём полноразмерный оригинал — важно для правильного вписывания в
    квадрат без обрезки. Готовые превью NocoDB (thumbnails.tiny/small/
    card_cover) генерируются самой NocoDB методом "cover" и обрезают
    неквадратные фото по центру ещё до того, как файл попадёт к нам —
    это уже необратимо на нашей стороне, поэтому используем их только как
    запасной вариант, если оригинала нет вообще."""
    if not photo_field:
        return None
    f = photo_field[0]
    th = f.get("thumbnails") or {}
    return (
        f.get("signedUrl")
        or f.get("url")
        or (th.get("small") or {}).get("signedUrl")
        or (th.get("card_cover") or {}).get("signedUrl")
        or (th.get("tiny") or {}).get("signedUrl")
    )


def make_thumb_data_uri(img):
    # thumbnail() уменьшает с сохранением пропорций и НЕ обрезает — но для
    # неквадратного фото результат тоже останется неквадратным (напр. 72x54).
    # Кладём его по центру на белый квадратный холст THUMB_SIZE x THUMB_SIZE —
    # так весь товар остаётся видимым, а не обрезанным по бокам/сверху/снизу
    thumb = img.copy()
    thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
    canvas = Image.new("RGB", (THUMB_SIZE, THUMB_SIZE), (255, 255, 255))
    offset = ((THUMB_SIZE - thumb.width) // 2, (THUMB_SIZE - thumb.height) // 2)
    canvas.paste(thumb, offset)
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def save_full_photo(img, rec_id):
    """Сохраняет полноразмерное (но сжатое до разумного предела) фото как
    обычный JPEG-файл photos/{Id}.jpg — свой, не зависящий от NocoDB и не
    протухающий, в отличие от подписанных ссылок."""
    full = img.copy()
    full.thumbnail((FULL_MAX_SIZE, FULL_MAX_SIZE))
    path = os.path.join(FULL_PHOTOS_DIR, f"{rec_id}.jpg")
    full.save(path, format="JPEG", quality=FULL_JPEG_QUALITY, optimize=True)


def main():
    if not TOKEN:
        print("Не задан NOCODB_TOKEN", file=sys.stderr)
        sys.exit(1)

    os.makedirs(FULL_PHOTOS_DIR, exist_ok=True)

    records = fetch_all_records()
    print(f"Загружено записей: {len(records)}")

    cache = {}
    current_ids_with_photo = set()
    errors = 0
    for i, rec in enumerate(records):
        rec_id = rec.get("Id")
        if rec_id is None:
            continue
        url = best_photo_url(rec.get("Photo"))
        if not url:
            continue
        try:
            resp = request_with_retry(url, timeout=20)
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            cache[str(rec_id)] = make_thumb_data_uri(img)
            save_full_photo(img, rec_id)
            current_ids_with_photo.add(str(rec_id))
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

    # Уборка: удаляем файлы для товаров, у которых фото убрали или которых
    # больше нет вообще — иначе папка photos/ будет только расти
    removed = 0
    for fname in os.listdir(FULL_PHOTOS_DIR):
        if not fname.endswith(".jpg"):
            continue
        rec_id = fname[:-4]
        if rec_id not in current_ids_with_photo:
            os.remove(os.path.join(FULL_PHOTOS_DIR, fname))
            removed += 1

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    full_size_mb = sum(
        os.path.getsize(os.path.join(FULL_PHOTOS_DIR, f))
        for f in os.listdir(FULL_PHOTOS_DIR)
    ) / 1024 / 1024
    print(f"Готово: {len(cache)} фото, пропущено с ошибкой: {errors}")
    print(f"  photos.js (миниатюры): {size_kb:.0f} КБ")
    print(f"  photos/ (полноразмерные): {len(current_ids_with_photo)} файлов, {full_size_mb:.1f} МБ, удалено устаревших: {removed}")

    # Cache-busting: ссылка на photos.js в index.html всегда была одна и та
    # же ("photos.js"), из-за этого браузеры могли годами отдавать старую
    # закэшированную копию файла, даже когда на GitHub уже лежит новая версия
    # с более крупными миниатюрами. Дописываем к ссылке короткий хэш от
    # содержимого — при каждом реальном изменении файла ссылка меняется,
    # и браузер гарантированно скачивает свежую версию.
    if os.path.exists(INDEX_FILE):
        version = hashlib.sha256(open(OUTPUT_FILE, "rb").read()).hexdigest()[:10]
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            html = f.read()
        new_html, n = re.subn(
            r'src="photos\.js(?:\?v=[^"]*)?"',
            f'src="photos.js?v={version}"',
            html,
            count=1,
        )
        if n:
            with open(INDEX_FILE, "w", encoding="utf-8") as f:
                f.write(new_html)
            print(f"  index.html: ссылка на photos.js обновлена (?v={version})")
        else:
            print(
                "  предупреждение: не нашёл тег <script src=\"photos.js\"...> "
                "в index.html — версию проставить не удалось, проверьте вручную",
                file=sys.stderr,
            )
    else:
        print("  index.html не найден рядом — версию photos.js не обновляю", file=sys.stderr)


if __name__ == "__main__":
    main()
