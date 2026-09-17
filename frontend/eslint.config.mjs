// Non-interactive ESLint setup (V9 engineering gap): `next lint` previously
// dropped into the interactive wizard because no config file existed, so lint
// could never run repeatably. Pure Next.js presets — no rule overrides.
import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({
  baseDirectory: dirname(fileURLToPath(import.meta.url)),
});

export default [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  { ignores: ["node_modules/**", ".next/**", "out/**"] },
];
