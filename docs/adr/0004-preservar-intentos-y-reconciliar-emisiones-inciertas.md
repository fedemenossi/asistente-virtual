# Preservar intentos rechazados y reconciliar emisiones inciertas

Un rechazo de ARCA se conservará como un intento inmutable y el operador iniciará una nueva Factura manual corregida para volver a emitir. Ante timeouts o errores de comunicación con resultado incierto, el sistema consultará primero a ARCA antes de habilitar otro intento, para evitar duplicar un comprobante que pudo haber recibido CAE.
