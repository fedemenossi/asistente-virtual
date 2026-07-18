# Asistente Virtual Médico

SaaS para consultorios médicos que concentra la atención por WhatsApp y la operación clínica y administrativa de cada organización. Cada tenant opera sus datos, configuraciones y procesos de manera aislada.

## Organización y atención

**Tenant**:
La organización o profesional que contrata y opera el sistema. Es el límite de aislamiento para la información, la configuración y la operación.
_Evitar_: cliente, cuenta, consultorio

**Consultorio**:
Unidad de atención de un Tenant en la que se realizan turnos presenciales o virtuales. Puede apoyarse en un proveedor externo de agenda.
_Evitar_: tenant, sede (salvo que se incorpore como concepto propio)

**Paciente**:
Persona que recibe atención dentro de un Tenant. Sus datos de contacto, identificación y condición frente al IVA se gestionan de forma aislada por Tenant y esta última se preselecciona al facturarle manualmente.
_Evitar_: usuario, cliente

**Turno**:
Reserva operativa de atención para un Paciente en un Consultorio, con fecha, duración y estado propios. Puede estar sincronizado con un proveedor externo, pero el registro local conserva su trazabilidad operativa.
_Evitar_: evento de calendario, slot

**Disponibilidad**:
Opción de horario que un proveedor de agenda informa como reservable. No es un Turno hasta que se confirma la reserva.
_Evitar_: turno, reserva

## Conversaciones

**Conversación**:
Interacción por WhatsApp entre un Paciente y un Tenant, identificada por el teléfono dentro de ese Tenant. Mantiene su estado mientras está activa y se archiva al resolverse.
_Evitar_: chat, ticket

**Solicitud operativa**:
Pedido surgido en una Conversación que requiere gestión del consultorio, como un turno, una receta u otra consulta. Puede permanecer pendiente de revisión humana.
_Evitar_: conversación pendiente, ticket (salvo que se adopte formalmente)

**Bandeja operativa**:
Vista de Solicitudes operativas pendientes y resueltas de un Tenant, desde la que el equipo las clasifica, revisa y cierra.
_Evitar_: bandeja global, inbox

**Derivación humana**:
Decisión de no continuar una Solicitud mediante automatización y dejarla para intervención del equipo del consultorio.
_Evitar_: resolución automática

## Cobros y facturación

**Pago**:
Registro del cobro asociado a un Paciente y, cuando aplica, a un Turno. Su estado refleja el resultado informado por el proveedor de pagos.
_Evitar_: factura, comprobante

**Consulta atendida**:
Atención efectivamente realizada que ingresa como candidata a facturación. Puede provenir de un sistema externo y requiere evaluación antes de emitirse un comprobante.
_Evitar_: turno, factura

**Comprobante ARCA**:
Documento fiscal electrónico emitido por un Tenant y autorizado mediante CAE. Puede originarse en una Consulta atendida o en una Factura manual, y conserva su autorización, representación y trazabilidad.
_Evitar_: pago, recibo, factura borrador

**Factura manual**:
Comprobante ARCA de prestación de servicios cuyo emisor carga directamente los datos fiscales y selecciona un único Ítem facturable del catálogo del Tenant, cuyo importe puede ajustar para esa emisión, sin partir de una Consulta atendida, un Turno ni un Pago. Se crea al confirmar una Previsualización temporal con fecha de emisión del día actual y se solicita directamente en producción dentro del mismo módulo que los demás Comprobantes ARCA.
_Evitar_: comprobante libre, cobro manual

**Factura C**:
Tipo de Comprobante ARCA que será el único alcance de emisión en la primera versión de facturación manual, en pesos argentinos. Las facturas A/B y las notas de crédito o débito quedan fuera de ese alcance inicial, por lo que un comprobante autorizado no se modifica ni anula desde el sistema.
_Evitar_: factura genérica

**Receptor fiscal**:
Persona u organización a nombre de la que se emite un Comprobante ARCA; puede seleccionarse desde Pacientes o desde Contactos fiscales, que son fuentes independientes. En la primera versión es el único sujeto asociado a una Factura manual y no se vincula además a un Paciente diferente.
_Evitar_: paciente, cliente

**Condición fiscal del receptor**:
Situación del Receptor fiscal frente al IVA que se elige al registrarlo y se informa en su Comprobante ARCA. Se guarda para Contactos fiscales y Pacientes, y no se presupone una condición única para todos los receptores.
_Evitar_: consumidor final por defecto

**Contacto fiscal**:
Receptor fiscal guardado en el directorio de un Tenant para reutilizar sus datos en futuras facturas manuales. Puede ser una persona identificada por DNI o una organización identificada por CUIT, no presupone que sea un Paciente y se crea al autorizar su primera Factura manual; su identidad fiscal no se duplica dentro del Tenant, su email guardado se usa para la Entrega automática y puede darse de baja sin alterar comprobantes históricos.
_Evitar_: paciente, contacto global

**Receptor provisorio**:
Datos fiscales de un receptor nuevo capturados durante una Previsualización temporal. Se convierten en un Contacto fiscal solo si ARCA autoriza el comprobante.
_Evitar_: contacto fiscal, paciente

**Ítem facturable**:
Concepto del catálogo de un Tenant que puede seleccionarse para emitir un Comprobante ARCA. Define el servicio o producto facturado y sus datos comerciales vigentes.
_Evitar_: descripción libre, renglón de factura

**Importe facturado**:
Importe final aplicado a un Ítem facturable en un Comprobante ARCA. Puede diferir del importe vigente en el catálogo y se conserva como parte de la instantánea del comprobante.
_Evitar_: precio actual del ítem

**Previsualización temporal**:
Revisión no persistida de los datos de una Factura manual antes de confirmar la emisión. No crea un Comprobante ARCA ni puede retomarse después de abandonar el flujo.
_Evitar_: borrador guardado, factura emitida

**Intento de emisión**:
Solicitud de autorización de una Factura manual enviada a ARCA, con resultado autorizado, rechazado o incierto. Un rechazo se conserva como histórico y habilita iniciar una nueva emisión corregida.
_Evitar_: reenvío del mismo comprobante

**Emisión segura**:
Autorización de comprobantes que evita asignar una numeración duplicada para un mismo Tenant, emisor, punto de venta y tipo de comprobante. Incluye la Reconciliación de resultados inciertos antes de crear otro intento.
_Evitar_: emisión concurrente sin control

**Reconciliación**:
Verificación automática del estado de un Intento de emisión incierto directamente con ARCA antes de habilitar una nueva emisión. Evita duplicar comprobantes si la autorización se produjo pese a una falla de comunicación y se informa en el detalle habitual del comprobante.
_Evitar_: reintento inmediato

**Período de prestación**:
Rango con la fecha inicial y final del servicio facturado en una Factura manual. Forma parte de los datos conservados al emitir el Comprobante ARCA.
_Evitar_: fecha única de atención

**Vencimiento de pago**:
Fecha informada en un Comprobante ARCA como vencimiento del pago. En la primera versión de facturación manual coincide con la fecha de emisión.
_Evitar_: fecha de prestación

**Condición de venta**:
Modalidad comercial informada en un Comprobante ARCA, como contado, transferencia o cuenta corriente. Se selecciona para cada Factura manual y se conserva en su instantánea.
_Evitar_: estado de pago

**Entrega por email**:
Envío opcional por email del PDF de un Comprobante ARCA al Receptor fiscal después de una autorización exitosa. Está seleccionado por defecto en una Factura manual, no forma parte de la autorización fiscal ni modifica su resultado, y puede realizarse o reenviarse sin volver a emitir.
_Evitar_: emisión, reintento de CAE

**Reenvío de factura**:
Nueva entrega por email del PDF de un Comprobante ARCA autorizado desde su detalle. Está disponible aunque el envío inicial no se haya solicitado o haya fallado.
_Evitar_: nueva emisión, reintento de CAE

**Capacidad de facturación**:
Habilitación de un Tenant para operar el módulo de facturación ARCA, incluida la facturación manual. La opera su Tenant Admin y exige una configuración productiva válida para emitir, sin una prueba externa previa por cada factura.
_Evitar_: permiso global, acceso de superadministrador

**Instantánea fiscal**:
Copia inmutable de los datos del Receptor fiscal que se incorporan al emitir un Comprobante ARCA. Conserva la información histórica aunque el Contacto fiscal se edite después.
_Evitar_: datos actuales del contacto

**Diagnóstico facturable**:
Dato clínico seleccionado para acompañar una Consulta atendida o un Comprobante ARCA cuando la operación de facturación lo requiere. No forma parte de una Factura manual.
_Evitar_: nota interna
