// Генерирует самостоятельный сабсет Font Awesome (только используемые на сайте
// иконки) — woff2-шрифты + минимальный CSS с реальными unicode-кодами глифов.
// Добавили новую иконку в шаблон — впишите её сюда и перезапустите build.sh.
const fs = require('fs');
const path = require('path');
const { fontawesomeSubset } = require('fontawesome-subset');
const iconFamilies = require('./node_modules/@fortawesome/fontawesome-free/metadata/icon-families.json');

const solidIcons = [
  'arrow-left', 'arrows-rotate', 'ban', 'bug', 'building-shield',
  'chevron-down', 'circle-check', 'circle-play', 'circle-question',
  'circle-xmark', 'clock', 'credit-card',
  'database', 'feather', 'gauge', 'gauge-high', 'gift', 'globe',
  'hammer', 'headset', 'key', 'lock', 'mask', 'qrcode', 'server',
  'shuffle', 'sliders', 'triangle-exclamation', 'up-right-from-square',
  'user-plus', 'user-secret', 'tv', 'mobile-screen',
];

const brandIcons = [
  'android', 'apple', 'chrome', 'google', 'linux', 'telegram',
  'vk', 'windows', 'yandex-international',
];

const FONTS_OUT = path.join(__dirname, '../static/fonts/fa');
const CSS_OUT = path.join(__dirname, '../static/css/fa-subset.css');

async function main() {
  await fontawesomeSubset(
    { solid: solidIcons, brands: brandIcons },
    FONTS_OUT,
    { targetFormats: ['woff2'] }
  );

  let css = `/* Font Awesome — самостоятельный сабсет: только используемые на сайте иконки
   (solid+brands). Сгенерировано website/tailwind/build-fa-subset.js из
   ${solidIcons.length + brandIcons.length} иконок вместо полной библиотеки all.min.css. */
@font-face {
  font-family: 'Font Awesome 7 Free';
  font-style: normal;
  font-weight: 900;
  font-display: swap;
  src: url('/static/fonts/fa/fa-solid-900.woff2') format('woff2');
}
@font-face {
  font-family: 'Font Awesome 7 Brands';
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url('/static/fonts/fa/fa-brands-400.woff2') format('woff2');
}
.fa-solid, .fas {
  font-family: 'Font Awesome 7 Free';
  font-weight: 900;
}
.fa-brands, .fab {
  font-family: 'Font Awesome 7 Brands';
  font-weight: 400;
}
[class^="fa-"], [class*=" fa-"] {
  display: inline-block;
  font-style: normal;
  font-variant: normal;
  line-height: 1;
  text-rendering: auto;
}
`;

  for (const name of solidIcons) {
    css += `.fa-${name}::before { content: "\\${iconFamilies[name].unicode}"; }\n`;
  }
  for (const name of brandIcons) {
    css += `.fa-${name}::before { content: "\\${iconFamilies[name].unicode}"; }\n`;
  }

  fs.writeFileSync(CSS_OUT, css);
  console.log(`OK: ${CSS_OUT} (${solidIcons.length + brandIcons.length} иконок)`);
}

main();
