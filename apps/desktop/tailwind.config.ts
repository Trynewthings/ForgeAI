import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#101828",
        mist: "#d0d5dd",
        surface: "#f8fafc",
        accent: "#0f766e",
        glow: "#f59e0b"
      },
      fontFamily: {
        sans: ["IBM Plex Sans", "Segoe UI", "sans-serif"]
      },
      boxShadow: {
        panel: "0 24px 80px rgba(16, 24, 40, 0.16)"
      }
    }
  },
  plugins: []
} satisfies Config;

