import { defineConfig } from 'vitest/config'

// Separate from vite.config.ts on purpose. Vitest resolves its own copy of
// Vite, so sharing one config file makes `@vitejs/plugin-react`'s Plugin type
// and Vitest's disagree and the type check fails with a wall of variance
// errors. Nothing under test needs the React plugin — every test file is plain
// TypeScript over pure functions — so this config simply omits it.
export default defineConfig({
  test: {
    // jsdom for the few tests that touch a DOM element; the rest would run
    // just as happily in node.
    environment: 'jsdom',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
})
