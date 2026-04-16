const THEME_KEY = "poe-market-theme";

export function initTheme() {
  const savedTheme = localStorage.getItem(THEME_KEY);
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = savedTheme || (prefersDark ? "dark" : "light");
  setTheme(theme);
}

export function setTheme(theme) {
  const root = document.documentElement;
  if (theme === "light") {
    root.classList.add("light-theme");
    localStorage.setItem(THEME_KEY, "light");
  } else {
    root.classList.remove("light-theme");
    localStorage.setItem(THEME_KEY, "dark");
  }
  updateThemeIcon();
}

export function toggleTheme() {
  const root = document.documentElement;
  const isLight = root.classList.contains("light-theme");
  setTheme(isLight ? "dark" : "light");
}

export function getCurrentTheme() {
  return document.documentElement.classList.contains("light-theme") ? "light" : "dark";
}

function updateThemeIcon() {
  const toggle = document.getElementById("themeToggle");
  if (toggle) {
    const isLight = document.documentElement.classList.contains("light-theme");
    toggle.querySelector(".theme-icon").textContent = isLight ? "☀️" : "🌙";
  }
}
