# PDF de factura electronica ARCA

## Archivos principales

- `app/services/billing_invoice_document_service.py`: genera HTML de respaldo, PDF fiscal, QR ARCA, nombre de archivo y ruta de almacenamiento.
- `requisitos_factura.md`: referencia fiscal usada para validar contenido obligatorio.

El proyecto genera el PDF con ReportLab. No usa una plantilla HTML/CSS para el PDF fiscal, por lo que el layout se dibuja con coordenadas A4 y unidades estables.

## Generacion del PDF

El flujo parte de una factura ya autorizada por WSFE y persistida en `billing_invoices`.

Validaciones implementadas antes de almacenar el PDF:

- la factura debe estar autorizada;
- debe tener CAE;
- debe tener vencimiento de CAE.

El PDF incluye tres copias:

- ORIGINAL;
- DUPLICADO;
- TRIPLICADO.

Cada copia contiene:

- encabezado fiscal con emisor, receptor y datos del comprobante;
- caja central superior con letra y codigo de comprobante;
- punto de venta y numero con mascara fiscal;
- periodo facturado y vencimiento de pago;
- tabla de detalle con columnas estilo ARCA;
- totales;
- leyenda profesional si esta configurada;
- QR ARCA;
- CAE y vencimiento de CAE;
- leyenda de comprobante autorizado;
- leyenda de no responsabilidad de ARCA;
- pagina `Pag. 1/1`;
- marca visible de homologacion cuando corresponde.

## QR ARCA

El QR se genera en `build_arca_qr_url(invoice)`.

El payload usa JSON version 1 codificado en Base64 y contiene:

- `ver`;
- `fecha`;
- `cuit`;
- `ptoVta`;
- `tipoCmp`;
- `nroCmp`;
- `importe`;
- `moneda`;
- `ctz`;
- `tipoDocRec`;
- `nroDocRec`;
- `tipoCodAut`;
- `codAut`.

Para CAE se informa:

- `tipoCodAut = "E"`;
- `codAut = invoice.cae`.

## Formatos visibles

- Fechas: `DD/MM/YYYY`.
- Importes: formato argentino, con coma decimal y punto de miles.
- Factura C: no discrimina IVA.
- Diagnostico: se imprime dentro del detalle solo si existe y la configuracion del tenant permite incluirlo.

## Como probar

1. Emitir una factura ARCA autorizada desde la grilla de consultas a facturar.
2. Abrir `Facturas emitidas`.
3. Entrar al detalle de la factura.
4. Usar `Generar PDF` o `Regenerar PDF`.
5. Descargar el PDF generado.
6. Comparar visualmente contra un comprobante bajado desde ARCA, verificando:
   - caja central de letra arriba;
   - lineas uniformes en la tabla de detalle;
   - QR legible;
   - CAE y vencimiento de CAE al pie;
   - ORIGINAL, DUPLICADO y TRIPLICADO.

