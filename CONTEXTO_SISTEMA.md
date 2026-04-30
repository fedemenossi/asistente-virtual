# Contexto del Sistema - Asistente Virtual Medico (FastAPI SSR Multi-tenant)

## 1) Proposito del producto
Sistema SaaS multi-tenant para consultorios medicos que:
- Atiende conversaciones por WhatsApp (bot + derivacion humana).
- Gestiona pacientes, consultorios, turnos y pagos.
- Integra Google Calendar para disponibilidad/reserva de slots.
- Integra Consultorio Movil para turnos presenciales.
- Integra Mercado Pago para cobros y webhooks.
- Incluye panel web SSR para `SUPER_ADMIN` y `TENANT_ADMIN`.
- Incluye notificaciones in-app + push web (PWA).

## 2) Stack tecnologico
- Backend: Python 3.13, FastAPI, Starlette.
- SSR templates: Jinja2.
- ORM/DB: SQLAlchemy 2 (async), MySQL (aiomysql), SQLite en tests.
- Auth/sesion: SessionMiddleware (cookie de sesion) + roles.
- Password hashing: passlib (`bcrypt` y `pbkdf2_sha256`).
- WhatsApp: Twilio SDK + validacion de firma.
- Pagos: HTTPX contra API de Mercado Pago.
- Calendario: Google API Client + Service Account.
- Frontend: Tailwind CSS, JS vanilla (`app/static/app.js`).
- Testing: pytest + FastAPI TestClient.
- Deploy: Uvicorn (Procfile para plataforma tipo Railway/Heroku).

Dependencias principales: `requirements.txt`, `package.json`.

## 3) Estructura de carpetas
- `app/main.py`: arranque app, middlewares, routers, handlers de error.
- `app/core`: configuracion, DB, seguridad, tenancy, audit, csrf, templates.
- `app/models`: modelos SQLAlchemy.
- `app/repositories`: acceso a datos (repositorios).
- `app/services`: logica de negocio.
- `app/integrations`: clientes externos (Google, Mercado Pago, Consultorio Movil).
- `app/api/routes`: endpoints API/webhooks/internos.
- `app/web/*`: vistas SSR admin/tenant/auth.
- `app/templates`: UI Jinja2.
- `app/static`: JS, CSS, PWA assets.
- `scripts`: init DB, upgrade schema, seed.
- `app/tests`: suite de tests.

### 3.1) Estructura de rutas SSR por panel
#### Tenant panel
- `app/web/tenant/router.py`: define todas las rutas `/t/*`.
- `app/web/tenant/views.py`: handlers (controladores) del panel tenant.

#### Admin panel
- `app/web/admin/router.py`: define todas las rutas `/admin/*`.
- `app/web/admin/views.py`: handlers (controladores) del panel admin.

Nota:
- Hoy no hay módulos separados tipo `appointments.py`, `payments.py`, etc. en `app/web/tenant/` o `app/web/admin/`.
- Todo está centralizado por panel en `router.py` + `views.py`.

### 3.2) Template de menú (sidebar) actual
- Archivo usado: `app/templates/layout/partials/sidebar.html`
- Ese template renderiza items condicionales por rol:
  - `SUPER_ADMIN`: enlaces `/admin/*`
  - `TENANT_ADMIN`: enlaces `/t/*`
- Items actuales en sidebar para `TENANT_ADMIN`:
  - `/t/dashboard`
  - `/t/consultorios`
  - `/t/pacientes`
  - `/t/turnos`
  - `/t/appointments`
  - `/t/payments`
  - `/t/conversation-states`
  - `/t/audit-logs`
  - `/t/notifications`
  - `/t/settings`
  - `/t/settings/payments`
  - `/t/settings/calendar`
  - `/t/settings/notifications`
- Items actuales en sidebar para `SUPER_ADMIN`:
  - `/admin/dashboard`
  - `/admin/tenants`
  - `/admin/users`
  - `/admin/appointments`
  - `/admin/calendars`
  - `/admin/payments`
  - `/admin/settings/payments`
  - `/admin/settings/notifications`
  - `/admin/chat-simulator`
  - `/admin/audit-logs`
  - `/admin/notifications`

## 4) Modelo de autenticacion y permisos
- Roles:
  - `SUPER_ADMIN`: acceso global.
  - `TENANT_ADMIN`: acceso solo a su tenant.
- Sesion en cookie (`SessionMiddleware`).
- Guardas:
  - `require_login`
  - `require_super_admin`
  - `require_tenant_admin`
  - `require_permission(...)`
- Permisos por rol definidos en `app/core/security.py`.

## 5) Modelo de datos (resumen)
### Tenant (`tenants`)
Campos clave:
- `id`, `nombre`, `activo`, `whatsapp_number` (unico).
- Perfil extendido: `fantasy_name`, `first_name`, `last_name`, `cuil`, `address`, `postal_code`, `phone`.
- Config JSON por tenant:
  - `payment_settings`
  - `calendar_settings`
  - `whatsapp_settings` (Twilio por tenant).
- Soft delete: `deleted_at`, `deleted_by`.

### User (`users`)
- `email` unico, `password_hash`, `role`, `tenant_id`, `active`.
- Soft delete.

### Paciente (`pacientes`)
- `tenant_id`, `telefono`, `nombre`, `apellido`, `dni`, `email`, `obra_social`.
- Soft delete.

### Consultorio (`consultorios`)
- `tenant_id`, `nombre`, `tipo` (`presencial|virtual`), `proveedor_turnos`, `configuracion_externa`.
- Soft delete.

### Turno (`turnos`)
- FK a paciente y consultorio.
- `fecha_hora`, `start_at`, `end_at`, `timezone`.
- Estado legacy `estado` + estado moderno `status`.
- Campos de integracion externa (calendar/cabildo).
- Soft delete.

### Payment / PaymentEvent
- Pagos por tenant/paciente/turno, estado de pago y URL.
- Eventos de webhook de pago.

### EstadoConversacion (`estados_conversacion`)
- PK compuesta: `telefono + tenant_id`.
- `estado_actual`, `contexto_json`.
- Bandeja: `status` (`active|pending|finished`), `pending_reason`, `pending_message`, timestamps.

### AuditLog / Notification / PushSubscription
- Auditoria de acciones.
- Notificaciones in-app.
- Suscripciones push web.

## 6) Arquitectura de alto nivel
1. Request entra por FastAPI.
2. Seguridad/rol decide acceso.
3. View/route usa repos/services.
4. SQLAlchemy persiste en MySQL.
5. Integraciones externas se invocan desde servicios.
6. Se registra `AuditLog` y `Notification` cuando corresponde.

Multi-tenant:
- En panel tenant, siempre filtrar por tenant autenticado.
- En panel admin, vista global.
- Webhook WhatsApp resuelve tenant por numero destino (`To`).

## 7) Endpoints y paneles principales
### Public/infra
- `GET /health`
- `GET /` -> `OK`
- `POST /webhook/whatsapp`
- `POST /webhook/whatsapp/{tenant_id}/{secret}`
- `POST /webhook/payments/mercadopago`
- `POST /internal/reminders/run` (token interno)

### Auth
- `GET/POST /login`
- `POST /logout`
- Push: `/push/*`

### Panel tenant (`/t/*`)
- `GET /t/dashboard`
- `GET /t/consultorios`
- `GET /t/consultorios/new`
- `POST /t/consultorios/new`
- `GET /t/consultorios/{consultorio_id}/edit`
- `POST /t/consultorios/{consultorio_id}/edit`
- `POST /t/consultorios/{consultorio_id}/delete`
- `GET /t/pacientes`
- `GET /t/pacientes/new`
- `POST /t/pacientes/new`
- `GET /t/pacientes/{paciente_id}/edit`
- `POST /t/pacientes/{paciente_id}/edit`
- `POST /t/pacientes/{paciente_id}/delete`
- `GET /t/turnos`
- `GET /t/turnos/{turno_id}`
- `GET /t/appointments`
- `GET /t/appointments/{turno_id}`
- `POST /t/appointments/{turno_id}/cancel`
- `POST /t/appointments/{turno_id}/resend`
- `GET /t/payments`
- `GET /t/payments/{payment_id}`
- `GET /t/conversation-states`
- `GET /t/conversation-states/{telefono}`
- `POST /t/conversation-states/{telefono}/resolve`
- `GET /t/audit-logs`
- `GET /t/notifications`
- `POST /t/notifications/{notification_id}/read`
- `GET /t/settings`
- `POST /t/settings`
- `GET /t/settings/payments`
- `POST /t/settings/payments`
- `GET /t/settings/calendar`
- `POST /t/settings/calendar`
- `GET /t/settings/calendar/test`
- `GET /t/settings/notifications`

### Panel admin (`/admin/*`)
- `GET /admin/dashboard`
- `GET /admin/tenants`
- `GET /admin/tenants/new`
- `POST /admin/tenants/new`
- `GET /admin/tenants/{tenant_id}`
- `GET /admin/tenants/{tenant_id}/edit`
- `POST /admin/tenants/{tenant_id}/edit`
- `POST /admin/tenants/{tenant_id}/toggle`
- `POST /admin/tenants/{tenant_id}/delete`
- `GET /admin/users`
- `GET /admin/users/new`
- `POST /admin/users/new`
- `GET /admin/users/{user_id}/edit`
- `POST /admin/users/{user_id}/edit`
- `POST /admin/users/{user_id}/toggle`
- `POST /admin/users/{user_id}/delete`
- `GET /admin/audit-logs`
- `GET /admin/calendars`
- `GET /admin/appointments`
- `GET /admin/payments`
- `GET /admin/payments/{payment_id}`
- `GET /admin/settings/payments`
- `GET /admin/settings/notifications`
- `GET /admin/chat-simulator`
- `POST /admin/chat-simulator/send`
- `POST /admin/chat-simulator/api`
- `GET /admin/chat-simulator/patients`
- `POST /admin/chat-simulator/reset`
- `GET /admin/notifications`
- `POST /admin/notifications/{notification_id}/read`

### API REST / endpoints FastAPI (no SSR)
- `GET /health`
- `GET /`
- `POST /webhook/whatsapp`
- `POST /webhook/whatsapp/{tenant_id}/{secret}`
- `POST /webhook/payments/mercadopago`
- `POST /internal/reminders/run`
- `GET /api/admin/consultorios`
- `POST /api/admin/consultorios`
- `GET /api/admin/consultorios/{consultorio_id}`
- `PUT /api/admin/consultorios/{consultorio_id}`
- `DELETE /api/admin/consultorios/{consultorio_id}`
- `GET /api/admin/tenants`
- `POST /api/admin/tenants`
- `GET /api/admin/tenants/{tenant_id}`
- `PUT /api/admin/tenants/{tenant_id}`
- `DELETE /api/admin/tenants/{tenant_id}`

## 8) Flujo conversacional WhatsApp (actual)
Archivo central: `app/services/conversation_service.py`.

Estados:
- Registro: `ask_first_name`, `ask_last_name`, `ask_dni`, `ask_email`.
- Menu: `main_menu`.
- Turnos: `ask_appointment_for`, `ask_other_dni`, `ask_other_confirm`, `first_time_check`.
- Presencial Consultorio Movil: `ask_presential_slot`, `ask_presential_dni`.
- Derivaciones: `other_detail`, `human_reason`.

Reglas:
- Timeout de inactividad: 30 min (`INACTIVITY_TIMEOUT_MINUTES`).
- Comandos de salida: `salir|cancelar|exit|reiniciar|menu`.
- Menu principal:
  - A/1: turno presencial
  - B/2: turno virtual
  - C/3: otra consulta
  - D/4: humano
- Si consulta/humano:
  - marca `status=pending`
  - guarda motivo/mensaje
  - crea notificacion tenant
  - vuelve a `main_menu`
- Presencial:
  - consulta disponibilidad en Consultorio Movil
  - usuario elige slot
  - intenta reservar en Consultorio Movil
  - refleja turno local en DB
  - manejo de errores + audit.
- Virtual:
  - la infraestructura de turnos local + sincronizacion Google existe en `AppointmentService` / `CalendarService`
  - pero no todo pedido conversacional por WhatsApp dispara reserva automatica; parte del manejo sigue en bandeja operativa/manual

Conversaciones pendientes/finalizadas:
- Bandeja en `/t/conversation-states`.
- Vista tenant y vista global super admin (`/admin/conversation-states`).
- Filtros operativos por estado, categoria, subtipo, adjuntos y ventana temporal.
- Detalle incluye link para responder por WhatsApp.
- Acciones manuales:
  - marcar resuelta
  - volver a pendiente
  - cambiar categoria operativa
  - guardar nota interna breve
- El historial cerrado queda en `conversaciones_historial` y no se elimina.

## 9) Integracion WhatsApp por tenant (Twilio)
### Inbound
- `POST /webhook/whatsapp`:
  1. parsea form Twilio.
  2. resuelve tenant por `To` (numero WhatsApp destino).
  3. valida firma Twilio con:
     - `tenant.whatsapp_settings.twilio_auth_token` si existe.
     - fallback a `TWILIO_AUTH_TOKEN` global.
  4. procesa conversacion.
  5. responde TwiML XML.

### Outbound
- `MessagingService.send_whatsapp(..., tenant=...)` usa:
  - `tenant.whatsapp_settings.twilio_account_sid`
  - `tenant.whatsapp_settings.twilio_auth_token`
  - `tenant.whatsapp_settings.twilio_whatsapp_number`
  - fallback a variables globales.

Configuracion de tenant en UI:
- `/t/settings` -> seccion "WhatsApp (Twilio)".
- Deben cargarse los 3 campos juntos (SID, token, numero).

## 10) Integracion Google Calendar
Archivos:
- `app/services/calendar_service.py`
- `app/integrations/google_calendar_provider.py`

Configuracion por tenant (`calendar_settings`):
- `google_calendar_id`
- `calendar_tags`
- `default_timezone` (recomendado: `America/Argentina/Buenos_Aires`)
- `virtual_meet_enabled`
- `google_credentials_json` (Service Account JSON)
- `google_delegated_user` (opcional)

Flujos:
- Listar slots disponibles: eventos con tags de disponibilidad.
- Reservar slot: patch al evento, agrega datos paciente, opcional Meet.
- Cancelar slot: delete del evento.

UI:
- `/t/settings/calendar`
- Boton "Probar conexion" abre modal con grilla de slots desde `/t/settings/calendar/test`.

## 11) Integracion Consultorio Movil
Archivo: `app/integrations/consultorio_movil.py`.

Funciones:
- login, fetch de disponibilidad, reserva presencial, alta paciente si falta.
- usa timezone por defecto `America/Argentina/Buenos_Aires`.
- requiere config por consultorio en `configuracion_externa.cabildo`:
  - user, password, staff_id, days, timezone.

## 12) Integracion Mercado Pago
Archivos:
- `app/services/payment_service.py`
- `app/integrations/mercadopago_service.py`
- webhook: `POST /webhook/payments/mercadopago`

Flujo:
1. Crea `Payment` pendiente.
2. Genera preference MP y guarda URL.
3. Webhook procesa evento.
4. Mapea estado MP a interno.
5. Actualiza `Payment`, crea `PaymentEvent`, audita.
6. Impacta turno (confirmado/cancelado/waiting_payment).

Credenciales:
- global `.env` o por tenant en `payment_settings`.

## 13) Seguridad y auditoria
- CSRF en formularios SSR (`csrf_token`).
- Auditoria central con `audit_log(...)`.
- Notificaciones por cambios relevantes.
- Soft delete en entidades sensibles.
- Aislamiento tenant en queries de panel tenant.

## 14) Timezone
Sistema guarda timestamps UTC en DB y convierte para vista.
Formato visual local en paneles admin/tenant de auditoria:
- conversion a `America/Argentina/Buenos_Aires` (GMT-3) en views.

Calendario:
- usar `default_timezone = America/Argentina/Buenos_Aires` en settings tenant.

## 15) Configuracion por variables de entorno
Archivo fuente: `.env`.

Principales:
- App: `APP_ENV`, `APP_NAME`, `SECRET_KEY`
- DB: `DATABASE_URL` o `DB_HOST/DB_USER/DB_PASSWORD/DB_NAME/DB_PORT`
- Auth seed: `ADMIN_EMAIL`, `ADMIN_PASSWORD_SEED`
- Twilio global fallback: `TWILIO_*`
- MP global fallback: `MP_ACCESS_TOKEN`, `MP_WEBHOOK_SECRET`
- Google fallback: `GOOGLE_CREDENTIALS_JSON`, `GOOGLE_DELEGATED_USER`
- Push: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`
- Jobs internos: `INTERNAL_JOB_TOKEN`

Importante:
- No subir secretos reales al repositorio.
- Preferir configuracion por tenant para entornos multi-cliente.

## 16) Scripts operativos
- `scripts/init_db.py`: crea tablas.
- `scripts/upgrade_schema.py`: migracion incremental/idempotente (agrega columnas faltantes).
- `scripts/seed_admin.py`: crea super admin si no existe.
- `scripts/seed_demo.py`: crea tenant/consultorio demo.

## 17) Comandos para correr local
### Windows PowerShell
1. Crear/activar venv:
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```
2. Instalar deps:
```powershell
py -m pip install -r requirements.txt
py -m pip install -r requirements-dev.txt
```
3. Inicializar DB:
```powershell
py scripts\init_db.py
py scripts\upgrade_schema.py
py scripts\seed_admin.py
```
4. Correr app:
```powershell
uvicorn app.main:app --reload
```

Frontend Tailwind (opcional en desarrollo):
```powershell
npm install
npm run tailwind:watch
```

## 18) Tests
Ubicacion: `app/tests`.

Coberturas destacadas:
- RBAC/auth.
- Flujo conversacional.
- Webhook pagos.
- Soft delete admin/audit.
- Aislamiento tenant en pagos/turnos.
- Campos extendidos de tenant.
- Endpoint de test de calendario.
- Config WhatsApp por tenant.

Comando:
```powershell
pytest -q
```

Nota: si los tests quedan en passed y no vuelve prompt, revisar recursos/event loop abiertos o procesos colgados de entorno local.

## 19) Riesgos/observaciones actuales (para IA y equipo)
- `Turno` no tiene `tenant_id` directo; el tenant se obtiene via `Turno -> Consultorio -> tenant_id`. Evitar helpers que asuman `model.tenant_id` en `Turno`.
- En `appointment_resend` de tenant view, validar seleccion/indice de columnas al leer filas de SQLAlchemy para evitar errores por index fuera de rango.
- `whatsapp_number` en tenant es unico global; manejar colisiones con validacion previa y errores amigables.
- Google Calendar puede devolver `403 accessNotConfigured` si API no esta habilitada en GCP.

## 20) Checklist para onboarding rapido de un nuevo tenant
1. Crear tenant (admin) con datos base y WhatsApp destino.
2. Crear usuario `TENANT_ADMIN`.
3. Configurar Twilio por tenant en `/t/settings` (SID/token/numero).
4. Configurar Google Calendar en `/t/settings/calendar`.
5. Configurar consultorio(s) y, si aplica, Consultorio Movil.
6. Configurar pagos en `/t/settings/payments`.
7. Probar:
   - webhook WhatsApp (mensaje real o chat simulator admin),
   - prueba de calendario,
   - flujo de turnos y pago.

## 21) Archivos clave para entender rapido
- `app/main.py`
- `app/core/config.py`
- `app/core/security.py`
- `app/web/tenant/router.py`
- `app/web/admin/router.py`
- `app/services/conversation_service.py`
- `app/api/routes/webhook.py`
- `app/services/payment_service.py`
- `app/services/calendar_service.py`
- `app/integrations/google_calendar_provider.py`
- `app/integrations/consultorio_movil.py`
- `scripts/upgrade_schema.py`

---

Este documento esta pensado como contexto base para otra IA (copiar/pegar o adjuntar). Si queres, en un siguiente paso lo puedo separar en 2 archivos:
- `CONTEXT_TECHNICAL.md` (arquitectura y codigo)
- `CONTEXT_OPERATIONS.md` (onboarding, runbooks, troubleshooting).
