# Contexto del Sistema - Asistente Virtual Medico (FastAPI SSR Multi-tenant)

Revision actual: 2026-05-24.

Nota de estado real:
- En la raiz actual no existen `README.md` ni `REBUILD.md`; los README vigentes son especificos por modulo.
- La suite actual corre verde con `.\.venv\Scripts\python -m pytest -q` (`71 passed` al 2026-05-24).
- Este documento refleja el codigo actual, no solo la arquitectura objetivo.

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
  - `/admin/conversation-states`
  - `/admin/calendars`
  - `/admin/payments`
  - `/admin/settings/notifications`
  - `/admin/chat-simulator`
  - `/admin/tenant-features`
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
- `tenant_id` directo + FK a paciente y consultorio.
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
- `Turno` tiene `tenant_id` directo y las vistas tambien validan relacion con `Consultorio.tenant_id`.

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
- `GET /t/conversation-states/history/{history_id}`
- `POST /t/conversation-states/{telefono}/resolve`
- `POST /t/conversation-states/{telefono}/review`
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
- `GET /admin/conversation-states`
- `GET /admin/conversation-states/{tenant_id}/{telefono}`
- `GET /admin/conversation-states/history/{history_id}`
- `POST /admin/conversation-states/{tenant_id}/{telefono}/resolve`
- `POST /admin/conversation-states/{tenant_id}/{telefono}/review`
- `GET /admin/payments`
- `GET /admin/payments/{payment_id}`
- `GET /admin/settings/notifications`
- `GET /admin/chat-simulator`
- `POST /admin/chat-simulator/send`
- `POST /admin/chat-simulator/api`
- `GET /admin/chat-simulator/patients`
- `POST /admin/chat-simulator/reset`
- `GET /admin/tenant-features`
- `GET /admin/tenant-features/{tenant_id}`
- `POST /admin/tenant-features/{tenant_id}`
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
- Registro actual: `ask_first_name`, `ask_last_name`, `ask_dni`, `ask_insurance`, `ask_insurance_number`, `ask_email`.
- Menu actual: `main_reason_menu` (se conserva `main_menu` como compatibilidad legacy).
- Turno presencial: `ask_presential_for_whom`, datos de otra persona si aplica, `ask_presential_first_time`.
- Turno virtual: `ask_virtual_for_whom`, datos de otra persona si aplica, `ask_virtual_first_time`.
- Receta/orden: `ask_recipe_kind`, `ask_recipe_detail`.
- Otras consultas: `ask_other_query`.
- Estados legacy conservados: `ask_appointment_for`, `ask_other_dni`, `ask_other_confirm`, `ask_presential_slot`, `ask_presential_dni`, `first_time_check`, `other_detail`, `human_reason`.

Reglas:
- Timeout de inactividad: 30 min (`INACTIVITY_TIMEOUT_MINUTES`).
- Comandos de salida: `salir|cancelar|exit|reiniciar|menu`.
- Menu principal:
  - 1: turno presencial
  - 2: turno virtual
  - 3: solicitar receta u orden medica
  - 4: otra consulta
  - 5: hablar con una persona
- Si consulta/turno/receta/humano:
  - marca `status=pending`
  - guarda motivo/mensaje
  - crea notificacion tenant
  - queda en bandeja operativa para gestion manual/semi-manual
- Turnos:
  - la infraestructura local y la sincronizacion externa existen en `AppointmentService` / `CalendarService`
  - el bot actual NO agenda automaticamente la mayoria de pedidos por WhatsApp
  - las solicitudes de turno presencial/virtual quedan como conversaciones pendientes clasificadas

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

### 8.1) Clasificador IA de intencion - Etapa 1
Archivos:
- `app/services/ai_intent_classifier.py`
- `app/services/tenant_ai_settings_service.py`
- Configuracion persistida en `tenants.ai_settings` (JSON).

Objetivo:
- Mejorar comprension de texto libre en `main_reason_menu` / `main_menu`.
- Devolver intencion normalizada, confianza, datos detectados y fuente (`rules`, `ai`, `fallback`).
- Mantener el flujo existente como respaldo.

Intents soportados:
- `book_presential_appointment`
- `book_virtual_appointment`
- `recipe_or_order`
- `other_medical_query`
- `human_handoff`
- `cancel_appointment`
- `reschedule_appointment`
- `greeting`
- `exit`
- `unknown`

Estado actual:
- Por defecto corre solo con reglas locales.
- La IA real se configura por tenant y viene desactivada por defecto con `ai_settings.enabled=false`.
- El `SUPER_ADMIN` puede configurar el agente en alta/edicion de tenant.
- Cada tenant puede tener su propia `api_key`, modelo, umbral de confianza, timeout, prompt, personalidad e intents permitidos.
- Si se activa IA y no hay API key del tenant, se permite fallback global explicito con `OPENAI_API_KEY` si existe.
- Si no hay API key disponible o falla la llamada, vuelve a reglas/fallback.
- La respuesta IA se pide como JSON estricto y no se envia al paciente.
- Desde Etapa 2 tambien extrae datos estructurados y los guarda en `EstadoConversacion.contexto_json`.
- La API key no se muestra completa en templates ni se loguea; en edicion se muestra enmascarada.

Que NO hace el agente en estas etapas:
- No reserva turnos.
- No consulta Calendar.
- No llama Consultorio Movil.
- No crea pagos.
- No cancela ni reprograma turnos.
- No modifica entidades criticas.

Mapeo actual en menu principal si `confidence >= ai_settings.min_confidence`:
- `book_presential_appointment` -> inicia flujo presencial existente.
- `book_virtual_appointment` -> inicia flujo virtual existente.
- `recipe_or_order` -> inicia flujo receta/orden existente.
- `other_medical_query` -> inicia flujo de otra consulta existente.
- `human_handoff` -> deriva a humano como el flujo existente.
- `exit` -> finaliza/reinicia conversacion como el flujo existente.
- `cancel_appointment`, `reschedule_appointment`, `greeting`, `unknown` -> no ejecutan acciones; se pide elegir una opcion valida.

Configuracion del Agente de IA por tenant:
```json
{
  "enabled": false,
  "provider": "openai",
  "api_key": "",
  "model": "gpt-4o-mini",
  "min_confidence": 0.75,
  "timeout_seconds": 8,
  "agent_name": "Asistente virtual",
  "system_prompt": "",
  "personality": "cordial, clara, profesional y breve",
  "allowed_intents": [
    "book_presential_appointment",
    "book_virtual_appointment",
    "recipe_or_order",
    "other_medical_query",
    "human_handoff",
    "cancel_appointment",
    "reschedule_appointment",
    "greeting",
    "exit",
    "unknown"
  ],
  "handoff_on_low_confidence": true,
  "max_tokens": 400,
  "temperature": 0.0
}
```

Seguridad:
- Preferir `ai_settings.api_key` por tenant.
- `OPENAI_API_KEY` global queda solo como fallback explicito.
- No exponer `api_key` completa en HTML, logs ni respuestas.
- `TENANT_ADMIN` no edita `ai_settings` en esta etapa; solo `SUPER_ADMIN`.
- `allowed_intents` limita las intenciones que puede devolver la IA.

Prueba manual rapida:
1. Con un paciente registrado, enviar "hola" para entrar al menu.
2. Enviar texto libre como "necesito turno en consultorio".
3. Debe continuar por el flujo de turno presencial existente, sin crear turno ni llamar proveedores externos.
4. Para IA real, habilitar el agente en `/admin/tenants/{tenant_id}/edit` y cargar API key/modelo.
5. Ejecutar `.\.venv\Scripts\python -m pytest -q`.

### 8.2) Etapa 2 IA - Extraccion de datos
Archivos:
- `app/services/ai_extraction_service.py`
- `app/services/ai_intent_classifier.py`
- `app/services/conversation_service.py`

Objetivo:
- Extraer datos utiles del mensaje libre del paciente y persistirlos en `EstadoConversacion.contexto_json`.
- Evitar preguntas repetidas cuando el paciente ya dio datos claros.
- Mantener el flujo stateful actual: la IA no ejecuta acciones, solo mejora comprension y contexto.

Datos que puede extraer:
- Datos del paciente: nombre, apellido, DNI, email, obra social y numero de afiliado.
- Turnos: tipo presencial/virtual, si es para el paciente u otra persona, nombre/DNI de otra persona, primera vez, dia/fecha/hora/franja preferida.
- Recetas/ordenes: tipo de solicitud y detalle expresado.
- Operacion: nivel de urgencia y si conviene derivar a humano.

Persistencia sugerida/actual:
```json
{
  "ai": {
    "last_intent": "book_virtual_appointment",
    "last_confidence": 0.91,
    "last_source": "rules",
    "extracted": {},
    "missing_fields": ["is_first_time"]
  },
  "appointment": {
    "type": "virtual",
    "for": "other",
    "preferred_day": "martes",
    "preferred_time_range": "tarde"
  },
  "other_patient": {
    "name": "Juan Perez",
    "dni": "40111222"
  },
  "recipe_order": {
    "type": "receta",
    "detail": "medicacion habitual"
  }
}
```

Reglas de seguridad:
- No pisa datos existentes confiables con valores vacios/null.
- Normaliza DNI a solo numeros y email a minusculas.
- Si detecta `urgency_level=high` o `needs_human=true`, deriva a bandeja humana prioritaria y responde sin consejo medico.
- No loguea API keys ni envia secrets al frontend.

Ejemplos:
- "Quiero un turno virtual para mi hijo Juan Perez DNI 40111222, el martes a la tarde" -> guarda turno virtual, otra persona, nombre/DNI y preferencia; pregunta solo si es primera vez.
- "Necesito receta para mi medicacion habitual" -> guarda tipo receta y detalle; deja la solicitud pendiente sin pedir el detalle otra vez.

Comando de validacion:
```powershell
.\.venv\Scripts\python -m pytest -q
```

### 8.3) Etapa 2.5 IA - Visibilidad y auditoria
Archivos:
- `app/services/ai_conversation_summary_service.py`
- `app/templates/tenant/conversation_states.html`
- `app/templates/tenant/conversation_state_detail.html`
- vistas tenant/admin de conversaciones.

Objetivo:
- Mostrar en la bandeja operativa lo que el agente entendio del mensaje.
- Ayudar al equipo humano a auditar clasificacion, confianza, datos extraidos y campos faltantes.
- No cambia la logica conversacional ni ejecuta acciones automaticas.

Se muestra en listados:
- Intencion detectada.
- Confianza y nivel (`Alta`, `Media`, `Baja`).
- Fuente (`rules`, `ai`, `fallback`).
- Datos resumidos extraidos.
- Campos faltantes.
- Badges de `Requiere humano` y `Urgencia posible` cuando aplica.
- En listados se enmascaran datos sensibles como DNI.

Se muestra en detalle:
- Seccion `Interpretacion de IA`.
- Intencion, confianza, fuente, urgencia y necesidad de humano.
- Datos extraidos completos necesarios para operacion del consultorio.
- Campos faltantes.
- Correccion humana de intencion y nota sobre interpretacion IA si se cargan.

Persistencia:
- La interpretacion viene de `EstadoConversacion.contexto_json.ai`.
- La revision humana se guarda en:
```json
{
  "ai_review": {
    "human_corrected_intent": "recipe_or_order",
    "review_note": "La IA lo tomo como turno, pero era pedido de receta.",
    "reviewed_by": 1,
    "reviewed_at": "..."
  }
}
```

Seguridad:
- No se muestra `raw_response` en listados ni detalle.
- No se muestran prompts internos ni API keys.
- El detalle admin global respeta tenant por URL; el tenant admin solo ve su tenant.

Que NO hace esta etapa:
- No reserva turnos.
- No consulta Google Calendar.
- No llama Consultorio Movil.
- No crea pagos.
- No cancela ni reprograma turnos reales.

Comando de validacion:
```powershell
.\.venv\Scripts\python -m pytest -q
```

### 8.4) Etapa 3 IA - Tools controladas de disponibilidad
Archivos:
- `app/services/ai_tools/base.py`
- `app/services/ai_tools/appointment_availability_tool.py`
- `app/services/conversation_service.py`
- `app/services/tenant_ai_settings_service.py`

Objetivo:
- Permitir consulta controlada de disponibilidad real desde servicios internos.
- Ofrecer opciones numeradas al paciente y guardar esas opciones en `contexto_json`.
- Pedir seleccion y confirmacion, sin reservar automaticamente.

Activacion por tenant:
```json
{
  "enabled": true,
  "tools_enabled": true,
  "availability_lookup_enabled": true,
  "max_offered_slots": 5,
  "require_confirmation_before_booking": true
}
```

Defaults seguros:
- `tools_enabled=false`
- `availability_lookup_enabled=false`
- `max_offered_slots=5`
- `require_confirmation_before_booking=true`

Tool disponible:
- `get_available_appointment_slots(...)`
- Devuelve slots normalizados con `slot_id` opaco, label, inicio/fin, timezone, provider y metadata segura.
- No devuelve tokens, credenciales ni IDs externos sensibles.

Disponibilidad:
- Virtual: usa `CalendarService` / proveedor Google configurado por tenant y consultorio virtual.
- Presencial: usa Consultorio Movil/Cabildo si el consultorio presencial esta configurado.
- Si falla proveedor o falta configuracion, devuelve error controlado y deriva a revision manual.

Estados conversacionales nuevos:
- `ask_ai_slot_selection`: espera numero de opcion ofrecida.
- `ask_ai_booking_confirmation`: espera confirmacion del paciente.

Estructura en `contexto_json`:
```json
{
  "appointment": {
    "type": "virtual",
    "for": "self",
    "is_first_time": true,
    "preferences": {
      "preferred_day": "martes",
      "preferred_time_range": "tarde"
    },
    "offered_slots": [
      {
        "option": 1,
        "slot_id": "opaque-id",
        "label": "Martes 28/05 a las 18:30",
        "start_at": "2026-05-28T18:30:00-03:00",
        "end_at": "2026-05-28T19:00:00-03:00",
        "provider": "google_calendar",
        "metadata": {}
      }
    ],
    "selected_slot": null,
    "awaiting_slot_selection": true,
    "awaiting_booking_confirmation": false
  }
}
```

Flujo:
1. Paciente pide turno por texto libre o completa el flujo existente.
2. Si faltan datos minimos, se pregunta el dato faltante.
3. Si tools estan habilitadas, se consulta disponibilidad.
4. Se ofrecen opciones numeradas.
5. El paciente selecciona una opcion.
6. El bot pide confirmacion.
7. En esta etapa la confirmacion deja la seleccion como pendiente para revision manual.

Que NO hace esta etapa:
- No reserva turnos reales.
- No crea pagos.
- No cancela ni reprograma turnos.
- No crea confirmaciones finales.
- No modifica eventos externos.
- No inventa horarios.

Seguridad:
- Si la IA detecta urgencia (`urgency_level=high` o `needs_human=true`), no consulta disponibilidad y deriva a humano.
- Logs de tool incluyen tenant, telefono, intent, tool, cantidad de slots, provider y error controlado.
- No se loguean credenciales ni secretos.

Comando de validacion:
```powershell
.\.venv\Scripts\python -m pytest -q
```

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
- Estado actual:
  - listar y reservar slots esta implementado via `CabildoProvider`.
  - cancelar turno externo reservado esta implementado via `cancel_presential_slot`, usando `/office/appointment/list/status` con `status_id=cancelled`.
  - `CalendarService.cancel_slot` libera turnos de Consultorio Movil cuando el turno local tiene `external_status` reservado/confirmado y `external_event_id` real.
  - los borradores locales con slot codificado no llaman cancelacion externa; solo se cancelan localmente.
  - el formulario tenant de consultorios permite probar conexion contra Consultorio Movil desde `/t/consultorios/{consultorio_id}/edit`.
    - endpoint: `POST /t/consultorios/{consultorio_id}/test-provider`.
    - usa los valores actuales del formulario sin guardarlos.
    - consulta disponibilidad presencial de los proximos 3 dias y lista hasta 30 slots.
    - no reserva, no cancela y no expone usuario/password en la respuesta.
  - `sync_cabildo_cancel` y `sync_cabildo_update` quedan como placeholders legacy `NotImplemented`.

Cancelacion operativa:
- Desde la interfaz de turnos tenant: `/t/appointments/{turno_id}` o el listado diario.
- Desde admin global: `/admin/appointments/{turno_id}`.
- Desde WhatsApp: el paciente puede pedir cancelar turno, elegir un turno activo futuro y confirmar.
- Si falla la cancelacion externa, no se marca el turno local como cancelado.

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

Observaciones actuales:
- El webhook acepta `payment_id` por query para resolver el pago local.
- Si existe `mp_webhook_secret`, la firma `x-signature` es obligatoria.
- El webhook persiste `external_payment_id` cuando viene `data.id`.

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
- El fallback global de Twilio aplica a outbound y tambien a validacion inbound si el tenant no tiene token propio.

## 16) Scripts operativos
- `scripts/init_db.py`: crea tablas.
- `scripts/upgrade_schema.py`: migracion incremental/idempotente (agrega columnas faltantes).
- `scripts/seed_admin.py`: crea super admin si no existe.
- `scripts/seed_demo.py`: crea tenant/consultorio demo.

Startup:
- En entornos distintos de `test`, `app.main` ejecuta `scripts.upgrade_schema.upgrade(...)` al iniciar antes de crear/sincronizar usuarios/features.
- Esto evita errores por columnas nuevas faltantes, por ejemplo `tenants.ai_settings`, despues de deploys sin migracion manual.
- `scripts/upgrade_schema.py` sigue pudiendo ejecutarse manualmente y es idempotente.

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
.\.venv\Scripts\python -m pytest -q
```

Nota: si los tests quedan en passed y no vuelve prompt, revisar recursos/event loop abiertos o procesos colgados de entorno local.

## 19) Riesgos/observaciones actuales (para IA y equipo)
- `Turno` SI tiene `tenant_id` directo. Mantenerlo sincronizado con `Consultorio.tenant_id` y conservar validaciones cruzadas para aislamiento.
- Conviven `/t/turnos` legacy y `/t/appointments` agenda real; evitar duplicar logica nueva en ambos sin decidir estrategia.
- El bot conversacional actual clasifica y deriva a bandeja; no asumir agenda automatica end-to-end desde WhatsApp.
- Inbound Twilio usa token por tenant y fallback global.
- Consultorio Movil reserva y cancelacion externa estan implementadas para turnos con `external_event_id` real; `sync_cabildo_update` sigue pendiente.
- `ReminderService` usa `reminder_sent_at` unico junto con flags `reminder_24h_sent` y `reminder_2h_sent`; revisar antes de depender de dos recordatorios independientes.
- Mercado Pago webhook exige firma si hay secret y persiste `external_payment_id`.
- APIs REST admin (`/api/admin/*`) usan Basic Auth y algunas operaciones hacen delete fisico; SSR admin usa soft delete. No mezclar supuestos.
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
