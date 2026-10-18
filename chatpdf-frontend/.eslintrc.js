module.exports = {
  parser: '@typescript-eslint/parser', // 指定 ESLint 解析器
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended', // 启用 TypeScript 推荐规则
  ],
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
  },
  rules: {
    // 可以在这里自定义规则
    '@typescript-eslint/no-unused-vars': 'warn',
  },
};