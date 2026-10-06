const path = require('path');

module.exports = {
  content: [path.join(__dirname, '../templates/**/*.html')],
  theme: {
    extend: {
      colors: {
        bg: '#0d0d0d',
        surface: '#141414',
        surface2: '#1c1c1c',
        line: '#242424',
        line2: '#2c2c2c',
        ink: '#f0f0f0',
        ink2: '#e8e8e8',
        dim: '#888888',
        mute: '#828282',
        faint: '#808080',
        primary: {
          DEFAULT: '#2563eb',
          light: '#3b82f6',
          dark: '#1d4ed8'
        },
        accent: '#22d3ee'
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace']
      }
    }
  },
  plugins: []
};
