README Turnos

Objetivo
- La base propia pasa a ser la fuente operativa de los turnos.
- Google Calendar y Consultorio Movil quedan como proveedores externos sincronizados.

Modelo operativo
- Tabla principal: `turnos`
- Cada turno queda asociado a:
  - `tenant_id`
  - `paciente_id`
  - `consultorio_id`
- El turno local guarda:
  - tipo
  - provider
  - external_id
  - external_status
  - start_at / end_at / timezone
  - status
  - notes
  - reminder_24h_sent / reminder_2h_sent
  - cancelled_at / cancellation_reason
  - created_at / updated_at

Compatibilidad
- Se conservan campos legacy:
  - `external_calendar_provider`
  - `external_calendar_id`
  - `external_event_id`
  - `reminder_sent_at`
- La capa de servicios mantiene sincronizados los campos nuevos con los legacy para evitar romper vistas o flujos existentes.

Servicios principales
- `AppointmentService.create_local_turno(...)`
  - crea el turno local en DB
- `AppointmentService.update_local_turno(...)`
  - actualiza atributos operativos del turno
- `AppointmentService.list_turnos_by_tenant_and_date(...)`
  - lista turnos locales por tenant y fecha
- `AppointmentService.get_daily_agenda(...)`
  - devuelve la agenda del dia del tenant

Vistas tenant
- `/t/dashboard`
  - muestra KPIs operativos, turnos de hoy, proximos turnos y tareas del dia
- `/t/appointments`
  - agenda diaria con filtro por fecha y resumen operativo
- `/t/appointments/{turno_id}`
  - detalle completo del turno local y sus datos de sincronizacion

Arquitectura objetivo
- Paciente -> Bot -> DB propia -> proveedor externo
- La DB local registra el turno antes de confirmar sincronizacion externa.

Estado actual real
- Reserva automatizada ya soportada:
  - flujo local de turnos via `AppointmentService`
  - confirmacion post-pago via `PaymentService.handle_mp_webhook(...)` -> `AppointmentService.confirm_after_payment(...)`
- sincronizacion con Google o Consultorio Movil segun `consultorio.proveedor_turnos`
- `turnos.tenant_id` existe como campo directo y debe mantenerse alineado con `consultorio.tenant_id`
- Panel tenant ya soporta:
  - dashboard operativo del dia (`/t/dashboard`)
  - agenda por fecha (`/t/appointments`)
  - detalle de turno (`/t/appointments/{turno_id}`)
- Bot conversacional:
  - ofrece disponibilidades elegibles de Google Calendar, DrApp y Consultorio Movil
  - para Google Calendar consulta desde el momento actual hasta el horizonte en dias configurado por Tenant en `Configuracion > Calendario` (21 dias por defecto, entre 1 y 90)
  - si las fuentes responden correctamente pero no devuelven horarios, informa que no hay turnos libres sin generar una derivacion manual; solo deriva cuando la consulta de agenda falla
  - para el propio Paciente, confirma automaticamente los turnos sin requisito de pago
  - las primeras consultas particulares retienen el horario y esperan el pago antes de confirmar
  - las solicitudes para otra persona y las agendas no automatizables siguen derivandose a la bandeja operativa
  - los reintentos de Twilio se deduplican por `tenant_id + MessageSid` y reutilizan la respuesta original

Matriz de sincronizacion
- `provider=google`
  - pensado para turnos virtuales o slots gestionados en Google Calendar
  - reserva/cancelacion via `CalendarService` + `GoogleCalendarProvider`
- `provider=consultorio_movil`
  - pensado para turnos presenciales en Consultorio Movil
  - listar/reservar via `CalendarService` + `CabildoProvider`
  - cancelacion/update externo todavia pendiente; la cancelacion externa se bloquea con error explicito para no marcar localmente un turno que sigue activo afuera
- `provider=manual`
  - reservado para carga/seguimiento local sin proveedor externo

Riesgos actuales
- Conviven vistas legacy `/t/turnos` y agenda real `/t/appointments`.
- La reserva automatica depende de una Fuente de agenda configurada y elegible; los casos no automatizables se derivan a la bandeja operativa.
- `ReminderService` tiene flags `reminder_24h_sent` / `reminder_2h_sent`, pero tambien usa `reminder_sent_at` unico; revisar antes de activar dos ventanas de recordatorio en produccion.

Pruebas manuales
1. Ejecutar migracion:
   - `python scripts/upgrade_schema.py`
2. Levantar app:
   - `.\.venv\Scripts\python -m uvicorn app.main:app --reload`
3. Crear tenant, consultorio y paciente.
4. Configurar el consultorio:
   - `proveedor_turnos=google` o `consultorio_movil`
   - settings/calendario o configuracion de Consultorio Movil segun corresponda
5. Generar un turno por el flujo actual soportado:
   - draft local + confirmacion por pago/webhook, o
   - flujo operativo/manual que ya use `AppointmentService`
6. Verificar en DB que `turnos` guarda:
   - `tenant_id`
   - `paciente_id`
   - `consultorio_id`
   - `provider`
   - `external_id`
   - `external_status`
   - `status`
   - `notes`
7. Verificar aislamiento tenant:
   - loguear como tenant A y revisar `/t/appointments`
   - loguear como tenant B y confirmar que no aparecen turnos de A
8. Verificar agenda diaria:
   - abrir `/t/appointments?date=YYYY-MM-DD`
9. Verificar dashboard operativo:
   - abrir `/t/dashboard`
   - revisar turnos de hoy, proximos turnos y tareas del dia
