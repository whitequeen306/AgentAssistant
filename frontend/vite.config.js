import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { viteSingleFile } from 'vite-plugin-singlefile';
import path from 'node:path';
// Single-file build: JS + CSS are inlined into index.html so there are NO
// external resource requests — critical because pywebview loads the file via
// file://, where ES module scripts with crossorigin are CORS-blocked
// (origin 'null'). One self-contained index.html, no assets/ fetches.
// window.py keeps loading web/index.html unchanged.
export default defineConfig({
    base: './',
    plugins: [react(), tailwindcss(), viteSingleFile()],
    resolve: {
        alias: { '@': path.resolve(__dirname, 'src') },
    },
    build: {
        outDir: path.resolve(__dirname, '../src/agent_assistant/ui/web'),
        emptyOutDir: true,
        sourcemap: false,
        target: 'es2020',
        assetsInlineLimit: 100000000,
    },
});
