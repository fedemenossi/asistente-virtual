(() => {
  const sidebar = document.querySelector("[data-sidebar]");
  const overlay = document.querySelector("[data-sidebar-overlay]");
  const toggles = document.querySelectorAll("[data-sidebar-toggle]");
  const closeBtn = document.querySelector("[data-sidebar-close]");

  const openSidebar = () => {
    if (!sidebar || !overlay) return;
    sidebar.classList.remove("-translate-x-full");
    overlay.classList.remove("hidden");
    document.body.classList.add("overflow-hidden");
  };

  const closeSidebar = () => {
    if (!sidebar || !overlay) return;
    sidebar.classList.add("-translate-x-full");
    overlay.classList.add("hidden");
    document.body.classList.remove("overflow-hidden");
  };

  toggles.forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!sidebar) return;
      if (sidebar.classList.contains("-translate-x-full")) {
        openSidebar();
      } else {
        closeSidebar();
      }
    });
  });

  closeBtn?.addEventListener("click", closeSidebar);
  overlay?.addEventListener("click", closeSidebar);

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeSidebar();
    }
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth >= 1024) {
      closeSidebar();
    }
  });

  const dropdowns = document.querySelectorAll("[data-dropdown]");
  dropdowns.forEach((dropdown) => {
    const toggle = dropdown.querySelector("[data-dropdown-toggle]");
    const menu = dropdown.querySelector("[data-dropdown-menu]");
    if (!toggle || !menu) return;

    const close = () => menu.classList.add("hidden");
    const open = () => menu.classList.remove("hidden");

    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      if (menu.classList.contains("hidden")) {
        open();
      } else {
        close();
      }
    });

    document.addEventListener("click", (event) => {
      if (!dropdown.contains(event.target)) {
        close();
      }
    });
  });

  const toasts = document.querySelectorAll("[data-toast]");
  toasts.forEach((toast) => {
    setTimeout(() => {
      toast.classList.add("opacity-0", "translate-y-1");
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  });
})();
