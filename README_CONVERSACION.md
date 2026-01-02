README Conversacion WhatsApp (referencia consultorio-virtual)

Objetivo
Documentar el flujo conversacional ya implementado en D:\Fede\consultorio-virtual
y dejar una guia clara para replicarlo en este proyecto FastAPI.

Fuente analizada
- D:\Fede\consultorio-virtual\whatsapp_assistant\bot.py
- D:\Fede\consultorio-virtual\whatsapp_assistant\app.py

Resumen arquitectonico
1) Webhook recibe el mensaje, resuelve tenant y delega en el bot.
2) El bot normaliza el chat_id, busca paciente y estado conversacional.
3) Se calcula la respuesta segun el estado y se persiste el nuevo estado.
4) Se envia la respuesta por WhatsApp (cliente UltraMsg).

Resolucion de tenant (app.py)
- Se extrae instance_id y owner_number del payload.
- Se busca Tenant por:
  - whatsapp_phone_number_id == instance_id, o
  - whatsapp_business_number == owner_number
- Si no encuentra, usa tenant default si esta activo.

Identificadores clave
- chat_id: whatsapp id completo, ej "54911xxxx@c.us".
- phone_number: chat_id sin el sufijo "@c.us".
- Patient.whatsapp_id = chat_id completo.
- ConversationState.whatsapp_id = chat_id completo.

Estados conversacionales (bot.py)
- collect_full_name
- collect_email
- main_menu
- first_time_check
- virtual_selection
- presential_selection
- presential_dni
- other_detail
- human_reason

Comandos de salida (reinicio)
- salir, cancelar, exit, reiniciar, menu
Accion:
- Si hay paciente, vuelve a main_menu.
- Si no hay paciente, vuelve a collect_full_name.

Timeout de inactividad
- 30 minutos (INACTIVITY_TIMEOUT_MINUTES = 30)
- Si el estado expira, se elimina y se recrea desde cero.

Flujo para paciente NO registrado
1) Estado inicial: collect_full_name
2) Se pide nombre y apellido en un solo mensaje.
3) Si no es valido, se reintenta el mismo paso.
4) Paso collect_email: valida email con regla simple.
5) Crea Patient con:
   - whatsapp_id = chat_id
   - phone_number = phone_number normalizado
   - first_name, last_name, email
6) Pasa a main_menu y muestra opciones.

Flujo para paciente registrado
1) Si el paciente no tiene email, se fuerza collect_email.
2) Si tiene email, entra a main_menu.

Menu principal (main_menu)
Detecta opcion con mapping:
- "a", "presencial", "turno presencial" -> turno_presencial
- "b", "virtual", "turno virtual" -> turno_virtual
- "c", "otra", "consulta" -> otra
- "d", "humano", "asistente" -> humano

Si turno_presencial o turno_virtual:
- Pasa a first_time_check (pregunta si es primera consulta).
- Respuesta SI/NO define etiquetas o flujo de agenda.

Si "otra":
- Pasa a other_detail, solicita detalle en un solo mensaje,
  registra la consulta y vuelve a main_menu.

Si "humano":
- Pasa a human_reason, solicita motivo, registra y vuelve a main_menu.

Persistencia del estado
- ConversationState.step = estado actual
- ConversationState.data = JSON con contexto (intent, nombres, etc)
- updated_at = timestamp UTC

Integraciones usadas en el flujo original
- Google Calendar: lista slots y reserva eventos.
- Cabildo: agenda presencial (scraper + booking).
- Push/email: se dispara al derivar a humano o confirmar turno.

Guia para replicarlo en este proyecto (FastAPI)

1) Webhook
- Entrada unica: /webhook/whatsapp
- Resolver tenant por numero destino (To/recipient)
- Normalizar chat_id (From) y phone_number
- Delegar a ConversationService.process_message()

2) Conversacion y estado
- Tabla EstadosConversacion:
  - telefono, tenant_id, estado_actual, contexto_json, updated_at
- Crear helper de expiracion:
  - si updated_at > 30 minutos -> resetear estado

3) Flujo minimo (equivalente base)
- Si paciente no existe y no hay estado:
  - pedir nombre
  - pedir apellido
  - pedir dni
  - pedir email
  - crear paciente
  - mostrar menu
- Si paciente existe:
  - ir a menu

4) Menu
- Opciones:
  - turno presencial
  - turno virtual
  - otras consultas
  - hablar con humano
- Guardar intencion en contexto_json si se necesitan pasos extra

5) Registro de humano
- Crear Notification
- Crear AuditLog (si aplica)

6) Puntos a mapear en este repo
- Conversacion: app/services/conversation_service.py
- Repos: app/repositories/paciente_repository.py
- Estado: app/repositories/conversacion_repository.py
- Webhook: app/api/routes/webhook.py (o router equivalente)

Checklist para implementar
- [ ] Normalizar phone_number (sin @c.us)
- [ ] Upsert de estado por (tenant_id, telefono)
- [ ] Expiracion de estado (30 min)
- [ ] Comandos de salida (salir/cancelar/menu)
- [ ] Registro de paciente nuevo
- [ ] Menu principal con opciones
- [ ] Notificaciones para humano

Notas
- En consultorio-virtual el estado se guarda en MySQL con insert IGNORE.
- El flujo usa un solo webhook y resuelve tenant por numero destino.
- El bot siempre responde texto plano por WhatsApp.
