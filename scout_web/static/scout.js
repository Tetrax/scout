"use strict";

document.querySelectorAll("[data-discovery-form]").forEach((form) => {
  form.addEventListener("submit", () => {
    const button = form.querySelector("button[type='submit']");
    const idle = form.querySelector("[data-idle]");
    const loading = form.querySelector("[data-loading]");
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
    if (idle) idle.hidden = true;
    if (loading) loading.hidden = false;
  });
});
