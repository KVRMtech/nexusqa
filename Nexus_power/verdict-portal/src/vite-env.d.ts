/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the QE-Central API (empty = same-origin relative requests). */
  readonly VITE_QEC_API_URL?: string;
  /** '1' enables mock mode: the API client returns design data, no network. */
  readonly VITE_QEC_MOCK?: string;
  /** The JWT audience the QE-Central gate expects (informational, displayed). */
  readonly VITE_QEC_AUDIENCE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
