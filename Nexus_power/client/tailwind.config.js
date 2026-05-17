/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // USAA-inspired navy blue palette
        nexus: {
          50:  '#e8f0f9',
          100: '#c5d9ee',
          200: '#9ec0e0',
          300: '#76a5d1',
          400: '#4f8ac3',
          500: '#2670a3',
          600: '#1d5784',
          700: '#164465',
          800: '#0f3250',
          900: '#0a2540',
          950: '#051524',
        },
        // USAA gold accent
        gold: {
          50:  '#fdf8ec',
          100: '#faedca',
          200: '#f4d98e',
          300: '#ecc14f',
          400: '#e0ac2c',
          500: '#D9A23A',
          600: '#b88324',
          700: '#92661d',
          800: '#6b4b15',
          900: '#43300d',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
    },
  },
  plugins: [],
};
