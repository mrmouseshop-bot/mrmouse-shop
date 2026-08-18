// Service Worker для Mr. Mouse — делает сайт по-настоящему надёжным офлайн
// ПОСЛЕ первого успешного захода (актуально для тех, кто добавил сайт на
// «Экран Домой»). Работает только для страницы, открытой как обычный сайт
// (https://...) — на скачанный локальный файл (offline.html) это никак не
// влияет, там Service Worker вообще не регистрируется браузером.
//
// Стратегия — "сеть в приоритете" (вы выбрали именно этот вариант):
// пока есть интернет, всегда отдаём самую свежую версию с сервера и тут же
// обновляем кэш; как только сети нет — отдаём то, что успело закэшироваться
// при последнем успешном заходе, вместо пустого экрана с ошибкой.

const CACHE_VERSION = 'mrmouse-v3';
const APP_SHELL = [
  './',
  'index.html',
  'photos.js',
  'manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => {
      // Кэшируем по одному — если какой-то один ресурс временно недоступен
      // (например, ещё не закоммитился photos.js), это не должно срывать
      // установку всего Service Worker целиком
      return Promise.all(
        APP_SHELL.map((url) =>
          cache.add(url).catch((err) => {
            console.warn('[SW] не удалось закэшировать', url, err);
          })
        )
      );
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Трогаем только свои GET-запросы — сторонние ресурсы (шрифты, Telegram
  // SDK, сама NocoDB) не кэшируем, у них своя логика и свои CORS-правила
  if (req.method !== 'GET') return;
  let url;
  try {
    url = new URL(req.url);
  } catch (e) {
    return;
  }
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    fetch(req)
      .then((networkResponse) => {
        // Сеть доступна — отдаём свежий ответ и тут же обновляем кэш,
        // чтобы следующий офлайн-заход получил именно эту версию
        const copy = networkResponse.clone();
        caches.open(CACHE_VERSION).then((cache) => cache.put(req, copy));
        return networkResponse;
      })
      .catch(() =>
        // Сети нет — отдаём то, что уже лежит в кэше с прошлого раза;
        // для самой страницы дополнительно подстраховываемся index.html
        caches.match(req).then((cached) => cached || caches.match('index.html'))
      )
  );
});
