(() => {
  const csrfToken = document.querySelector("meta[name='csrf-token']")?.content;

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

  let deferredInstallPrompt = null;
  const installBanner = document.querySelector("[data-install-banner]");
  const installAccept = document.querySelector("[data-install-accept]");
  const installDismiss = document.querySelector("[data-install-dismiss]");

  const installDismissed = localStorage.getItem("pwa_install_dismissed");
  if (installDismissed) {
    installBanner?.classList.add("hidden");
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredInstallPrompt = event;
    if (installBanner && !installDismissed) {
      installBanner.classList.remove("hidden");
    }
  });

  installAccept?.addEventListener("click", async () => {
    if (!deferredInstallPrompt) return;
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    installBanner?.classList.add("hidden");
  });

  installDismiss?.addEventListener("click", () => {
    installBanner?.classList.add("hidden");
    localStorage.setItem("pwa_install_dismissed", "1");
  });

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", async () => {
      try {
        const registration = await navigator.serviceWorker.register(
          "/static/service-worker.js"
        );
        if (registration.waiting) {
          registration.waiting.postMessage({ type: "SKIP_WAITING" });
        }
      } catch (err) {
        // ignore sw errors
      }
    });

    navigator.serviceWorker.addEventListener("controllerchange", () => {
      window.location.reload();
    });
  }

  const urlBase64ToUint8Array = (base64String) => {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  };

  const pushToggle = document.querySelector("[data-push-toggle]");
  const pushStatus = document.querySelector("[data-push-status]");
  const pushTest = document.querySelector("[data-push-test]");

  const updatePushStatus = (text, state) => {
    if (!pushStatus) return;
    pushStatus.textContent = text;
    pushStatus.dataset.state = state;
  };

  const getVapidKey = async () => {
    const response = await fetch("/push/vapid-public-key");
    const data = await response.json();
    return data.publicKey;
  };

  const getSubscription = async () => {
    if (!navigator.serviceWorker?.ready) return null;
    const registration = await navigator.serviceWorker.ready;
    return registration.pushManager.getSubscription();
  };

  const subscribePush = async () => {
    const publicKey = await getVapidKey();
    if (!publicKey) {
      updatePushStatus("No configurado", "disabled");
      return;
    }
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(publicKey),
    });
    await fetch("/push/subscribe", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken || "",
      },
      body: JSON.stringify(subscription),
    });
    updatePushStatus("Habilitado", "enabled");
  };

  const unsubscribePush = async () => {
    const subscription = await getSubscription();
    if (!subscription) return;
    await fetch("/push/unsubscribe", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken || "",
      },
      body: JSON.stringify({ endpoint: subscription.endpoint }),
    });
    await subscription.unsubscribe();
    updatePushStatus("Deshabilitado", "disabled");
  };

  if (pushToggle && (!("serviceWorker" in navigator) || !("PushManager" in window))) {
    pushToggle.disabled = true;
    updatePushStatus("No soportado", "disabled");
  }

  pushToggle?.addEventListener("change", async (event) => {
    if (event.target.checked) {
      if (!("Notification" in window)) {
        event.target.checked = false;
        updatePushStatus("No soportado", "disabled");
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        event.target.checked = false;
        updatePushStatus("Bloqueado", "blocked");
        return;
      }
      await subscribePush();
    } else {
      await unsubscribePush();
    }
  });

  const ensurePushReady = async () => {
    if (!("Notification" in window) || !("serviceWorker" in navigator) || !("PushManager" in window)) {
      updatePushStatus("No soportado", "disabled");
      return false;
    }
    let subscription = await getSubscription();
    if (!subscription) {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        updatePushStatus("Bloqueado", "blocked");
        return false;
      }
      await subscribePush();
      subscription = await getSubscription();
    }
    return !!subscription;
  };

  pushTest?.addEventListener("click", async () => {
    const ready = await ensurePushReady();
    if (!ready) {
      alert("Necesitas habilitar las notificaciones para probar.");
      return;
    }
    await fetch("/push/test", {
      method: "POST",
      headers: {
        "X-CSRF-Token": csrfToken || "",
      },
    });
  });

  if (pushToggle) {
    getSubscription().then((sub) => {
      if (sub) {
        pushToggle.checked = true;
        updatePushStatus("Habilitado", "enabled");
      } else {
        updatePushStatus("Deshabilitado", "disabled");
      }
    });
  }
})();
