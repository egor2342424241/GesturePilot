const $ = (id) => document.getElementById(id);
const navButtons = [...document.querySelectorAll('.nav-item')];
const sections = [...document.querySelectorAll('.section')];
navButtons.forEach(button => button.addEventListener('click', () => {
  navButtons.forEach(item => item.classList.toggle('active', item === button));
  sections.forEach(section => section.classList.toggle('active-section', section.id === button.dataset.section));
}));
const modeButtons = [...document.querySelectorAll('[data-mode]')];
function setMode(mode) {
  modeButtons.forEach(button => button.classList.toggle('active', button.dataset.mode === mode));
  const dual = mode === 'dual';
  $('secondaryControls').classList.toggle('hidden', !dual);
  $('modeStat').textContent = dual ? 'Две камеры' : 'Одна камера';
  $('primaryLabel').textContent = $('primaryCamera').selectedOptions[0].textContent;
  $('sourceName').textContent = $('primaryCamera').selectedOptions[0].textContent;
  $('previewHint').textContent = dual ? 'Ожидает оба источника' : 'Ожидает подключения камеры';
}
modeButtons.forEach(button => button.addEventListener('click', () => setMode(button.dataset.mode)));
$('primaryCamera').addEventListener('change', () => {
  $('primaryLabel').textContent = $('primaryCamera').selectedOptions[0].textContent;
  $('sourceName').textContent = $('primaryCamera').selectedOptions[0].textContent;
});
$('sensitivity').addEventListener('input', e => $('sensitivityValue').value = `${e.target.value}%`);
$('smoothing').addEventListener('input', e => $('smoothingValue').value = `${e.target.value}%`);
$('controlBtn').addEventListener('click', () => {
  const active = $('controlBtn').classList.toggle('enabled');
  $('controlBtn').innerHTML = active ? '<span>Ⅱ</span> Выключить управление' : '<span>▶</span> Включить управление';
  $('globalStatus').classList.toggle('on', active);
  $('globalStatus').innerHTML = active ? '<i></i> Управление активно' : '<i></i> Предпросмотр';
  $('trackingStat').textContent = active ? 'Демо-режим' : 'Ожидание';
  $('trackingFoot').textContent = active ? 'Реальная камера не подключена' : 'Камера не подключена';
  $('qualityText').textContent = active ? 'Демо' : '—';
  $('fpsStat').textContent = '— FPS'; $('latencyStat').textContent = '— мс';
  $('previewHint').textContent = active ? 'Макет: управление включено' : 'Ожидает подключения камеры';
});
$('stopBtn').addEventListener('click', () => {
  $('controlBtn').classList.remove('enabled');
  $('controlBtn').innerHTML = '<span>▶</span> Включить управление';
  $('globalStatus').classList.remove('on'); $('globalStatus').innerHTML = '<i></i> Предпросмотр';
  $('trackingStat').textContent = 'Остановлено'; $('trackingFoot').textContent = 'Ввод отключён';
  $('previewHint').textContent = 'Экстренная остановка (макет)';
});
$('themeBtn').addEventListener('click', () => document.body.classList.toggle('light-mode'));
setMode('single');
