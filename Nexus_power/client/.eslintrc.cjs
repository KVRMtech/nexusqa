// ── Client lint gate ────────────────────────────────────────────────────────
//
// WHY THIS FILE EXISTS. package.json has shipped `"lint": "eslint . --ext ts,tsx"`
// for the whole life of this client, and .github/workflows/ci.yml's `frontend`
// job runs it — but eslint was in neither `dependencies` nor `devDependencies`,
// had ZERO entries in package-lock.json, and no config file existed anywhere in
// the project. After `npm ci` the binary was simply absent, so the step could
// only ever exit "eslint: not found". It was a phantom gate: it looked like the
// frontend was linted, and nothing was ever linted.
//
// It is wired up for real here rather than deleted, because deleting it is how a
// gate silently stops existing. The rule set is chosen on the SAME doctrine as
// ruff.toml on the Python side: select the CORRECTNESS rules that are clean on
// this tree TODAY, so any new violation is a genuine blocking regression, and
// record the style backlog with its measured count as a ratchet to be cleared in
// its own reviewed commit — never as a side effect of turning CI on.
//
// Measured on this tree with eslint:recommended + @typescript-eslint/recommended:
// 440 errors, 9 warnings. The disposition of every one of them is below.
module.exports = {
  root: true,
  env: { browser: true, es2020: true, node: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
  ],
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 2020, sourceType: 'module', ecmaFeatures: { jsx: true } },
  plugins: ['@typescript-eslint', 'react-hooks'],
  ignorePatterns: ['dist', 'node_modules', 'coverage', 'playwright-report', '*.cjs'],
  rules: {
    // ── ENFORCED. Clean at zero today, so these genuinely block. ────────────
    //
    // rules-of-hooks is the one that catches a real crash: a hook behind a
    // condition or a loop corrupts React's hook order at runtime. Zero findings,
    // so it costs nothing to enforce and everything to omit.
    'react-hooks/rules-of-hooks': 'error',

    // Everything inherited from the two `extends` above stays ERROR: no-undef,
    // no-dupe-keys, no-unreachable, no-fallthrough, no-misused-new, and the rest
    // of the correctness surface. All already at zero on this tree.

    // `checkLoops: false` ONLY. `while (true)` with an explicit break/return is
    // the intended idiom in both places it appears — api.ts:441 pages an endpoint
    // until it returns an empty page, SessionCommandPage.tsx:995 is a bounded
    // worker pulling from a shared index — and neither is a bug. The rule keeps
    // flagging genuine constant conditions everywhere else (`if (true)`, a
    // constant ternary test), which is the half of it that catches mistakes.
    'no-constant-condition': ['error', { checkLoops: false }],

    // ── DEFERRED RATCHET. Counted, not hidden. Clear a backlog, flip the rule
    //    back to 'error' in its own commit, and it can never regress again. ──

    // 267 findings. A typing backlog, not a correctness one — `any` is legal
    // TypeScript and `tsc --noEmit` (which DOES gate, in the same CI job) already
    // proves the tree compiles under `"strict": true`. Turning this on today
    // would mean 267 blocking errors on a gate that has to be green to ship M0,
    // and the fix for each is a real typing decision, not a mechanical edit.
    '@typescript-eslint/no-explicit-any': 'off',

    // 171 findings. Mostly unused imports and unused catch bindings. Worth
    // clearing — it occasionally hides a genuine mistake — but it is a 171-site
    // sweep that wants its own diff where each removal can actually be reviewed.
    // tsconfig.json's noUnusedLocals/noUnusedParameters are likewise false today;
    // this rule and those two flags should be turned on together.
    '@typescript-eslint/no-unused-vars': 'off',

    // 9 findings, all in already-working components. Left as WARN deliberately:
    // `npm run lint` does not fail on warnings, so these are reported on every
    // run and block nothing. A missing dep here is a stale-closure risk that
    // needs each call site reasoned about — "add the dep the linter named" is
    // exactly how an effect starts looping. Not a mechanical fix, so not a gate.
    'react-hooks/exhaustive-deps': 'warn',
  },
};
