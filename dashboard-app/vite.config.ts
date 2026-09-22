import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The console talks to server.py. Proxying in dev means the app ships one origin and no
// CORS or base URL to get wrong on someone else's machine. The port is pinned rather
// than auto-picked: vite silently moves to the next free one, and a proxy on a port the
// README does not name is a confusing ten minutes for whoever clones this.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5180,
    strictPort: true,
    proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true } },
  },
})
