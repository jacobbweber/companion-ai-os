import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  define:
    mode === 'test'
      ? {}
      : {
          'process.env.NODE_ENV': JSON.stringify('production'),
        },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    cssCodeSplit: false,
    target: 'es2022',
    minify: 'esbuild',
    lib: {
      entry: 'src/main.tsx',
      name: 'CompanionStudioApp',
      formats: ['iife'],
      fileName: () => 'app.js',
    },
    rollupOptions: {
      output: {
        assetFileNames: (assetInfo) => {
          if (assetInfo.name && assetInfo.name.endsWith('.css')) {
            return 'styles.css';
          }
          return '[name][extname]';
        },
      },
    },
  },
}));
