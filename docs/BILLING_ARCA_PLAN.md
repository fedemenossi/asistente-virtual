# Facturacion ARCA - Plan tecnico de Etapa 0

Estado: Etapa 4 implementada.  
Fecha: 2026-07-04.  
Alcance: analisis del SaaS actual y de la POC ARCA `D:\Fede\FeApp`, con plan tecnico por etapas. No se implementa todavia el modulo funcional de facturacion.

## 1. Objetivo de la Etapa 0

La Etapa 0 busca entender el sistema existente antes de tocar codigo productivo. El resultado esperado es este documento, que deja definido:

- arquitectura propuesta;
- tablas nuevas;
- rutas nuevas;
- servicios nuevos;
- templates nuevos;
- estrategia de integracion con Consultorio Movil;
- estrategia de integracion con ARCA reutilizando la POC;
- riesgos tecnicos;
- plan de implementacion incremental.

Cambios realizados en Etapa 0:

- se creo `docs/BILLING_ARCA_PLAN.md`.

Cambios no realizados:

- no se agregaron modelos;
- no se agregaron rutas;
- no se agregaron servicios;
- no se agregaron templates;
- no se modificaron migraciones;
- no se agregaron dependencias;
- no se hicieron llamadas reales a ARCA;
- no se modifico logica existente.

## 2. Arquitectura actual del SaaS

El proyecto `D:\Fede\asistente-virtual` es una aplicacion FastAPI SSR multi-tenant para consultorios medicos.

### Modelos

Los modelos SQLAlchemy estan en `app/models`. Los principales para esta integracion son:

- `Tenant`: entidad multi-tenant. Hoy contiene configuraciones JSON por tenant: `payment_settings`, `calendar_settings`, `whatsapp_settings`, `ai_settings`.
- `Paciente`: paciente por tenant. Aporta datos de receptor: nombre, apellido, DNI, email, telefono.
- `Turno`: turno/agendamiento por tenant. Se vincula con paciente, consultorio, proveedor externo y pagos.
- `Payment` y `PaymentEvent`: pagos y eventos de Mercado Pago.
- `Consultorio`: origen funcional del turno, con proveedor `google`, `consultorio_movil` o manual.
- `AuditLog` y `Notification`: auditoria y notificaciones operativas.

El patron actual es agregar modelos en `app/models`, exportarlos en `app/models/__init__.py` y asegurar su creacion en `scripts/upgrade_schema.py`.

### Rutas tenant

Las rutas tenant estan centralizadas en:

- `app/web/tenant/router.py`
- `app/web/tenant/views.py`

El prefijo es `/t`. Las rutas operativas relevantes son:

- `/t/dashboard`
- `/t/pacientes`
- `/t/consultorios`
- `/t/appointments`
- `/t/payments`
- `/t/settings`
- `/t/settings/payments`
- `/t/settings/calendar`

El modulo ARCA deberia seguir este patron y sumar rutas tenant bajo `/t/arca/*` y `/t/settings/arca`.

### Rutas admin

Las rutas admin estan centralizadas en:

- `app/web/admin/router.py`
- `app/web/admin/views.py`

El prefijo es `/admin`. El admin hoy gestiona tenants, usuarios, features, pagos globales, turnos globales y auditoria.

Para ARCA no conviene emitir comprobantes desde admin global en una primera etapa. El admin podria ver estado/configuracion general por tenant mas adelante, pero la operatoria fiscal debe vivir en el tenant para reducir riesgo de aislamiento.

### Templates

Los templates estan en `app/templates`. Las pantallas tenant viven en:

- `app/templates/tenant/*`

Los componentes y layout comunes viven en:

- `app/templates/layout/base.html`
- `app/templates/layout/partials/sidebar.html`
- `app/templates/layout/partials/topbar.html`
- `app/templates/layout/partials/components/*`

El modulo ARCA debe crear templates tenant propios y reutilizar el layout existente.

### Sidebar

El sidebar se renderiza desde:

- `app/templates/layout/partials/sidebar.html`

Los items actuales se condicionan por rol y por features. Para ARCA se deberian agregar items tenant como:

- `Facturacion ARCA` -> `/t/arca/invoices`
- `Configuracion ARCA` -> `/t/settings/arca`

### Servicios

La logica de negocio vive en `app/services`. Ejemplos relevantes:

- `PaymentService`: crea pagos y procesa webhooks.
- `AppointmentService`: crea/confirma/cancela turnos locales y externos.
- `CalendarService`: integra Google Calendar y Consultorio Movil.
- `TenantFeatureService`: sincroniza features por tenant.

ARCA deberia seguir el mismo patron: servicios transaccionales en `app/services` y clientes SOAP en `app/integrations`.

### Integraciones

Las integraciones externas viven en `app/integrations`. Ejemplos:

- `mercadopago_service.py`
- `google_calendar_provider.py`
- `consultorio_movil.py`

ARCA deberia ir en un subpaquete propio:

- `app/integrations/arca/`

Esto evita mezclar SOAP, certificados y normalizacion de respuestas con vistas o servicios de negocio.

### Scripts de migracion

La migracion incremental esta en:

- `scripts/upgrade_schema.py`

El startup ejecuta `upgrade(...)` fuera de entorno `test`. Cualquier tabla o columna nueva de ARCA debe entrar ahi de forma idempotente.

### Tests existentes

Los tests estan en `app/tests`. Cubren:

- auth/RBAC;
- aislamiento tenant;
- pagos y webhooks;
- agenda;
- Google Calendar;
- Consultorio Movil;
- conversaciones;
- migraciones de schema;
- features.

Para ARCA se deberian agregar tests unitarios sin llamadas reales a ARCA, usando fakes/mocks del cliente WSAA/WSFE.

## 3. POC ARCA existente

La POC esta en:

```text
D:\Fede\FeApp
```

Archivos principales:

- `app/config.py`
- `app/wsaa_client.py`
- `app/wsfe_client.py`
- `app/http_transport.py`
- `app/repository.py`
- `app/sync_service.py`
- `app/main.py`
- `tests/*`

### Autenticacion contra WSAA

`app/wsaa_client.py` implementa:

1. construccion del `LoginTicketRequest`;
2. firma CMS del XML;
3. llamada SOAP a WSAA `loginCms`;
4. parseo de `LoginTicketResponse`;
5. cache de `Token` y `Sign` hasta 5 minutos antes del vencimiento.

El request incluye:

- `uniqueId`;
- `generationTime` con margen hacia atras;
- `expirationTime` con ventana de 12 horas;
- `service`, hoy `wsfe`.

### Certificado y clave privada

La POC usa:

- `ARCA_CERT_PATH`;
- `ARCA_KEY_PATH`;
- `ARCA_KEY_PASSPHRASE`.

El certificado se carga como PEM o DER con `cryptography.x509`. La clave privada se carga con `serialization.load_pem_private_key`. Luego se firma con:

- `PKCS7SignatureBuilder`;
- hash `SHA256`;
- encoding DER;
- salida base64.

La clave privada nunca debe enviarse a ARCA. Se usa localmente para firmar el Login Ticket Request.

### Obtencion de token/sign

`WsaaClient.get_ticket()`:

- intenta reutilizar ticket cacheado;
- si falta o vence dentro de 5 minutos, firma un nuevo request;
- invoca `loginCms`;
- extrae `credentials/token`, `credentials/sign` y `header/expirationTime`;
- guarda el ticket en repositorio.

En la POC el repositorio es SQLite y cifra token/sign con Fernet.

### Llamadas a WSFEv1

`app/wsfe_client.py` crea un cliente Zeep contra:

- homologacion: `https://wswhomo.afip.gov.ar/wsfev1/service.asmx?WSDL`;
- produccion: `https://servicios1.afip.gov.ar/wsfev1/service.asmx?WSDL`.

Metodos ya implementados:

- `FEDummy`;
- `FEParamGetPtosVenta`;
- `FEParamGetTiposCbte`;
- `FECompUltimoAutorizado`;
- `FECompConsultar`.

`get_auth()` arma:

```python
{
    "Token": ticket.token,
    "Sign": ticket.sign,
    "Cuit": settings.represented_cuit,
}
```

La POC normaliza respuestas SOAP con `zeep.helpers.serialize_object`, lee `Errors` y `Events`, y evita loguear secretos.

### Solicitud de CAE

La POC no implementa todavia `FECAESolicitar`. Esto esta confirmado por su README y por `app/wsfe_client.py`.

Para emitir CAE habra que agregar un metodo nuevo que envie:

- `Auth`;
- `FeCAEReq.FeCabReq`;
- `FeCAEReq.FeDetReq.FECAEDetRequest`.

Tambien habra que persistir request/respuesta y manejar reconciliacion ante timeout.

### Homologacion y produccion

`app/config.py` separa ambiente con `ARCA_ENV`:

- `homo`;
- `prod`.

Define WSDLs distintos para WSAA y WSFE.

`app/http_transport.py` agrega una compatibilidad TLS solo para produccion WSFEv1:

- host: `https://servicios1.afip.gov.ar/`;
- cipher policy: `DEFAULT@SECLEVEL=1`;
- mantiene validacion de certificado y hostname.

Esa excepcion debe reutilizarse de forma acotada. No debe aplicarse globalmente a todo HTTP.

### Partes a reutilizar

Conviene reutilizar:

- firma CMS del `LoginTicketRequest`;
- parseo de `LoginTicketResponse`;
- margen de cache de 5 minutos;
- seleccion de WSDL por ambiente;
- transporte TLS especifico para WSFE produccion;
- normalizacion de respuestas SOAP;
- lectura de `Errors` y `Events`;
- hints de errores WSFE;
- tests de WSAA y normalizacion.

### Partes a refactorizar

Conviene refactorizar:

- repositorio SQLite -> SQLAlchemy async multi-tenant;
- `Settings.from_env()` global -> settings por tenant;
- almacenamiento de certificados/passphrase -> estrategia segura por tenant;
- comandos CLI -> servicios internos y vistas SSR;
- sync local SQLite -> tabla `arca_invoices` por tenant;
- llamadas Zeep bloqueantes -> wrapper que pueda ejecutarse en thread cuando se invoque desde FastAPI async;
- logs -> mantener mascarado de CUIT/paths cuando corresponda y nunca loguear token/sign/passphrase.

## 4. Arquitectura propuesta para el modulo ARCA

Separacion propuesta:

- `app/integrations/arca`: cliente tecnico WSAA/WSFE.
- `app/services`: logica fiscal y transaccional.
- `app/repositories`: persistencia async.
- `app/models`: tickets, comprobantes y eventos.
- `app/web/tenant`: rutas y vistas tenant.
- `app/templates/tenant`: pantallas SSR.
- `scripts/upgrade_schema.py`: migracion incremental.
- `app/tests`: tests unitarios y de rutas sin llamadas reales.

Principios:

- ARCA es multi-tenant: toda entidad debe tener `tenant_id`.
- La emision no debe depender directamente de Consultorio Movil.
- La fuente local para facturar debe ser `Paciente`, `Turno` y/o `Payment`.
- Toda operacion fiscal debe auditarse.
- No emitir automaticamente en la primera version.
- No reenviar `FECAESolicitar` a ciegas ante timeout.

## 5. Tablas nuevas propuestas

### `arca_auth_tokens`

Cache de credenciales WSAA.

Campos:

- `id`
- `tenant_id`
- `represented_cuit`
- `environment`
- `service`
- `token_encrypted`
- `sign_encrypted`
- `expiration_time`
- `created_at`
- `updated_at`

Unique:

- `tenant_id`, `represented_cuit`, `environment`, `service`

### `billing_invoices`

Comprobantes locales.

Campos:

- `id`
- `tenant_id`
- `patient_id`
- `appointment_id`
- `payment_id`
- `represented_cuit`
- `environment`
- `pto_vta`
- `cbte_tipo`
- `cbte_nro`
- `concepto`
- `doc_tipo`
- `doc_nro`
- `cbte_fch`
- `imp_total`
- `imp_tot_conc`
- `imp_neto`
- `imp_op_ex`
- `imp_trib`
- `imp_iva`
- `mon_id`
- `mon_cotiz`
- `status`
- `cae`
- `cae_fch_vto`
- `request_json`
- `response_json`
- `error_message`
- `created_at`
- `updated_at`
- `authorized_at`

Estados sugeridos:

- `draft`
- `pending_authorization`
- `authorized`
- `rejected`
- `needs_reconciliation`
- `cancelled`

Unique recomendado:

- `tenant_id`, `environment`, `represented_cuit`, `pto_vta`, `cbte_tipo`, `cbte_nro`
- `tenant_id`, `external_consultation_id`

Campos agregados para diagnostico y envio:

- `external_consultation_id`
- `billing_item_id`
- `diagnosis_original_snapshot`
- `diagnosis_final_snapshot`
- `send_email`
- `email_to`
- `email_sent_at`
- `created_by`

### `billing_items`

Items facturables por tenant.

Campos:

- `id`
- `tenant_id`
- `code`
- `name`
- `description`
- `unit_price`
- `tax_rate`
- `iva_id`
- `currency`
- `concepto`
- `active`
- `default_item`
- `created_at`
- `updated_at`

### `billing_invoice_lines`

Lineas emitidas por factura, con diagnostico final visible.

Campos:

- `id`
- `invoice_id`
- `item_code`
- `description`
- `diagnosis_text`
- `quantity`
- `unit_price`
- `subtotal`
- `tax_rate`
- `total`

### `arca_invoice_events`

Eventos de auditoria tecnica por comprobante.

Campos:

- `id`
- `invoice_id`
- `event_type`
- `payload_json`
- `created_at`

Eventos sugeridos:

- `draft_created`
- `authorization_requested`
- `authorization_approved`
- `authorization_rejected`
- `authorization_timeout`
- `reconciliation_requested`
- `reconciliation_matched`
- `reconciliation_mismatch`
- `sync_imported`

### `tenants.arca_settings`

Columna JSON propuesta para configuracion no sensible y referencias a secretos.

Ejemplo:

```json
{
  "enabled": false,
  "environment": "homo",
  "represented_cuit": "",
  "service": "wsfe",
  "cert_path": "",
  "key_path": "",
  "key_passphrase_ref": "",
  "default_pto_vta": 1,
  "default_cbte_tipo": 11,
  "default_concepto": 2,
  "default_mon_id": "PES",
  "default_mon_cotiz": 1
}
```

No guardar token, sign, clave privada ni passphrase en claro.

## 6. Rutas nuevas propuestas

### Tenant

Configuracion:

- `GET /t/settings/arca`
- `POST /t/settings/arca`
- `POST /t/settings/arca/test`

Comprobantes:

- `GET /t/arca/invoices`
- `GET /t/arca/invoices/new`
- `POST /t/arca/invoices/new`
- `GET /t/arca/invoices/{invoice_id}`
- `POST /t/arca/invoices/{invoice_id}/authorize`
- `POST /t/arca/invoices/{invoice_id}/reconcile`
- `POST /t/arca/invoices/{invoice_id}/cancel-draft`
- `POST /t/arca/invoices/sync`

Integracion operativa:

- `POST /t/appointments/{turno_id}/arca-invoice-draft`
- `POST /t/payments/{payment_id}/arca-invoice-draft`

### Admin

Primera version: no agregar emision desde admin.

Posibles rutas futuras de supervision:

- `GET /admin/arca`
- `GET /admin/tenants/{tenant_id}/arca`

Estas rutas deberian ser solo lectura o diagnostico. La emision debe permanecer bajo tenant.

## 7. Servicios nuevos propuestos

### Integracion tecnica

- `app/integrations/arca/config.py`
  - resolver WSDLs y settings por tenant.

- `app/integrations/arca/http_transport.py`
  - reutilizar transporte TLS de FeApp.

- `app/integrations/arca/wsaa_client.py`
  - firmar Login Ticket Request;
  - llamar `loginCms`;
  - parsear token/sign.

- `app/integrations/arca/wsfe_client.py`
  - `dummy`;
  - `get_puntos_venta`;
  - `get_tipos_comprobante`;
  - `get_ultimo_autorizado`;
  - `consultar_comprobante`;
  - futuro `solicitar_cae`.

### Servicios de negocio

- `app/services/arca_settings_service.py`
  - validar y guardar configuracion tenant;
  - probar conexion;
  - resolver paths/referencias de secretos.

- `app/services/arca_ticket_service.py`
  - obtener/cachear ticket WSAA por tenant;
  - cifrar token/sign.

- `app/services/arca_invoice_service.py`
  - crear borradores;
  - validar importes;
  - construir request `FECAESolicitar`;
  - autorizar comprobante;
  - registrar eventos y auditoria.

- `app/services/arca_reconciliation_service.py`
  - reconciliar estados inciertos con `FECompConsultar`;
  - detectar mismatch.

- `app/services/arca_sync_service.py`
  - traer comprobantes ya emitidos;
  - persistencia idempotente.

### Repositories

- `app/repositories/arca_ticket_repository.py`
- `app/repositories/arca_invoice_repository.py`
- `app/repositories/arca_invoice_event_repository.py`

## 8. Templates nuevos propuestos

Tenant:

- `app/templates/tenant/settings_arca.html`
- `app/templates/tenant/arca_invoices_list.html`
- `app/templates/tenant/arca_invoice_form.html`
- `app/templates/tenant/arca_invoice_detail.html`
- `app/templates/tenant/arca_invoice_events.html` o seccion parcial dentro del detalle.

Layout:

- modificar `app/templates/layout/partials/sidebar.html` para agregar navegacion ARCA cuando la feature este habilitada.

Admin futuro:

- `app/templates/admin/arca_dashboard.html`
- `app/templates/admin/tenant_arca_status.html`

No crear templates admin en la primera version funcional salvo que haya requerimiento explicito.

## 9. Estrategia con Consultorio Movil

Consultorio Movil ya participa en agenda presencial, pero no debe ser dependencia directa del modulo fiscal.

Reglas propuestas:

- ARCA factura desde datos locales persistidos, no desde el sistema externo.
- Para turnos presenciales reservados por Consultorio Movil, usar `Turno`, `Paciente`, `Consultorio` y `Payment`.
- Si la cancelacion externa falla, no permitir facturar como si el turno estuviera resuelto.
- Si el turno no tiene pago aprobado, definir regla de negocio antes de crear factura:
  - facturar manualmente;
  - facturar al aprobar pago;
  - facturar al marcar asistencia;
  - o facturar solo desde pago aprobado.
- No mezclar identificadores externos de Consultorio Movil con numeracion fiscal ARCA.
- Evitar doble factura por el mismo `payment_id` o `appointment_id`, salvo notas de credito/debito explicitas.

Integracion recomendada:

1. crear borrador desde turno/pago;
2. prellenar paciente, concepto, descripcion e importe;
3. dejar revision humana antes de solicitar CAE;
4. auditar relacion entre factura, turno y pago.

## 10. Estrategia de integracion con ARCA usando la POC

### Port incremental

1. Copiar estructura conceptual de FeApp a `app/integrations/arca`.
2. Mantener tests de WSAA, parseo y normalizacion.
3. Cambiar settings globales por settings por tenant.
4. Cambiar repositorio SQLite por SQLAlchemy async.
5. Mantener cliente SOAP encapsulado y sin conocimiento de FastAPI/Jinja.
6. Agregar `FECAESolicitar` recien cuando existan modelos locales de comprobantes.

### Operacion segura de emision

Flujo recomendado:

1. validar configuracion ARCA del tenant;
2. validar certificado/clave;
3. obtener ticket WSAA;
4. consultar ultimo autorizado;
5. asignar numero local;
6. persistir request y estado `pending_authorization`;
7. llamar `FECAESolicitar`;
8. guardar respuesta completa;
9. marcar `authorized`, `rejected` o `needs_reconciliation`;
10. ante estado incierto, reconciliar con `FECompConsultar`.

### Factura C simple

Primera emision recomendada:

- `CbteTipo=11`;
- `Concepto=2` o segun servicio medico definido;
- `DocTipo` segun receptor;
- `ImpIVA=0`;
- `ImpTrib=0`;
- `ImpTotConc=0`;
- `ImpOpEx=0`;
- `MonId=PES`;
- `MonCotiz=1`;
- sin array `Iva`;
- sin opcionales salvo que la normativa del emisor lo requiera.

## 11. Riesgos

- Concurrencia de numeracion: mitigar con unique constraint y lock logico por tenant/ambiente/CUIT/punto/tipo.
- Timeout de `FECAESolicitar`: no reintentar sin reconciliar.
- SOAP bloqueante en FastAPI async: ejecutar en thread o aislar en servicio.
- Secretos fiscales: no guardar passphrase/token/sign en claro.
- Certificados por tenant: definir storage y permisos antes de produccion.
- Diferencia homologacion/produccion: no mezclar tickets ni numeracion.
- Errores ARCA con observaciones: distinguir aprobado con observaciones vs rechazo.
- Importes: usar `Decimal`, no `float`, en servicios/modelos fiscales.
- Datos de receptor incompletos: validar DNI/CUIT y condicion IVA antes de emitir.
- Multi-tenant: todo query debe filtrar por `tenant_id`.
- Consultorio Movil: evitar asumir que turno externo equivale a pago/factura.
- Auditoria: registrar toda autorizacion, rechazo, reconciliacion y sync.

## 12. Plan de implementacion por etapas

### Etapa 0 - Analisis y documentacion

Estado: completada con este documento.

Entregable:

- `docs/BILLING_ARCA_PLAN.md`.

### Etapa 1 - Cliente tecnico ARCA sin UI

Estado: implementada parcialmente como base estructural, sin cliente ARCA ni llamadas WSAA/WSFE.

Objetivo:

- crear la base estructural multi-tenant;
- agregar feature flag `billing_arca`;
- agregar modelos, rutas protegidas, templates iniciales y tests de acceso/aislamiento;
- no emitir todavia.

Archivos:

- crear `app/models/arca_access_ticket.py`;
- crear `app/models/arca_invoice.py`;
- crear `app/models/arca_invoice_event.py`;
- crear templates tenant iniciales de facturacion/configuracion;
- modificar `scripts/upgrade_schema.py`;
- modificar `app/models/__init__.py`;
- modificar `app/core/features.py`;
- modificar `app/core/security.py`;
- modificar `app/web/tenant/router.py`;
- modificar `app/web/tenant/views.py`;
- modificar sidebar;
- agregar tests.

Queda para una etapa posterior:

- portar WSAA/WSFE desde FeApp;
- agregar dependencias `cryptography` y `zeep`;
- agregar repositorios/servicios de ticket;
- implementar cliente tecnico ARCA.

### Etapa 2 - Configuracion ARCA por tenant

Estado: implementada como parametrizacion local, sin llamadas a ARCA.

Objetivo:

- guardar `arca_settings`;
- cargar CUIT, punto de venta, ambiente, tipo de comprobante, moneda y datos fiscales;
- cargar certificado, clave privada y passphrase cifrados;
- preparar boton de test sin llamada externa;
- administrar items facturables por tenant;
- no emitir todavia.

Archivos:

- `app/templates/tenant/settings_arca.html`;
- `app/templates/tenant/settings_billing_arca.html`;
- `app/templates/tenant/billing_arca_items_list.html`;
- `app/templates/tenant/billing_arca_item_form.html`;
- `app/services/billing_arca_settings_service.py`;
- `app/models/arca_billable_item.py`;
- `app/web/tenant/router.py`;
- `app/web/tenant/views.py`;
- `app/core/features.py`;
- `app/core/security.py`;
- `scripts/upgrade_schema.py`;
- tests de settings.

Rutas implementadas:

- `GET /t/settings/billing-arca`;
- `POST /t/settings/billing-arca`;
- `POST /t/settings/billing-arca/test`;
- `GET /t/billing-arca/items`;
- `GET /t/billing-arca/items/new`;
- `POST /t/billing-arca/items/new`;
- `GET /t/billing-arca/items/{item_id}/edit`;
- `POST /t/billing-arca/items/{item_id}/edit`;
- `POST /t/billing-arca/items/{item_id}/delete`.

Notas:

- el boton de test solo muestra aviso de etapa pendiente;
- los secretos se cifran con `cryptography.fernet` usando una clave derivada de `SECRET_KEY`;
- no se invoca WSAA/WSFE;
- no se emiten comprobantes.

### Etapa 3 - Borradores locales de comprobantes

Estado: implementada como importacion de consultas atendidas pendientes desde Consultorio Movil, sin crear borradores de factura ni emitir.

Objetivo:

- buscar consultas atendidas en Consultorio Movil;
- importar consultas externas a `billing_external_consultations`;
- excluir consultas ya asociadas a una factura;
- permitir edicion local de diagnostico;
- no llamar `FECAESolicitar`.

Archivos:

- `app/integrations/consultorio_movil.py`: `fetch_attended_consultations`;
- `app/models/billing_external_consultation.py`;
- `app/templates/tenant/billing_pending.html`;
- `app/web/tenant/router.py`;
- `app/web/tenant/views.py`;
- tests con Consultorio Movil mockeado.

Rutas implementadas:

- `GET /t/billing/pending`;
- `POST /t/billing/pending/import`;
- `POST /t/billing/pending/{consultation_id}/diagnosis`.

Notas:

- la importacion usa la configuracion `cabildo` del consultorio;
- se hace upsert por tenant/proveedor/external_id;
- se saltean registros que ya tienen `arca_invoice_id`;
- no se integra ARCA ni se emiten facturas.

### Etapa 4 - Emision CAE homologacion

Estado: implementada parcialmente como integracion tecnica ARCA y prueba de conexion, sin emision.

Objetivo:

- adaptar WSAA desde la POC;
- adaptar WSFEv1 desde la POC;
- cachear token/sign en `arca_access_tickets`;
- soportar homologacion/produccion;
- habilitar boton Probar conexion ARCA;
- manejar errores de configuracion, WSAA y WSFEv1;
- no emitir comprobantes todavia.

Rutas:

- `POST /t/settings/billing-arca/test`.

Archivos:

- `app/integrations/arca/config.py`;
- `app/integrations/arca/http_transport.py`;
- `app/integrations/arca/wsaa_client.py`;
- `app/integrations/arca/wsfe_client.py`;
- `app/repositories/arca_ticket_repository.py`;
- `app/services/arca_service.py`;
- `app/web/tenant/views.py`;
- `requirements.txt`;
- tests mockeando WSAA y WSFEv1.

Flujo implementado:

1. resolver configuracion ARCA del tenant;
2. descifrar certificado, clave privada y passphrase;
3. buscar ticket WSAA cacheado con margen de 5 minutos;
4. si no hay ticket valido, pedir uno nuevo por WSAA y guardarlo cifrado;
5. invocar `FEDummy`;
6. invocar `FEParamGetPtosVenta`;
7. mostrar resultado por flash y auditar la prueba.

Cierre:

- tests con WSAA/WSFE fake;
- sin llamada real en tests;
- sin `FECAESolicitar`;
- sin emision desde grilla.

### Etapa 5 - Emision desde consultas seleccionadas

Estado: implementada como emision individual y por lote desde consultas externas importadas, usando WSFEv1 y manteniendo idempotencia por consulta.

Objetivo implementado:

- previsualizar facturas antes de invocar ARCA;
- emitir una factura individual desde una consulta seleccionada;
- emitir por lote desde la pantalla de preview;
- validar diagnostico obligatorio antes de solicitar CAE;
- incluir el diagnostico en `request_json.metadata`, `FECAEDetRequest.Diagnostico` y descripcion del item;
- guardar CAE, numero de comprobante, vencimiento, request y response ARCA;
- marcar `billing_external_consultations.arca_invoice_id` solo cuando la factura queda autorizada o recuperada;
- bloquear doble facturacion cuando la consulta ya tiene factura asociada;
- ante error de `FECAESolicitar`, consultar `FECompConsultar` para recuperar autorizaciones posiblemente emitidas.

Rutas:

- `POST /t/billing/preview`;
- `POST /t/billing/emit`;
- `POST /t/billing/emit/{consultation_id}`.

Archivos:

- `app/services/arca_service.py`;
- `app/integrations/arca/wsfe_client.py`;
- `app/web/tenant/router.py`;
- `app/web/tenant/views.py`;
- `app/templates/tenant/billing_pending.html`;
- `app/templates/tenant/billing_invoice_preview.html`;
- `app/tests/test_billing_arca_structure.py`.

Flujo:

1. el usuario selecciona consultas pendientes en `/t/billing/pending`;
2. el sistema valida que no esten facturadas y que tengan diagnostico;
3. se renderiza preview con seleccion de item facturable por consulta;
4. al emitir, el servicio obtiene token/sign cacheado via WSAA;
5. consulta ultimo comprobante autorizado con `FECompUltimoAutorizado`;
6. arma `FECAESolicitar` incorporando el diagnostico;
7. guarda factura, eventos y respuesta;
8. si ARCA autoriza, vincula la consulta con la factura;
9. si hay error de WSFE, intenta recuperar con `FECompConsultar`.

Tests:

- emision exitosa con CAE y diagnostico obligatorio en payload;
- rechazo/error ARCA persistido como factura rechazada;
- bloqueo de doble facturacion;
- recuperacion via `FECompConsultar`;
- validacion de diagnostico en preview.

Riesgos pendientes:

- concurrencia: dos emisiones simultaneas de la misma consulta pueden competir antes de confirmar la transaccion;
- reintentos de consultas rechazadas pueden necesitar una politica explicita para reutilizar, reconciliar o descartar intentos previos;
- el contenido exacto que ARCA persiste como descripcion depende de campos admitidos por WSFEv1 y puede requerir representacion propia en PDF/impresion fiscal.

### Etapa 6 - Comprobante visual y envio por mail

Estado: implementada como generacion de HTML/PDF y envio/reenvio por SMTP con log de auditoria operacional.

Objetivo implementado:

- generar comprobante HTML de factura;
- generar PDF descargable/adjunto;
- mostrar siempre el diagnostico en el comprobante;
- bloquear generacion/envio si no hay diagnostico registrado;
- enviar y reenviar factura al paciente por SMTP usando `MessagingService`;
- guardar historial en `billing_email_logs`;
- ampliar pantalla detalle de factura con diagnostico, links HTML/PDF, formulario de envio y logs.

Tablas:

- `billing_email_logs`:
  - `tenant_id`;
  - `invoice_id`;
  - `recipient_email`;
  - `subject`;
  - `status`;
  - `error_message`;
  - `sent_at`;
  - `created_at`.

Rutas:

- `GET /t/billing-arca/{invoice_id}`;
- `GET /t/billing-arca/{invoice_id}/comprobante.html`;
- `GET /t/billing-arca/{invoice_id}/comprobante.pdf`;
- `POST /t/billing-arca/{invoice_id}/send-email`.

Servicios:

- `BillingInvoiceDocumentService`;
- `BillingInvoiceEmailService`;
- `MessagingService.send_email` con SMTP, HTML y adjuntos.

Configuracion SMTP:

- `SMTP_HOST`;
- `SMTP_PORT`;
- `SMTP_USERNAME`;
- `SMTP_PASSWORD`;
- `SMTP_FROM_EMAIL`;
- `SMTP_FROM_NAME`;
- `SMTP_USE_TLS`.

Tests:

- HTML/PDF contienen diagnostico obligatorio;
- PDF generado como `application/pdf`;
- envio por mail adjunta PDF y contiene diagnostico en cuerpo HTML/texto;
- registro exitoso en `billing_email_logs`;
- ruta de reenvio usa email del paciente cuando esta disponible.

Riesgos pendientes:

- el PDF actual es simple y suficiente para comprobante operativo inicial; puede requerir mejora visual o libreria dedicada si se necesita formato fiscal imprimible complejo;
- el email del paciente se obtiene del payload importado o del formulario de reenvio; Consultorio Movil debe seguir entregando email o el operador debe cargarlo manualmente;
- no hay cola/reintentos asincronicos todavia, el envio SMTP ocurre dentro del request.

### Etapa 7 - Reconciliacion y sincronizacion

Objetivo:

- resolver timeouts;
- consultar comprobantes emitidos;
- sync idempotente.

Rutas:

- `POST /t/arca/invoices/{invoice_id}/reconcile`;
- `POST /t/arca/invoices/sync`.

### Etapa 8 - Integracion operativa con turnos y pagos

Objetivo:

- crear borradores desde turno/pago;
- evitar doble factura;
- auditar relacion operacional.

Rutas:

- `POST /t/appointments/{turno_id}/arca-invoice-draft`;
- `POST /t/payments/{payment_id}/arca-invoice-draft`.

### Etapa 9 - Produccion controlada

Objetivo:

- habilitar produccion por tenant;
- checklist operativo;
- permisos y auditoria reforzados;
- monitoreo de errores.

Precondiciones:

- reconciliacion implementada;
- manejo de concurrencia implementado;
- storage seguro de secretos definido;
- prueba homologacion completada.
