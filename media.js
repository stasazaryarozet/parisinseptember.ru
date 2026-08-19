/**
 * Эргономика воспроизведения — ЗАКОН САЙТА, а не поведение одной страницы.
 *
 * КОРЕНЬ (принципал 2026-08-19, дословно): «Для любого Сайта Системы: Эргономика
 * подразумевает, что можно прослушивать или просматривать только одну запись за раз,
 * и возникает на виду управление воспроизведением.»
 *
 * На странице рассказа-показа плееров ДЕСЯТКИ — по одному при каждой главе и при
 * записи целиком. Браузер разрешает играть всем сразу, а уехавший за край экрана
 * плеер остановить нечем: читатель слышит два голоса и ищет, где выключить. Оба
 * следствия — одного корня: у воспроизведения нет ЕДИНСТВЕННОСТИ и нет ПРИСУТСТВИЯ.
 *
 * ЕДИНСТВЕННОСТЬ. `play` не всплывает, поэтому слушатель ставится в ФАЗЕ ЗАХВАТА на
 * документе: один слушатель на страницу, сколько бы носителей на ней ни появилось и
 * когда бы они ни появились. Ни списка узлов, ни повторной привязки при отрисовке.
 *
 * ПРИСУТСТВИЕ. Пока играющий носитель ВИДЕН — управление у него самого, и второго
 * не нужно. Ушёл из виду (IntersectionObserver — наблюдение, а не опрос позиции) —
 * появляется полоса управления, и она есть ПРОЕКЦИЯ того же носителя: пауза и
 * возврат к нему. Остановился или снова стал виден — полоса исчезает сама.
 *
 * Ни одного имени страницы, ни одного слова в коде: подписи берутся с САМОГО
 * носителя (`aria-label`/`title`/подпись рядом), а надписи в полосе нет — только
 * знак действия и имя записи. Поэтому файл общий для всех страниц всех владельцев.
 */
(function () {
  'use strict';

  var SEL = 'audio, video';
  var current = null;                 // единственный играющий носитель
  var bar = null, barName = null, barBtn = null, io = null;

  function label(el) {
    // ИМЯ ЗАПИСИ ЧИТАЕТСЯ С НОСИТЕЛЯ. Порядок — от самого явного к самому общему;
    // пусто — полоса покажет только управление, и это честнее выдуманного имени.
    var own = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    if (own.trim()) return own.trim();
    var holder = el.closest('[data-role="record"]') || el.parentElement;
    var t = holder ? (holder.textContent || '') : '';
    return t.replace(/\s+/g, ' ').trim().slice(0, 80);
  }

  function ensureBar() {
    if (bar) return bar;
    bar = document.createElement('div');
    bar.className = 'now-playing';
    bar.hidden = true;
    barBtn = document.createElement('button');
    barBtn.type = 'button';
    barBtn.className = 'now-playing-toggle';
    barName = document.createElement('button');
    barName.type = 'button';
    barName.className = 'now-playing-name';
    bar.appendChild(barBtn);
    bar.appendChild(barName);
    document.body.appendChild(bar);
    barBtn.addEventListener('click', function () { if (current) current.pause(); });
    barName.addEventListener('click', function () {
      if (current) current.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    return bar;
  }

  function showBar(el) {
    ensureBar();
    barName.textContent = label(el);
    // Знак действия, а не слово: полоса появляется ТОЛЬКО при играющем носителе.
    barBtn.textContent = '⏸';
    barBtn.setAttribute('aria-label', barName.textContent || '⏸');
    bar.hidden = false;
  }

  function hideBar() { if (bar) bar.hidden = true; }

  function watch(el) {
    if (io) io.disconnect();
    if (!('IntersectionObserver' in window)) { showBar(el); return; }
    io = new IntersectionObserver(function (entries) {
      var seen = entries[0] && entries[0].isIntersecting;
      if (seen) hideBar(); else if (current === el && !el.paused) showBar(el);
    }, { threshold: 0.15 });
    io.observe(el);
  }

  document.addEventListener('play', function (e) {
    var el = e.target;
    if (!el || !el.matches || !el.matches(SEL)) return;
    if (current && current !== el && !current.paused) current.pause();
    current = el;
    watch(el);
  }, true);

  document.addEventListener('pause', function (e) {
    if (e.target === current) { hideBar(); if (io) io.disconnect(); }
  }, true);

  document.addEventListener('ended', function (e) {
    if (e.target === current) { current = null; hideBar(); if (io) io.disconnect(); }
  }, true);
})();
