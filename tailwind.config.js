/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/templates/**/*.html",
    "./app/static/**/*.js"
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#f8fafc",
          100: "#f1f5f9",
          200: "#e2e8f0",
          300: "#cbd5f5",
          400: "#94a3b8",
          500: "#64748b",
          600: "#475569",
          700: "#334155",
          800: "#1f2937",
          900: "#111827"
        },
        accent: {
          500: "#6366f1",
          600: "#4f46e5"
        }
      },
      boxShadow: {
        soft: "0 1px 2px rgba(15, 23, 42, 0.08)",
        card: "0 10px 30px rgba(15, 23, 42, 0.08)"
      }
    }
  },
  plugins: []
};
