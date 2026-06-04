import { initTheme, toggleTheme } from "./ui/theme.js";
import { initAltArtsHoldings } from "./ui/altArtsHoldings.js";

initTheme();
const themeToggle = document.getElementById("themeToggle");
if (themeToggle) {
  themeToggle.addEventListener("click", toggleTheme);
}
initAltArtsHoldings();
