/**
 * Onde o produto mora na URL: `/crypto-traders` (telas, API em `/api`, tempo real
 * em `/ws`, tudo debaixo dela).
 *
 * Uma fonte so: o `base` do `vite.config.ts`, que o Vite expoe como
 * `import.meta.env.BASE_URL` ("/crypto-traders/"). O backend monta o app no mesmo
 * caminho (`api/app.py`, `RAIZ`); mudou la, muda no `base`.
 */
export const RAIZ = import.meta.env.BASE_URL.replace(/\/+$/, '')
