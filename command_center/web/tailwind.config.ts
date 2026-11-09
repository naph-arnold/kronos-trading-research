import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#07090d", 900: "#0b0f15", 850: "#10151d", 800: "#151b25", 700: "#1e2633", 600: "#2a3444" },
        line: "#232c3a",
        mute: "#8692a6",
        up: "#26a69a",
        down: "#ef5350",
        warn: "#f5a524",
        info: "#4f9cf9",
        sim: "#b48cf2",
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Mono", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
