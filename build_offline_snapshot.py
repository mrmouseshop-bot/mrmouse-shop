"""
build_offline_snapshot.py — собирает offline.html: полностью самодостаточную
версию сайта с "зашитыми" внутрь данными каталога и фото (photos.js
инлайнится прямо в файл). Не делает ни одного обращения к NocoDB при
открытии — можно скачать этот один файл и открыть его позже даже совсем
без интернета (кроме шрифтов Google Fonts — они подгружаются с сервера,
и Telegram Web App SDK, если сайт открыт вне Telegram — без него сайт
тоже прекрасно работает, просто чуть проще выглядит без этих двух вещей).

Что работает офлайн после скачивания:
  - Каталог товаров, которые в наличии на сайте (названия, цены, фото —
    включая фото, которых ещё нет в photos.js на момент сборки, "просядут"
    до плейсхолдера). Товары не в наличии в снепшот не попадают — на
    сайте их всё равно не видно, а лишний вес они дают немалый.
  - Корзина, фильтры, поиск, расчёт стоимости.
  - Переход в Telegram/WhatsApp/MAX по ссылке и копирование заказа в
    буфер обмена — всё это работает локально, без сервера.

Что НЕ будет работать (и не может, это не баг):
  - Текстовое описание товара (окно "Описание") — само окно и полноразмерное
    фото открываются, но текст среза́н ради размера файла; вместо текста
    покажется "Описание пока не добавлено". Для полного описания — только
    на живом сайте.
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

import base64
import datetime
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

# Миниатюры в photos.js сделаны крупными и чёткими для живого сайта (190px).
# Для офлайн-файла это лишний вес — там не нужна такая чёткость, важнее
# надёжная загрузка. Пересжимаем каждую оставшуюся миниатюру заметно мельче
# и грубее специально для офлайн-сборки, не трогая исходный photos.js.
OFFLINE_THUMB_SIZE = 64
OFFLINE_THUMB_QUALITY = 45

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


def recompress_thumb_smaller(data_uri):
    """Декодирует уже готовую base64-миниатюру из photos.js и пересжимает
    её заметно мельче/грубее специально для офлайн-файла. Если по какой-то
    причине декодировать не получилось (битые данные и т.п.) — возвращает
    исходную миниатюру как есть, не роняя всю сборку."""
    try:
        header, b64data = data_uri.split(",", 1)
        raw = base64.b64decode(b64data)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((OFFLINE_THUMB_SIZE, OFFLINE_THUMB_SIZE))
        canvas = Image.new("RGB", (OFFLINE_THUMB_SIZE, OFFLINE_THUMB_SIZE), (255, 255, 255))
        offset = ((OFFLINE_THUMB_SIZE - img.width) // 2, (OFFLINE_THUMB_SIZE - img.height) // 2)
        canvas.paste(img, offset)
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=OFFLINE_THUMB_QUALITY, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return data_uri


def is_available(rec):
    """Точная копия isAvailable() из index.html — поддерживает и чекбокс
    (true/false), и старое текстовое поле."""
    val = rec.get("Availability")
    if isinstance(val, bool):
        return val
    return str(val or "").strip().lower() != "нет в наличии"


def strip_for_offline(rec):
    """Облегчённая копия записи для офлайн-снепшота:
    - Photo (подписанные S3-ссылки на оригинал и превью NocoDB) в офлайне
      абсолютно бесполезно — без интернета эти ссылки всё равно не
      откроются, а миниатюра и так берётся из встроенного PHOTO_CACHE по
      Id записи независимо от содержимого Photo. ~60% веса записи.
    - Description (текстовое описание товара) — самое тяжёлое текстовое
      поле после Photo (~44% веса того, что остаётся после среза Photo).
      В офлайн-версии окно "Описание" при пустом поле корректно покажет
      "Описание пока не добавлено" — это уже штатный фолбэк на сайте,
      ничего не ломается, просто нет текста для этого конкретного случая."""
    light = dict(rec)
    light["Photo"] = None
    light["Description"] = None
    return light


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
    all_records = fetch_all_records()
    print(f"  товаров всего: {len(all_records)}")

    # Товары без названия или не в наличии на сайте всё равно не
    # отрисовываются (applyRawRecords на живом сайте фильтрует их точно
    # так же) — включать их в офлайн-снепшот незачем, это чистый лишний
    # вес. На реальных данных таких оказывается до 80% всех записей —
    # огромная экономия, особенно важная для слабых окружений вроде
    # iOS Quick Look, где тяжёлые файлы могут просто не осиливаться.
    records = [r for r in all_records if (r.get("Name") or "").strip() and is_available(r)]
    print(f"  из них попадёт в офлайн-снепшот (в наличии, с названием): {len(records)}")

    print("Читаю index.html...")
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    kept_ids = {str(r.get("Id")) for r in records}
    photos_js = ""
    if os.path.exists(PHOTOS_FILE):
        with open(PHOTOS_FILE, "r", encoding="utf-8") as f:
            photos_js_raw = f.read()
        # photos.js хранит миниатюры для ВСЕХ товаров (включая те, что не
        # попали в офлайн-снепшот) — оставляем только те, что реально
        # понадобятся, остальное просто раздувает файл без пользы
        cache_match = re.search(r"window\.PHOTO_CACHE\s*=\s*(\{.*?\});", photos_js_raw, re.S)
        if cache_match:
            full_cache = json.loads(cache_match.group(1))
            print(f"  пересжимаю миниатюры под офлайн-файл ({OFFLINE_THUMB_SIZE}px, качество {OFFLINE_THUMB_QUALITY})...")
            light_cache = {
                k: recompress_thumb_smaller(v)
                for k, v in full_cache.items()
                if k in kept_ids
            }
            photos_js = (
                "// Автоматически сгенерировано build_offline_snapshot.py "
                "(обрезано и пересжато под офлайн-версию) — не редактировать руками\n"
                "window.PHOTO_CACHE = " + json.dumps(light_cache, ensure_ascii=False) + ";\n"
            )
            print(f"  photos.js: оставлено {len(light_cache)} из {len(full_cache)} миниатюр")
        else:
            photos_js = photos_js_raw
            print("  не нашёл window.PHOTO_CACHE внутри photos.js — оставляю файл как есть", file=sys.stderr)
    else:
        print("  photos.js не найден — офлайн-версия будет без встроенных миниатюр", file=sys.stderr)

    generated_at = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    light_records = [strip_for_offline(r) for r in records]
    snapshot_obj = {"records": light_records, "generatedAt": generated_at}
    snapshot_json = json.dumps(snapshot_obj, ensure_ascii=False)
    # Защита: если в названии/описании товара вдруg встретится буквальная
    # последовательность "</script" — браузерный HTML-парсер закроет тег
    # раньше времени и обрежет весь дальнейший JSON. \/ — валидный экранированный
    # слэш в JSON, JSON.parse превратит его обратно в обычный "/"
    snapshot_json = re.sub(r"</(script)", r"<\\/\1", snapshot_json, flags=re.IGNORECASE)

    # 0) Статичная подсказка "откройте в Safari" — вставляется ДО любого JS
    # и не зависит от него вообще. iOS часто открывает скачанный HTML-файл
    # не в полноценном Safari, а в урезанном режиме предпросмотра (Quick
    # Look), где тяжёлые страницы могут работать медленнее или зависать.
    # Эта строка — чистый HTML/CSS, покажется в любом случае, даже если
    # весь дальнейший JavaScript на странице не запустится вовсе.
    static_hint = (
        '<div style="background:#6B2737;color:#fff;text-align:center;'
        'padding:10px 16px;font-family:sans-serif;font-size:13px;'
        'line-height:1.4;">'
        '📴 Офлайн-версия каталога. Если товары долго не появляются — '
        'подождите ещё немного, не закрывайте страницу.'
        '</div>'
    )
    if "<body>" in html:
        html = html.replace("<body>", "<body>\n" + static_hint, 1)

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
    # и, если он есть, использовать его без единого обращения к NocoDB.
    # Данные лежат в <script type="application/json"> (браузер воспринимает
    # это как обычный текст, не разбирая как JavaScript) и превращаются в
    # объект через JSON.parse — это заметно быстрее и легче для движка, чем
    # разбор эквивалентного "сырого" JS-объекта такого размера.
    snapshot_tag = (
        f'<script type="application/json" id="offline-snapshot-json">{snapshot_json}</script>\n'
        f"<script>window.OFFLINE_SNAPSHOT = "
        f"JSON.parse(document.getElementById('offline-snapshot-json').textContent);</script>"
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
