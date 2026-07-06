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

    toggle.setAttribute("aria-expanded", "false");

    const close = () => {
      menu.classList.add("hidden");
      toggle.setAttribute("aria-expanded", "false");
    };
    const open = () => {
      menu.classList.remove("hidden");
      toggle.setAttribute("aria-expanded", "true");
    };

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

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
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

  const chatRoot = document.querySelector("[data-chat-simulator]");
  if (chatRoot) {
    const form = chatRoot.querySelector("[data-chat-form]");
    const log = chatRoot.querySelector("[data-chat-log]");
    const input = chatRoot.querySelector("[data-chat-input]");
    const tenantSelect = chatRoot.querySelector("[data-chat-tenant]");
    const patientSelect = chatRoot.querySelector("[data-chat-patient]");
    const resetForm = chatRoot.querySelector("[data-chat-reset]");
    const phoneInput = chatRoot.querySelector("[data-chat-phone]");

    const STORAGE_TO = "chat_simulator_to";
    const STORAGE_PATIENT = "chat_simulator_patient";
    const STORAGE_PHONE = "chat_simulator_phone";

    const setDefaults = () => {
      const savedTo = localStorage.getItem(STORAGE_TO);
      const savedPhone = localStorage.getItem(STORAGE_PHONE);
      if (tenantSelect && savedTo) {
        const option = tenantSelect.querySelector(`option[value="${savedTo}"]`);
        if (option) {
          tenantSelect.value = savedTo;
        } else {
          localStorage.removeItem(STORAGE_TO);
        }
      }
      if (phoneInput && savedPhone) {
        phoneInput.value = savedPhone;
      }
    };

    const persistDefaults = () => {
      if (tenantSelect?.value) {
        localStorage.setItem(STORAGE_TO, tenantSelect.value);
      }
      if (patientSelect?.value) {
        localStorage.setItem(STORAGE_PATIENT, patientSelect.value);
      }
      if (phoneInput?.value) {
        localStorage.setItem(STORAGE_PHONE, phoneInput.value);
      }
      if (resetForm) {
        const toHidden = resetForm.querySelector('input[name="to_number"]');
        const fromHidden = resetForm.querySelector('input[name="from_number"]');
        if (toHidden) {
          const selectedTenant = tenantSelect?.selectedOptions[0];
          toHidden.value = selectedTenant?.dataset?.whatsapp || "";
        }
        if (fromHidden) {
          const selected = patientSelect?.selectedOptions[0];
          const manual = phoneInput?.value?.trim() || "";
          fromHidden.value = selected?.dataset?.phone || manual;
        }
      }
    };

    const syncPhoneInput = () => {
      if (!phoneInput) return;
      const selected = patientSelect?.selectedOptions[0];
      const manual = localStorage.getItem(STORAGE_PHONE) || "";
      if (selected?.value) {
        phoneInput.value = selected?.dataset?.phone || "";
        phoneInput.readOnly = true;
        phoneInput.classList.add("bg-slate-100");
      } else {
        phoneInput.readOnly = false;
        phoneInput.classList.remove("bg-slate-100");
        if (manual) {
          phoneInput.value = manual;
        }
      }
    };

    const appendMessage = (label, text) => {
      if (!log) return;
      const wrapper = document.createElement("div");
      wrapper.className = "mb-3";
      const title = document.createElement("div");
      title.className = "text-xs text-slate-500";
      title.textContent = label;
      const body = document.createElement("div");
      body.className = "text-slate-900";
      body.textContent = text;
      wrapper.appendChild(title);
      wrapper.appendChild(body);
      log.appendChild(wrapper);
      log.scrollTop = log.scrollHeight;
    };

    const loadPatients = async () => {
      if (!tenantSelect || !patientSelect) return;
      const tenantId = tenantSelect.value;
      if (!tenantId) return;
      patientSelect.innerHTML = '<option value="">Seleccionar paciente</option>';
      const response = await fetch(`/admin/chat-simulator/patients?tenant_id=${tenantId}`);
      const data = await response.json();
      (data.items || []).forEach((item) => {
        const opt = document.createElement("option");
        opt.value = String(item.id);
        opt.textContent = `${item.nombre} ${item.apellido} - ${item.telefono}`;
        opt.dataset.phone = item.telefono || "";
        patientSelect.appendChild(opt);
      });
      const savedPatient = localStorage.getItem(STORAGE_PATIENT);
      if (savedPatient) {
        patientSelect.value = savedPatient;
      }
      syncPhoneInput();
    };

    setDefaults();
    loadPatients().then(() => {
      persistDefaults();
    });
    tenantSelect?.addEventListener("change", async () => {
      localStorage.removeItem(STORAGE_PATIENT);
      persistDefaults();
      await loadPatients();
    });
    patientSelect?.addEventListener("change", () => {
      syncPhoneInput();
      persistDefaults();
    });
    phoneInput?.addEventListener("input", () => {
      if (!patientSelect?.value) {
        persistDefaults();
      }
    });

    form?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const selectedTenant = tenantSelect?.selectedOptions[0];
      const toNumber = selectedTenant?.dataset?.whatsapp?.trim();
      const selected = patientSelect?.selectedOptions[0];
      const manualNumber = phoneInput?.value?.trim();
      const fromNumber = selected?.dataset?.phone?.trim() || manualNumber;
      const message = input?.value?.trim();
      if (!toNumber || !fromNumber || !message) {
        alert("Selecciona tenant, ingresa un telefono valido y escribe un mensaje.");
        return;
      }
      persistDefaults();
      appendMessage("Paciente", message);
      if (input) input.value = "";

      try {
        const resp = await fetch("/admin/chat-simulator/api", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken || "",
          },
          body: JSON.stringify({
            to_number: toNumber,
            from_number: fromNumber,
            patient_id: patientSelect?.value || null,
            message,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          appendMessage("Bot", data.error || "Error al enviar.");
          return;
        }
        appendMessage("Bot", data.reply || "(sin respuesta)");
      } catch (err) {
        appendMessage("Bot", "No se pudo contactar al servidor.");
      }
    });
  }

  const providerSelect = document.querySelector("[data-provider-select]");
  const cabildoFields = document.querySelector("[data-cabildo-fields]");
  if (providerSelect && cabildoFields) {
    const toggleCabildo = () => {
      if (providerSelect.value === "consultorio_movil") {
        cabildoFields.classList.remove("hidden");
      } else {
        cabildoFields.classList.add("hidden");
      }
    };
    providerSelect.addEventListener("change", toggleCabildo);
    toggleCabildo();
  }

  const calendarTestButton = document.querySelector("[data-calendar-test]");
  const calendarModal = document.querySelector("[data-calendar-modal]");
  const calendarModalClose = document.querySelectorAll("[data-calendar-modal-close]");
  const calendarTestBody = document.querySelector("[data-calendar-test-body]");
  const calendarTestCount = document.querySelector("[data-calendar-test-count]");
  const calendarTestError = document.querySelector("[data-calendar-test-error]");

  const formatDateTime = (value) => {
    if (!value) return "";
    try {
      return new Date(value).toLocaleString("es-AR");
    } catch (err) {
      return value;
    }
  };

  const resetCalendarTest = () => {
    calendarTestBody.innerHTML = "";
    calendarTestError.textContent = "";
    calendarTestError.classList.add("hidden");
    calendarTestCount.textContent = "";
  };

  const openCalendarModal = () => {
    if (!calendarModal) return;
    calendarModal.classList.remove("hidden");
    calendarModal.classList.add("flex");
  };

  const closeCalendarModal = () => {
    if (!calendarModal) return;
    calendarModal.classList.add("hidden");
    calendarModal.classList.remove("flex");
  };

  calendarModalClose.forEach((btn) => {
    btn.addEventListener("click", closeCalendarModal);
  });

  calendarModal?.addEventListener("click", (event) => {
    if (event.target === calendarModal) {
      closeCalendarModal();
    }
  });

  calendarTestButton?.addEventListener("click", async (event) => {
    event.preventDefault();
    if (!calendarModal || !calendarTestBody || !calendarTestCount || !calendarTestError) {
      return;
    }
    openCalendarModal();
    resetCalendarTest();
    calendarTestButton.disabled = true;
    calendarTestButton.textContent = "Probando...";
    try {
      const resp = await fetch("/t/settings/calendar/test");
      const data = await resp.json();
      if (!resp.ok) {
        calendarTestError.textContent = data.error || "No se pudo conectar al calendario.";
        calendarTestError.classList.remove("hidden");
        calendarTestCount.textContent = "0 slots";
        return;
      }
      const items = data.items || [];
      calendarTestCount.textContent = `${data.count || items.length} slots`;
      if (items.length === 0) {
        calendarTestError.textContent = "No se encontraron slots disponibles.";
        calendarTestError.classList.remove("hidden");
        return;
      }
      items.forEach((item) => {
        const row = document.createElement("tr");
        row.className = "border-t border-slate-200";
        row.innerHTML = `
          <td class="px-4 py-3 text-slate-900">${formatDateTime(item.start_at)}</td>
          <td class="px-4 py-3 text-slate-600">${formatDateTime(item.end_at)}</td>
          <td class="px-4 py-3 text-slate-600">${item.timezone || "-"}</td>
          <td class="px-4 py-3 text-slate-600">${item.slot_id || "-"}</td>
        `;
        calendarTestBody.appendChild(row);
      });
    } catch (err) {
      calendarTestError.textContent = "Error al ejecutar la prueba.";
      calendarTestError.classList.remove("hidden");
    } finally {
      calendarTestButton.disabled = false;
      calendarTestButton.textContent = "Probar conexion";
    }
  });

  document.querySelectorAll("form[data-submit-label]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit']");
      if (!button) return;
      button.dataset.originalText = button.textContent || "";
      button.textContent = form.dataset.submitLabel || "Procesando...";
      button.disabled = true;
    });
  });
})();
