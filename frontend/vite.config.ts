/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  // `--mode demo`: the static recorded demo for GitHub Pages. It bundles
  // demo/recording.json.gz, uses relative URLs (served under /fablink/) and
  // builds to its own folder so it never mixes with the normal build.
  const demo = mode === 'demo'
  return {
    plugins: [react()],
    base: demo ? './' : '/',
    publicDir: demo ? 'demo' : 'public',
    build: { outDir: demo ? 'dist-demo' : 'dist' },
    define: demo ? { 'import.meta.env.VITE_DEMO': JSON.stringify('1') } : {},
    // In dev, the FastAPI backend runs on :8000; proxy REST and WebSocket to it.
    server: {
      proxy: {
        '/api': { target: 'http://127.0.0.1:8000', ws: true },
      },
    },
    test: { environment: 'node' },
  }
})
