# Facturacion ARCA

Modulo tenant para facturar consultas atendidas en Consultorio Movil mediante WSAA/WSFEv1 de ARCA.

## Configuracion por tenant

Ruta principal: `/t/settings/billing`.

Datos requeridos:

- ambiente `homo` o `prod`;
- CUIT emisor;
- punto de venta;
- tipo de comprobante;
- concepto;
- moneda;
- condicion fiscal emisor y condicion IVA receptor por defecto;
- certificado publico y clave privada PEM cifrados en base de datos;
- parametrizacion de email de factura.

El diagnostico es obligatorio y visible siempre. No se expone como opcion desactivable.

## Items facturables

Rutas:

- `/t/settings/billing/items`;
- `/t/billing-arca/items/new`;
- `/t/billing-arca/items/{item_id}/edit`.

Cada item incluye codigo, nombre, descripcion, precio, tasa, IVA ID, moneda, concepto, activo y marca de item por defecto.

## Consultorio Movil

Ruta: `/t/billing/pending`.

La importacion usa `consultorio.configuracion_externa.cabildo` con usuario, password y staff ID.

Filtros disponibles:

- fecha desde/hasta;
- consultorio;
- texto libre;
- DNI;
- obra social;
- staff/profesional;
- estado.

La grilla muestra paciente, DNI, email, obra social, practica y diagnostico editable.

## Emision

Flujo:

1. importar consultas atendidas;
2. editar diagnostico si corresponde;
3. seleccionar consultas;
4. previsualizar;
5. elegir item facturable;
6. emitir individual o por lote;
7. solicitar CAE a ARCA;
8. guardar factura, linea, snapshots de diagnostico y respuesta;
9. marcar consulta como facturada;
10. generar HTML/PDF;
11. enviar o reenviar por mail.

## Diagnostico obligatorio

Se guarda en:

- `billing_external_consultations.diagnosis_original`;
- `billing_external_consultations.diagnosis`;
- `billing_invoices.diagnosis_original_snapshot`;
- `billing_invoices.diagnosis_final_snapshot`;
- `billing_invoice_lines.diagnosis_text`.

El comprobante HTML/PDF muestra el diagnostico en una seccion visible. El email tambien lo incluye en el cuerpo.

## ARCA

WSAA:

- genera TRA para `wsfe`;
- firma CMS con certificado y clave privada;
- cachea token/sign cifrados en `arca_auth_tokens`.

WSFEv1:

- `FEDummy`;
- `FEParamGetPtosVenta`;
- `FEParamGetTiposCbte`;
- `FECompUltimoAutorizado`;
- `FECAESolicitar`;
- `FECompConsultar`.

Ante error de `FECAESolicitar`, el servicio intenta recuperar con `FECompConsultar`.

## Seguridad

- rutas protegidas por feature flag `billing_arca`;
- consultas filtradas por `tenant_id`;
- CSRF en formularios;
- secretos cifrados;
- token/sign cifrados;
- no se muestran certificados o claves en templates;
- doble facturacion bloqueada por logica y constraint por `tenant_id` + `external_consultation_id`.

## Checklist de pruebas

- configurar ARCA en homologacion;
- probar conexion;
- crear item facturable;
- importar consultas atendidas;
- filtrar por DNI y obra social;
- editar diagnostico;
- previsualizar;
- emitir;
- verificar CAE y numero;
- verificar que la consulta no vuelve a pendientes;
- abrir HTML/PDF y confirmar diagnostico visible;
- enviar/re enviar email;
- confirmar registro en `billing_email_logs`.
