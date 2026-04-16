const switcher = document.getElementById("theme-switcher");
switcher.addEventListener("click", () => {
  const html = document.documentElement;
  if (html.classList.contains("theme-dark")) {
    html.classList.remove("theme-dark");
    html.classList.add("theme-light");
    localStorage.setItem("theme", "light");
  } else if (html.classList.contains("theme-light")) {
    html.classList.remove("theme-light");
    localStorage.removeItem("theme");
  } else {
    html.classList.add("theme-dark");
    localStorage.setItem("theme", "dark");
  }
});

(() => {
  const theme = localStorage.getItem("theme");
  if (theme === "dark") document.documentElement.classList.add("theme-dark");
  if (theme === "light") document.documentElement.classList.add("theme-light");
})();
