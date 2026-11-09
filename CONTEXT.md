# Asistente Virtual Médico

SaaS para profesionales médicos que concentra la atención de Pacientes por WhatsApp y administra la operación clínica y administrativa de cada Tenant de manera aislada.

## Organización y atención

**Tenant**:
El profesional que contrata y opera el sistema. Es el límite de aislamiento para su información, configuración y operación; dos profesionales distintos siempre pertenecen a Tenants separados, aunque trabajen en una misma institución.
_Evitar_: clínica, organización, cliente, cuenta, consultorio

**Operador**:
Persona que gestiona un Tenant en nombre del profesional. En la primera versión, el profesional y su secretaria comparten una única identidad operativa y el sistema no distingue cuál de ellos realiza cada acción.
_Evitar_: paciente, profesional autenticado, rol de secretaria

**Consultorio**:
Unidad de atención de un Tenant en la que el profesional realiza turnos presenciales o virtuales. Un profesional puede tener varios Consultorios y cada Consultorio mantiene su propia agenda, que puede apoyarse en un proveedor externo.
_Evitar_: tenant, sede (salvo que se incorpore como concepto propio)

**Agenda del Consultorio**:
Conjunto de días, horarios, Disponibilidades y Turnos propios de un Consultorio. Dos Consultorios del mismo Tenant mantienen agendas independientes, aunque compartan modalidad de atención.
_Evitar_: agenda del tenant, calendario global, consultorio

**Franja de atención**:
Período semanal recurrente de un Consultorio, definido por día, inicio, fin, duración del turno e intervalo. Puede haber varias franjas por día; en agendas Google se usan para generar Disponibilidades y para limitar cuáles pueden ofrecerse.
_Evitar_: disponibilidad, turno, horario excepcional

**Modalidad de atención**:
Forma en la que se realiza una atención, presencial o virtual. La modalidad solicitada determina qué Consultorios y Agendas del Consultorio pueden evaluarse antes de aplicar las Reglas de elegibilidad.
_Evitar_: consultorio, fuente de agenda, tipo de calendario

**Fuente de agenda**:
Origen autorizado desde el que se obtienen y reservan las Disponibilidades de una Agenda del Consultorio. Cada Consultorio define su propia Fuente de agenda y esa fuente conserva la autoridad sobre sus horarios, ya sea DrApp o Google Calendar.
_Evitar_: disponibilidad, turno, agenda global

**Agenda automatizable**:
Agenda del Consultorio cuya Fuente de agenda permite al sistema consultar y reservar Disponibilidades de forma segura. El Asistente de WhatsApp solo puede ofrecer horarios de Agendas automatizables.
_Evitar_: agenda consultable, agenda visible, integración de solo lectura

**Generación automática de disponibilidades**:
Creación masiva de Disponibilidades en Google Calendar, iniciada por el Operador desde la interfaz y calculada a partir de las Franjas de atención configuradas. Abastece las agendas basadas en Google Calendar sin reemplazar las Disponibilidades propias de DrApp.
_Evitar_: reserva automática, turno confirmado, carga manual de turnos

**Paciente**:
Persona que recibe atención dentro de un Tenant, identificada de manera única por su DNI dentro de ese Tenant. Puede vincularse a uno o más Contactos de WhatsApp, y cada Contacto puede representar a varios Pacientes.
_Evitar_: usuario, cliente

**Paciente asociado**:
Paciente previamente vinculado a un Contacto de WhatsApp. Forma parte de la nómina que el Solicitante puede reutilizar para identificar a quién corresponde una nueva atención, sin crear un Paciente duplicado.
_Evitar_: contacto, solicitante, paciente nuevo

**Identificación del Paciente**:
Paso obligatorio de una Conversación que determina a qué Paciente corresponde la gestión, mediante la selección de un Paciente asociado o el Alta de uno nuevo. Debe completarse antes de iniciar cualquier Solicitud operativa.
_Evitar_: identificación por teléfono solamente, solicitante asumido, paciente implícito

**Perfil mínimo del Paciente**:
Conjunto de datos obligatorios para dar de alta a un Paciente: nombre, apellido, DNI, email y Cobertura declarada del Paciente. La entidad y el número de afiliado son obligatorios cuando declara obra social o prepaga. Si el Paciente ya existe y se identifica por DNI, sus datos registrados se reutilizan sin volver a solicitarlos.
_Evitar_: datos del solicitante, contexto transitorio de la conversación

**Cobertura declarada del Paciente**:
Obra social, prepaga o condición particular que el Paciente informa durante el Alta y que queda guardada en su perfil junto con el número de afiliado. Sirve como dato clínico y administrativo para recetas, facturación o reintegros, pero no selecciona automáticamente la Cobertura del Turno.
_Evitar_: cobertura del turno, regla de elegibilidad, medio de pago

**Cobertura del Turno**:
Condición elegida expresamente para una Solicitud de turno. El Asistente ofrece Particular y las obras sociales o prepagas habilitadas en los Consultorios compatibles; las prepagas se muestran por su nombre parametrizado. Esta elección participa en las Reglas de elegibilidad y el Requisito de pago sin modificar la Cobertura declarada del Paciente.
_Evitar_: cobertura del perfil, dato de facturación, selección automática por prepaga declarada

**Datos de cobertura para facturación**:
Prepaga u obra social y número de afiliado que se solicitan para emitir una factura o facilitar un reintegro, incluso cuando la Cobertura del Turno sea particular. Ante una atención particular, el Asistente pregunta si el Paciente necesita factura para reintegro y solo solicita estos datos cuando la respuesta es afirmativa. Son datos administrativos y no modifican las Reglas de elegibilidad ni el Requisito de pago de la Solicitud de turno.
_Evitar_: cobertura de atención, cambio de consultorio, exención del pago previo

**Alta de Paciente**:
Creación de un Paciente nuevo dentro de un Tenant después de comprobar que su DNI no está registrado y completar su Perfil mínimo. No corresponde realizar un Alta cuando el DNI ya identifica a un Paciente existente.
_Evitar_: vinculación de contacto, actualización de paciente, registro duplicado

**Primera consulta**:
Condición de una Solicitud de turno que indica que el Paciente todavía no fue atendido por el profesional del Tenant. No forma parte de la identidad del Paciente, participa en las Reglas de elegibilidad y solo requiere pago previo cuando la Cobertura del Turno es particular.
_Evitar_: paciente nuevo, dato de identidad permanente

**Requisito de pago**:
Condición de una Primera consulta con Cobertura del Turno particular que exige recibir un Pago aprobado antes de confirmar el Turno. No se aplica a obra social, prepaga ni a Pacientes ya atendidos por el profesional, con independencia de la Cobertura declarada del Paciente.
_Evitar_: precio de consulta, pago opcional, requisito universal

**Tarifa de primera consulta**:
Importe y moneda configurados por el Operador en Pagos para cobrar las Solicitudes de turno con Requisito de pago. Pertenece al Tenant y no debe confundirse con el precio de un Ítem facturable ni con el importe de un Comprobante ARCA.
_Evitar_: importe facturado, tarifa del consultorio, precio de facturación

**Regla de elegibilidad**:
Restricción parametrizable que determina si una Solicitud de turno puede utilizar un Consultorio, considerando al menos la Modalidad de atención, la condición de Primera consulta y la Cobertura del Turno. Cada Consultorio declara las prepagas que admite por nombre; solo las agendas elegibles pueden aportar opciones a la Oferta de disponibilidad.
_Evitar_: preferencia, sugerencia, filtro visual, regla codificada para un profesional

**Contacto de WhatsApp**:
Número desde el que una persona conversa con un Tenant. Puede corresponder al propio Paciente o a un familiar o representante que gestiona la atención de uno o más Pacientes.
_Evitar_: paciente, usuario, identidad del paciente

**Vinculación de contacto**:
Asociación autorizada entre un Contacto de WhatsApp y un Paciente. Si un Contacto nuevo informa el DNI de un Paciente existente, la Vinculación requiere aprobación del Operador antes de exponer datos o permitir operaciones sobre ese Paciente.
_Evitar_: coincidencia por DNI, asociación automática, autenticación por DNI

**Solicitante**:
Persona que utiliza un Contacto de WhatsApp para pedir atención. Puede solicitarla para sí misma o actuar en representación del Paciente que recibirá la atención.
_Evitar_: paciente asumido, usuario del sistema

**Turno**:
Reserva operativa de atención para un Paciente en un Consultorio, con fecha, duración y estado propios. Puede estar sincronizado con un proveedor externo, pero el registro local conserva su trazabilidad operativa.
_Evitar_: evento de calendario, slot

**Turno pendiente de pago**:
Turno de Primera consulta particular todavía no confirmado que conserva la Disponibilidad elegida mediante una Retención de disponibilidad mientras espera el resultado del cobro. Solo puede pasar a confirmado después de recibir un Pago aprobado y completar la reserva en la Fuente de agenda.
_Evitar_: turno confirmado, disponibilidad libre, pago aprobado

**Retención de disponibilidad**:
Bloqueo temporal y exclusivo de una Disponibilidad de Primera consulta particular durante una hora mientras el Turno espera el pago. Impide ofrecer el mismo horario a otra persona y no equivale a una confirmación.
_Evitar_: turno confirmado, reserva definitiva, disponibilidad libre

**Expiración de reserva**:
Finalización de un Turno pendiente de pago cuando no se recibe un Pago aprobado dentro de una hora. Libera la Disponibilidad retenida para que pueda volver a ofrecerse; un pago que se acredite después no recupera ese horario y se convierte en Saldo a favor del mismo Paciente.
_Evitar_: cancelación de turno confirmado, rechazo de pago, confirmación tardía

**Reserva automática**:
Confirmación de una Disponibilidad mediante escritura exitosa en su Fuente de agenda y actualización del Turno correspondiente. Cuando existe un Requisito de pago solo se completa después de recibir un Pago aprobado; en los demás casos no exige pago previo.
_Evitar_: selección de horario, solicitud pendiente, confirmación verbal

**Confirmación de turno**:
Respuesta posterior a una Reserva automática exitosa que informa al Solicitante que el Turno quedó reservado. Cuando existe un Requisito de pago requiere además un Pago aprobado y, cuando corresponde, incluye el enlace del evento de calendario y el acceso a la videollamada.
_Evitar_: selección, solicitud recibida, intento de reserva

**Disponibilidad**:
Opción de horario que un proveedor de agenda informa como reservable. No es un Turno hasta que se confirma la reserva.
_Evitar_: turno, reserva

**Oferta de disponibilidad**:
Conjunto ordenado de Disponibilidades que el Asistente de WhatsApp combina desde DrApp y Google Calendar. Solo incluye Agendas automatizables de Consultorios cuyas Reglas de elegibilidad y Franjas de atención admiten la Solicitud de turno.
_Evitar_: agenda, turno confirmado, disponibilidad de una única fuente

**Oferta parcial**:
Oferta de disponibilidad construida con las Fuentes de agenda que respondieron correctamente cuando otra fuente está temporalmente indisponible. Una falla de DrApp no impide ofrecer horarios elegibles de Google Calendar, ni viceversa.
_Evitar_: oferta completa, ausencia de disponibilidad, error global de agenda

**Derivación por falta de disponibilidad**:
Conversión de una Solicitud de turno en una Solicitud operativa pendiente cuando ninguna Fuente de agenda ofrece horarios elegibles. Conserva al Paciente, sus respuestas y las reglas aplicadas para que el Operador la resuelva desde la Bandeja operativa.
_Evitar_: conversación cerrada, turno rechazado, oferta vacía sin seguimiento

**Ronda de disponibilidad**:
Grupo de hasta cinco opciones de una Oferta de disponibilidad, cada una correspondiente a un día diferente y ordenadas desde la fecha más cercana. Si ninguna resulta adecuada, la siguiente ronda presenta opciones nuevas sin repetir las ya ofrecidas.
_Evitar_: listado completo, repetición de horarios, agenda completa del profesional

**Solicitud de turno**:
Pedido de un Solicitante para reservar una atención a un Paciente identificado durante la Conversación. Mientras no se confirme una Disponibilidad, no constituye un Turno.
_Evitar_: turno, reserva, evento de calendario

**Cancelación de turno**:
Finalización de un Turno confirmado que el Paciente selecciona entre sus propios Turnos cancelables. Libera el slot en Google Calendar y conserva la trazabilidad; si se realiza con al menos 24 horas de anticipación, el Pago aprobado asociado se convierte en Saldo a favor.
_Evitar_: eliminación de turno, expiración de reserva, ausencia, reembolso monetario

**Pago no reembolsable**:
Pago aprobado de un Turno cancelado con menos de 24 horas de anticipación. No se devuelve ni genera Saldo a favor, aunque la Cancelación de turno libere el horario.
_Evitar_: saldo a favor, devolución automática, pago anulado

**Saldo a favor**:
Crédito originado por la cancelación de un Turno pagado con al menos 24 horas de anticipación o por la acreditación tardía de un pago después de una Expiración de reserva. Solo puede aplicarse durante los siete días siguientes a una nueva reserva del mismo Paciente identificado por DNI; no es transferible ni representa una devolución de dinero.
_Evitar_: reembolso, pago pendiente, cupón promocional

**Reprogramación de turno**:
Cambio de un Turno confirmado a una nueva Disponibilidad que cumple el mismo Contexto de elegibilidad. El Asistente lista los Turnos futuros, exige seleccionar y confirmar el Turno de origen, asegura primero la nueva reserva y luego libera la anterior; si existía un Pago aprobado, lo conserva sin volver a cobrar.
_Evitar_: cancelación seguida de alta sin relación, edición de fecha sin reserva, nuevo cobro

**Contexto de elegibilidad**:
Instantánea de las condiciones con las que un Turno obtuvo acceso a una agenda, incluidas la Cobertura del Turno, la condición de Primera consulta y la Modalidad de atención aplicable. Se conserva para que una Reprogramación busque horarios bajo las mismas reglas.
_Evitar_: perfil actual del paciente, configuración actual del consultorio, contexto técnico de conversación

## Conversaciones y gestión

**Asistente de WhatsApp**:
Canal automatizado de un Tenant que recibe mensajes de un Contacto de WhatsApp, conduce la Conversación y ejecuta las operaciones habilitadas. Es el canal real de atención para Pacientes.
_Evitar_: simulador, operador humano, portal del paciente

**Conversación**:
Interacción guiada entre un Contacto de WhatsApp y el Asistente de WhatsApp de un Tenant. Conserva su estado, identifica al Paciente y conduce a una Solicitud operativa o a una operación automática.
_Evitar_: chat sin estado, ticket, simulación

**Simulador de conversación**:
Interfaz interna para probar el mismo flujo y estado del Asistente de WhatsApp sin enviar mensajes reales por WhatsApp. Sus operaciones de agenda modifican el Google Calendar configurado, pero no ejecuta cobros reales ni constituye un canal de atención.
_Evitar_: segundo bot, entorno aislado de agenda, portal del paciente, flujo alternativo

**Pago simulado**:
Aprobación de prueba disponible exclusivamente en el Simulador de conversación para hacer avanzar un Turno pendiente de pago sin generar una transacción monetaria. Debe identificarse y auditarse como simulación y nunca estar disponible en el canal real.
_Evitar_: pago aprobado real, cobro de prueba al proveedor, omisión silenciosa del pago

**Solicitud operativa**:
Pedido surgido en una Conversación y asociado obligatoriamente a un Paciente identificado, como un turno, una receta u otra consulta. Puede permanecer pendiente de revisión humana.
_Evitar_: conversación pendiente, ticket (salvo que se adopte formalmente)

**Categoría operativa**:
Clasificación de una Solicitud operativa según el motivo que debe gestionar el Operador, como turno no automatizable, receta, otra consulta o pedido explícito de atención humana. Determina qué información debe recopilarse antes de la derivación.
_Evitar_: estado de conversación, intención técnica, nota libre

**Solicitud de receta u orden**:
Solicitud operativa asociada obligatoriamente a un Paciente para que el profesional emita, renueve o reescriba una receta u orden médica. Puede incluir una descripción o una fotografía del documento anterior como respaldo y siempre requiere intervención del Operador.
_Evitar_: receta emitida, prescripción automática, adjunto sin solicitud

**Solicitud de factura para reintegro**:
Solicitud operativa que el Asistente registra para un Paciente particular que indicó necesitar factura y para la cual recopila los Datos de cobertura para facturación. Queda pendiente para emisión manual por el Operador; el Asistente no genera ni autoriza comprobantes fiscales.
_Evitar_: factura emitida, facturación automática, requisito para reservar

**Reescritura de receta**:
Solicitud de receta u orden basada en una receta anterior aportada mediante una fotografía. La imagen documenta el pedido, pero no sustituye la revisión ni la nueva emisión por el profesional.
_Evitar_: renovación automática, copia de receta, receta vigente

**Bandeja operativa**:
Vista de Solicitudes operativas pendientes y resueltas de un Tenant, desde la que el equipo las clasifica, revisa y cierra.
_Evitar_: bandeja global, inbox

**Respuesta del Operador**:
Respuesta humana que el Operador envía al abrir el Contacto mediante el enlace de WhatsApp de la Bandeja operativa. El sistema conserva la gestión y el estado de la Solicitud operativa, pero no el contenido de esa respuesta.
_Evitar_: respuesta automática, mensaje del bot, nota interna

**Resolución operativa**:
Cierre manual de una Solicitud operativa en la Bandeja operativa después de que el Operador completó su gestión y, cuando corresponde, envió la Respuesta del Operador.
_Evitar_: respuesta enviada, apertura de WhatsApp, cierre automático

**Derivación humana**:
Transferencia de una Solicitud operativa al Operador cuando requiere intervención humana. Conserva su Categoría operativa y puede producirse por una limitación de automatización, por el tipo de pedido o porque el Solicitante pide hablar con una persona.
_Evitar_: categoría operativa, resolución automática, cierre de conversación

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

**Texto libre del comprobante**:
Observación opcional incorporada por el operador a una Factura manual. No representa necesariamente un diagnóstico clínico, aunque internamente todavía pueda persistirse en campos heredados con ese nombre.
_Evitar_: diagnóstico, diagnóstico facturable
