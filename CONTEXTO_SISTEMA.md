# Contexto del Sistema - Asistente Virtual Medico (FastAPI SSR Multi-tenant)

## 1) Proposito del producto
Sistema SaaS multi-tenant para consultorios medicos que:
- Atiende conversaciones por WhatsApp (bot + derivacion humana).
- Gestiona pacientes, consultorios, turnos y pagos.
- Integra Google Calendar para disponibilidad/reserva de slots.
- Integra Consultorio Movil (Cabildo) para turnos presenciales.
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
- `app/integrations`: clientes externos (Google, Mercado Pago, Cabildo).
- `app/api/routes`: endpoints API/webhooks/internos.
- `app/web/*`: vistas SSR admin/tenant/auth.
- `app/templates`: UI Jinja2.
- `app/static`: JS, CSS, PWA assets.
- `scripts`: init DB, upgrade schema, seed.
- `app/tests`: suite de tests.

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
- `POST /webhook/payments/mercadopago`
- `POST /internal/reminders/run` (token interno)

### Auth
- `GET/POST /login`
- `POST /logout`
- Push: `/push/*`

### Panel tenant (`/t/*`)
- Dashboard, consultorios, pacientes.
- Turnos: `/t/appointments`, detalle, cancelar, reenviar confirmacion.
- Pagos: `/t/payments`, detalle.
- Conversaciones: `/t/conversation-states`, detalle, resolver pendiente.
- Settings:
  - `/t/settings` (perfil tenant + WhatsApp Twilio por tenant)
  - `/t/settings/payments`
  - `/t/settings/calendar`
  - `/t/settings/calendar/test` (test de slots)
  - `/t/settings/notifications`

### Panel admin (`/admin/*`)
- Dashboard, tenants, users.
- Calendarios globales, turnos globales, pagos globales.
- Chat simulator del bot.
- Audit logs y notificaciones.

## 8) Flujo conversacional WhatsApp (actual)
Archivo central: `app/services/conversation_service.py`.

Estados:
- Registro: `ask_first_name`, `ask_last_name`, `ask_dni`, `ask_email`.
- Menu: `main_menu`.
- Turnos: `ask_appointment_for`, `ask_other_dni`, `ask_other_confirm`, `first_time_check`.
- Presencial Cabildo: `ask_presential_slot`, `ask_presential_dni`.
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
  - intenta reservar en Cabildo
  - refleja turno local en DB
  - manejo de errores + audit.

Conversaciones pendientes/finalizadas:
- Bandeja en `/t/conversation-states`.
- Detalle incluye link para responder por WhatsApp.
- Accion manual para resolver y pasar a `finished`.

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

## 11) Integracion Consultorio Movil (Cabildo)
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
5. Configurar consultorio(s) y, si aplica, Cabildo.
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
