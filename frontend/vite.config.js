import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { viteSingleFile } from 'vite-plugin-singlefile'
export default defineConfig({
  base: './',
  plugins: [vue(), viteSingleFile()],
  build: { outDir: '../orchestration/studio_dist', emptyOutDir: true, assetsInlineLimit: 100000000, sourcemap: false },
})
