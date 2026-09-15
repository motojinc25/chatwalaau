import { defineConfig } from 'vitest/config'

// Unit tests cover only the Electron-free Main modules (PRP-0167 section 9).
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
  },
})
