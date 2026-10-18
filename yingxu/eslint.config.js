export default [
  {
    files: ['src/**/*.{js,jsx}', 'vite.config.js', 'eslint.config.js', 'tools/*.mjs'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    rules: {
      'no-dupe-args': 'error',
      'no-duplicate-case': 'error',
      'no-unexpected-multiline': 'error',
      'valid-typeof': 'error',
    },
  },
];
