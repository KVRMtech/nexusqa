import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./src/**/*.{js,ts,jsx,tsx,mdx}'],
  theme: {
    extend: {
      colors: {
        navy: {
          DEFAULT: '#0b1f3a',
          50: '#eef3ff',
          100: '#dbe6f7',
          200: '#b5cdef',
          300: '#7ba8e2',
          400: '#4080d2',
          500: '#1a5dba',
          600: '#12305a',
          700: '#0b1f3a',
          800: '#081529',
          900: '#050d19',
        },
        gold: {
          DEFAULT: '#c8a24a',
          50: '#fdf8eb',
          100: '#f5eac4',
          200: '#e6c877',
          300: '#d4ad52',
          400: '#c8a24a',
          500: '#b08a30',
          600: '#8c6d25',
          700: '#69521c',
          800: '#4a3a14',
          900: '#2e240d',
        },
      },
    },
  },
  plugins: [],
};

export default config;
