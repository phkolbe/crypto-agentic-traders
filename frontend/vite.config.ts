import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * O produto mora em `/crypto-traders/` (telas, `/crypto-traders/api`,
 * `/crypto-traders/ws`). O backend monta o app no mesmo caminho (`api/app.py`,
 * `RAIZ`); o frontend le este valor por `import.meta.env.BASE_URL` (`api/raiz.ts`).
 */
const RAIZ = '/crypto-traders'

export default defineConfig({
  base: `${RAIZ}/`,
  plugins: [react()],
  server: {
    port: 5173,
    // O backend escuta apenas em localhost; o proxy evita CORS no desenvolvimento
    // e mantem a porta da API fora do navegador. Telas em localhost:5173/crypto-traders/.
    proxy: {
      [`${RAIZ}/api`]: { target: 'http://127.0.0.1:8000', changeOrigin: true },
      [`${RAIZ}/ws`]: { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
