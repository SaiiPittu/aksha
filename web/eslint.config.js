import js from "@eslint/js"
import globals from "globals"
import reactHooks from "eslint-plugin-react-hooks"
import reactRefresh from "eslint-plugin-react-refresh"
import tseslint from "typescript-eslint"

export default tseslint.config(
  { ignores: ["dist", "node_modules", "playwright-report", "test-results"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { ecmaVersion: 2023, globals: globals.browser },
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": "off",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  {
    // Copied component libraries keep their upstream style.
    files: ["src/components/**/*.{ts,tsx}"],
    rules: { "@typescript-eslint/no-explicit-any": "off", "@typescript-eslint/no-empty-object-type": "off" },
  },
  {
    files: ["*.config.ts", "e2e/**/*.ts"],
    languageOptions: { globals: globals.node },
  },
)
