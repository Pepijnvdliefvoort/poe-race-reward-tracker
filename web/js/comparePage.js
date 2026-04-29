import { initTheme, toggleTheme } from "./ui/theme.js";
import { initAccountCompare } from "./ui/accountCompare.js";

initTheme();
const themeToggle = document.getElementById("themeToggle");
if (themeToggle) {
  themeToggle.addEventListener("click", toggleTheme);
}
initAccountCompare();

