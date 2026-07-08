import { initTheme, toggleTheme } from "./ui/theme.js";
import { initAaLadder } from "./ui/aaLadder.js";

initTheme();
const themeToggle = document.getElementById("themeToggle");
if (themeToggle) {
  themeToggle.addEventListener("click", toggleTheme);
}

initAaLadder();
