/** @type {import('tailwindcss').Config}
 *
 * VKPower Verdict design system. Colors are driven by CSS custom properties
 * declared in `src/theme/tokens.css` (channel triplets → `rgb(var(--x) / a)`),
 * so every hue is themeable from one place AND supports Tailwind opacity
 * modifiers (e.g. `bg-teal/10`, `border-line/60`). The instrument-grade dark
 * identity is the only theme the portal ships.
 */
function ch(v) {
  return `rgb(var(${v}) / <alpha-value>)`;
}

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // ── Surfaces ──
        bg: ch('--bg'),
        'bg-elev': ch('--bg-elev'),
        panel: ch('--panel'),
        'panel-2': ch('--panel-2'),
        inset: ch('--inset'),
        line: ch('--line'),
        'line-strong': ch('--line-strong'),
        // ── Ink (text) ──
        ink: {
          DEFAULT: ch('--ink'),
          mid: ch('--ink-mid'),
          low: ch('--ink-low'),
          faint: ch('--ink-faint'),
        },
        // ── Brand accents (teal → gold) ──
        teal: ch('--teal'),
        // `gold` DEFAULT stays the verdict token (text-gold / bg-gold in the
        // native portal); the numeric stops below are ADDITIVE, consumed only by
        // the ported Test Studio panels (gold-300 / from-gold-400 gradients).
        gold: {
          DEFAULT: ch('--gold'),
          50: '#fdf8ec',
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
        // ── Test Studio (ported video-portal panels) — navy `nexus-*` scale.
        // Additive: nothing in the native verdict UI references it. The panels
        // render in their own polished light identity, embedded in the shell.
        nexus: {
          50: '#e8f0f9',
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
        // ── Semantic verdict colors (separate from the teal accent) ──
        good: ch('--good'),
        warn: ch('--warn'),
        crit: ch('--crit'),
        // ── Verdict kinds (certified | refused | healed) ──
        certified: ch('--verdict-certified'),
        refused: ch('--verdict-refused'),
        healed: ch('--verdict-healed'),
      },
      fontFamily: {
        sans: 'var(--font-ui)',
        ui: 'var(--font-ui)',
        mono: 'var(--font-mono)',
      },
      borderRadius: {
        DEFAULT: 'var(--r-2)',
        sm: 'var(--r-1)',
        md: 'var(--r-2)',
        lg: 'var(--r-3)',
        xl: 'var(--r-4)',
        '2xl': 'var(--r-5)',
      },
      boxShadow: {
        panel: 'var(--shadow-panel)',
        pop: 'var(--shadow-pop)',
        'glow-teal': 'var(--glow-teal)',
        'glow-gold': 'var(--glow-gold)',
        // Ported Test Studio elevation tokens (navy-tinted cards).
        card: '0 1px 2px rgba(15, 50, 80, 0.04), 0 12px 28px -16px rgba(15, 50, 80, 0.18)',
        'card-hover': '0 2px 6px rgba(15, 50, 80, 0.06), 0 20px 44px -20px rgba(15, 50, 80, 0.30)',
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      keyframes: {
        'pulse-dot': {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.45', transform: 'scale(0.82)' },
        },
        'sheen': {
          '0%': { backgroundPosition: '200% 0' },
          '100%': { backgroundPosition: '-200% 0' },
        },
        'fade-in': {
          from: { opacity: '0', transform: 'translateY(4px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        'pulse-dot': 'pulse-dot 1.8s ease-in-out infinite',
        sheen: 'sheen 1.4s linear infinite',
        'fade-in': 'fade-in 0.28s ease-out both',
      },
    },
  },
  plugins: [],
};
