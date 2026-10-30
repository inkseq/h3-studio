import hooks from 'eslint-plugin-react-hooks';

// Findings here always fail; broader rules are a ratchet (eslint.ratchet.config.js).
export default [
  {
    files: ['src/**/*.{js,jsx}', 'vite.config.js', 'eslint.config.js', 'tools/*.mjs'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { 'react-hooks': hooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
      'no-dupe-keys': 'error',
      'no-dupe-args': 'error',
      'no-duplicate-case': 'error',
      'no-unexpected-multiline': 'error',
      'valid-typeof': 'error',
    },
  },
  {
    // Cleared to zero in the app source (design#293); kept at zero from here on.
    files: ['src/**/*.{js,jsx}'],
    rules: {
      'no-unused-vars': [
        'error',
        {
          caughtErrors: 'none',
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^(_|React$)',
          ignoreRestSiblings: true,
        },
      ],
      'no-empty': 'error',
      'preserve-caught-error': 'error',
      'no-useless-assignment': 'error',
      'no-loss-of-precision': 'error',
    },
  },
];
