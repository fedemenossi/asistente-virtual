

# Requisitos de ARCA (ex AFIP) para la generación del PDF de comprobantes electrónicos con CAE: guía técnica para desarrolladores

## TL;DR

- **El PDF que tu sistema "dibuja" tras obtener el CAE por web service debe replicar TODOS los datos del Anexo II de la RG 1415/2003 (emisor, receptor, letra, numeración, fecha) más el CAE, su fecha de vencimiento y el código QR de la RG 4892/2020; el código de barras Interleaved 2 de 5 dejó de ser obligatorio para comprobantes electrónicos (lo eliminó la RG 4892).** El diseño visual (logo, colores, tipografía, bloques extra) es libre siempre que se respeten el recuadro de la letra, las medidas mínimas (15 cm × 20 cm) y la legibilidad de los datos obligatorios.
- **Las diferencias A/B/C son de contenido fiscal, no de formato:** en la "A" se discrimina IVA y se exigen todos los datos del receptor RI/monotributista; en la "B" no se discrimina IVA pero (desde 2025) debe figurar "IVA Contenido" por la Ley 27.743; en la "C" (monotributo) nunca se discrimina IVA ni hay crédito fiscal.
- **Como monotributista emitís "C"; si tu sistema genera "A" y "B" para terceros RI debe soportar la discriminación de IVA, las leyendas de la Ley 27.618 y de Transparencia Fiscal, y la condición frente al IVA del receptor (RG 5616/2024), que el web service acepta desde el 6/4/2025 y cuya obligatoriedad plena fue prorrogada por la RG (ARCA) 5782 al 1/8/2026.**

## Key Findings

1. **Norma madre:** la RG (AFIP) 1415/2003 y su Anexo II fijan el contenido y la ubicación de los datos de todo comprobante A/B/C/E, sea preimpreso, por controlador fiscal o electrónico. La RG 4291/2018 regula el régimen de emisión electrónica y la representación gráfica; la RG 4892/2020 incorpora el QR y elimina el código de barras para electrónicos; la RG 5614/2024 (Transparencia Fiscal, Ley 27.743) y la RG 5616/2024 (condición IVA del receptor + moneda extranjera) son las novedades 2024-2026.
2. **El web service solo devuelve datos de autorización (CAE, fecha de vencimiento del CAE, resultado).** No genera PDF. Tu sistema es responsable de armar el comprobante completo e incorporar esos datos. ARCA no homologa ni certifica el diseño del PDF: la responsabilidad de que contenga todos los datos es del emisor.
3. **El recuadro de la letra es obligatorio** y es el único elemento de diseño rígidamente normado: la letra (A/B/C) va destacada en el centro del espacio superior, con el código numérico de tipo de comprobante debajo.
4. **El QR es obligatorio para todos los comprobantes electrónicos** y reemplazó al código de barras. El código de barras Interleaved 2 de 5 ya NO es obligatorio en comprobantes electrónicos.

## Details

### 1. Contenido obligatorio (RG 1415/2003, Anexo II, Apartado A + RG 4291 Art. 14)

El **Art. 14 de la RG 4291/2018** establece que, una vez asignado el C.A.E., el comprobante puesto a disposición del receptor debe contener: **a) el "C.A.E."; b) el código identificatorio del tipo de comprobante; c) de corresponder, el código representativo de la leyenda que indica que el impuesto discriminado no puede computarse como crédito fiscal; y d) todos los demás datos previstos en el Apartado A del Anexo II de la RG 1.415** (con las excepciones del punto 9 del inciso a y del inciso c del Acápite I, que son datos propios de la imprenta/CAI que no aplican al electrónico). El **Art. 15 de la RG 4291** agrega que la representación gráfica debe consignar al pie el "C.A.E." o "C.A.E.A." en reemplazo del logo "CF".

**Datos obligatorios del emisor (Anexo II, A, I):**

- Apellido y nombres, denominación o razón social.
- Domicilio comercial (el del lugar físico de emisión del comprobante).
- CUIT.
- Número de inscripción en Ingresos Brutos (o condición de no contribuyente).
- Leyenda de condición frente al IVA: "IVA Responsable Inscripto", "IVA Exento", "Responsable Monotributo", etc.
- Numeración consecutiva y progresiva de 12 dígitos (4 de punto de venta + 8 de número).
- Fecha de inicio de actividades, precedida de la leyenda "Inicio de actividades".
- Fecha de emisión.

**Datos del receptor según tipo de comprobante** (ver sección 8).

**CAE y vencimiento del CAE:** se consignan al pie (espacio inferior derecho por analogía con el CAI). En los PDF de ARCA figuran como "CAE N°: ..." y "Fecha de Vto. de CAE: ...". Nota importante para el desarrollador: estos rótulos exactos ("CAE N°", "Fecha de Vto. de CAE:") no son texto literal de la RG 4291; son los rótulos que usa el sistema "Comprobantes en Línea" de ARCA y que la práctica adoptó. Lo que la norma exige (Art. 14 a y Art. 15) es que el CAE figure en el comprobante, consignado al pie.

**Discriminación de IVA:** según tipo (ver sección 8).

### 2. Código de barras Interleaved 2 de 5 (RG 1702/2004; ya NO obligatorio para electrónicos)

Históricamente la RG 1702/2004 obligó a imprimir un código de barras en los comprobantes A/B/C/M/E. La **RG 4892/2020 (Art. 4) eliminó esa obligación para los comprobantes electrónicos** autorizados en los términos de la RG 4291, reemplazándola por el QR. **Conclusión para tu sistema: no estás obligado a imprimir el código de barras**; podés incluirlo opcionalmente, pero el QR es el obligatorio.

Si decidís incluirlo, la especificación (Anexo I RG 1702, modif. por RG 4290) es:

- **Lenguaje:** "Código Entrelazado 2 de 5 (Interleaved 2 of 5 ITF)", continuo, solo numérico, par de dígitos.
- **Datos y orden (40 caracteres):** CUIT emisor (11) + código tipo de comprobante (3) + punto de venta (5) + CAE (14, en reemplazo del CAI) + fecha de vencimiento AAAAMMDD (8) + dígito verificador (1).
- **Dígito verificador:** módulo 10 (suma posiciones impares ×3 + suma posiciones pares; el menor número que completa el múltiplo de 10).
- **Módulo mínimo nominal:** 0,191 mm (0,0075"). Densidad máxima recomendada 7,1 caracteres/cm. Debe llevar zonas mudas (quiet zones) a izquierda y derecha y los dígitos en texto debajo del código. No imprimir con matriz de punto. Evitar tintas rojas/azul reflejo.
- **Ubicación:** cualquier sector, anverso o reverso, sin obstaculizar los datos obligatorios.

### 3. Código QR obligatorio (RG 4892/2020)

**Obligatorio para todos los comprobantes electrónicos** autorizados por RG 4291. Si emitís por "Comprobantes en Línea" o "Facturador Móvil", ARCA lo agrega automáticamente; si emitís por **web service y generás el PDF, debés generar el QR vos mismo.**

**Especificación técnica (micrositio afip.gob.ar/fe/qr):**

- El QR codifica el texto: `{URL}?p={DATOS_CMP_BASE64}` donde `{URL}` = `https://www.afip.gob.ar/fe/qr/` (el micrositio publica también la variante `https://www.arca.gob.ar/fe/qr/`) y `{DATOS_CMP_BASE64}` es un JSON con los datos del comprobante codificado en **Base64**.
- **JSON (versión 1), campos:**
  - `ver` (numérico, 1 dígito) – versión del formato = 1 (OBLIGATORIO)
  - `fecha` (full-date RFC3339, "AAAA-MM-DD") – fecha de emisión (OBLIGATORIO)
  - `cuit` (numérico 11) – CUIT emisor (OBLIGATORIO)
  - `ptoVta` (numérico hasta 5) – punto de venta (OBLIGATORIO)
  - `tipoCmp` (numérico hasta 3) – tipo de comprobante (OBLIGATORIO)
  - `nroCmp` (numérico hasta 8) – número de comprobante (OBLIGATORIO)
  - `importe` (decimal hasta 13 enteros, 2 decimales) – importe total (OBLIGATORIO)
  - `moneda` (3 caracteres, p.ej. "PES", "DOL") (OBLIGATORIO)
  - `ctz` (decimal hasta 13 enteros, 6 decimales) – cotización; 1 si es pesos (OBLIGATORIO)
  - `tipoDocRec` (numérico hasta 2) – tipo doc receptor (DE CORRESPONDER)
  - `nroDocRec` (numérico hasta 20) – número doc receptor (DE CORRESPONDER)
  - `tipoCodAut` (string) – "E" para CAE, "A" para CAEA (OBLIGATORIO)
  - `codAut` (numérico 14) – el CAE/CAEA (OBLIGATORIO)
- **Ejemplo JSON:** `{"ver":1,"fecha":"2020-10-13","cuit":30000000007,"ptoVta":10,"tipoCmp":1,"nroCmp":94,"importe":12100,"moneda":"DOL","ctz":65,"tipoDocRec":80,"nroDocRec":20000000001,"tipoCodAut":"E","codAut":70417054367476}`
- **Ubicación:** en el frente del comprobante, sin obstaculizar los datos obligatorios. No hay tamaño mínimo expresamente normado en cm; debe ser legible por una cámara estándar de celular.
- **Vigencia:** desde 24/12/2020 para "Comprobantes en Línea"; para web service según cronograma escalonado por facturación 2020. Conforme el sitio oficial de ARCA (afip.gob.ar/fe/qr/vigencia-y-aplicacion.asp): RI con ventas 2020 superiores a $10M desde el 1/3/2021; superiores a $2M e iguales o inferiores a $10M desde el 1/4/2021; superiores a $500.000 e iguales o inferiores a $2M desde el 1/5/2021; y "para el resto de los responsables inscriptos en el impuesto al valor agregado, sujetos exentos ante dicho gravamen y pequeños contribuyentes inscriptos en el Monotributo: a partir del 1 junio de 2021".
- **Formato del importe en notas de crédito:** según versión; consultar las FAQ de ARCA (el campo importe puede informarse en valor absoluto).

### 4. Leyendas obligatorias

- **"A CONSUMIDOR FINAL":** en comprobantes B/C a consumidor final (la consigna automáticamente el sistema de ARCA; en sistemas propios hay que incluirla).
- **Régimen de Transparencia Fiscal al Consumidor (Ley 27.743 / RG 5614/2024):** en comprobantes **B** (y demás clases a consumidor final) emitidos por **responsables inscriptos**, debe figurar la leyenda **"Régimen de Transparencia Fiscal al Consumidor (Ley 27.743)"** y debajo los datos **"IVA Contenido"** y **"Otros Impuestos Nacionales Indirectos"**, cada uno con su valor. Ubicación indicada: espacio inferior izquierdo del comprobante. Vigencia escalonada según el Art. 6 de la RG (ARCA) 5614/2024 (BO 13/12/2024): "a) A partir del 1° de enero de 2025, las 'empresas grandes' definidas según el artículo 2° de la Resolución General N° 4.367...; b) El resto de los contribuyentes... podrán discriminar el impuesto al valor agregado desde el 1° de enero de 2025, siendo obligatorio su cumplimiento a partir del 1 de abril del citado año". **Los monotributistas (factura C) están EXCLUIDOS:** según las FAQ oficiales de ARCA difundidas en marzo de 2025, "la medida no incluye a los contribuyentes del Régimen Simplificado para Pequeños Contribuyentes (monotributistas), quienes quedan exentos de esta obligación"; el incumplimiento se sanciona con clausura de 2 a 6 días (Ley de Procedimientos Fiscales).
- **Leyenda Ley 27.618 (factura A a monotributista):** cuando un RI emite "A" a un monotributista debe incluir **"El crédito fiscal discriminado en el presente comprobante, sólo podrá ser computado a efectos del Régimen de Sostenimiento e Inclusión Fiscal para Pequeños Contribuyentes de la Ley Nº 27.618"** (RG 5003/2021, modif. Art. 15 RG 1415).
- **"Esta Administración Federal no se responsabiliza por los datos ingresados en el detalle de la operación":** figura en los PDF de "Comprobantes en Línea". Es leyenda del formato de ARCA, recomendable replicarla.
- **Operaciones de exportación (clase E):** leyenda "IVA EXENTO OPERACIÓN DE EXPORTACIÓN".
- **Factura "A con leyenda":** "OPERACIÓN SUJETA A RETENCIÓN" / "PAGO EN CBU INFORMADA" (reemplazo de factura M para nuevos RI con inconsistencias patrimoniales).

### 5. Requisitos de diseño y formato físico

- **Recuadro del emisor:** los datos del emisor (bloque superior izquierdo) y la numeración/CUIT/IIBB/fecha (bloque superior derecho) deben figurar dentro de un recuadro de **mínimo 7 cm de ancho × 3 cm de alto** (Anexo II, B).
- **Recuadro/letra del comprobante:** en el **centro del espacio superior** se consigna en forma destacada la letra A/B/C/E. Debajo de la letra se ubica el código numérico de tipo de comprobante de 3 dígitos (p. ej. "COD. 01" para factura A, "06" factura B, "11" factura C). Este es el característico cuadro central con la letra grande.
- **Medidas mínimas del comprobante:** **15 cm de ancho × 20 cm de largo** (Art. 19 RG 1415). Aplica a la representación gráfica/PDF.
- **Ubicación de datos (Anexo II, B):** superior izquierdo = datos del emisor; superior derecho = numeración (centrada), fecha de emisión, CUIT, IIBB, inicio de actividades; centro superior = letra; luego datos del receptor, condiciones de venta, detalle, y al pie el CAE/vencimiento.
- **Tipografía:** la fecha de vencimiento debe consignarse en caracteres no inferiores al **tamaño tipográfico 12**. El resto: legibilidad suficiente. No hay fuente obligatoria.
- **Márgenes/orientación/papel:** no hay norma específica más allá del tamaño mínimo; vertical es lo habitual.

### 6. Qué se puede agregar libremente

- Logo y nombre de fantasía de la empresa.
- Colores, fuentes y diseño general (respetando legibilidad y el recuadro de la letra).
- Información de contacto adicional (teléfono, email, web, redes).
- Datos bancarios / CBU / alias para pago.
- Términos y condiciones, leyendas comerciales, mensajes promocionales.
- Campos operativos propios (número de orden, vendedor, observaciones).

Los datos obligatorios no contemplados en el Anexo II y los que surjan de la actividad pueden ubicarse sin sujeción a distribución, siempre que sean legibles (Art. 19 RG 1415).

### 7. Qué está prohibido o restringido

- Omitir cualquier dato obligatorio del Anexo II o el CAE/vencimiento.
- Alterar, mover fuera del recuadro o reducir la letra del comprobante.
- Tapar u obstaculizar con el logo, QR o código de barras la visualización de datos obligatorios.
- Discriminar IVA en comprobantes B o C (prohibido).
- Reducir el comprobante por debajo de 15 × 20 cm.
- Emitir un PDF como "original" sin haber obtenido el CAE: sin CAE el comprobante carece de validez fiscal.

### 8. Diferencias A / B / C

| Aspecto                           | Factura A                                                   | Factura B                                                                        | Factura C                  |
| --------------------------------- | ----------------------------------------------------------- | -------------------------------------------------------------------------------- | -------------------------- |
| Emisor                            | Responsable Inscripto                                       | Responsable Inscripto                                                            | Monotributo / Exento       |
| Receptor                          | RI u otro RI / Monotributo                                  | Consumidor final, exento, monotributo                                            | Cualquiera                 |
| Discriminación de IVA             | **Sí**, alícuota + monto                                    | **No** (IVA incluido)                                                            | **No** (no hay IVA)        |
| Datos del receptor                | Razón social, domicilio, CUIT, condición IVA (obligatorios) | Identificación obligatoria solo si supera el umbral; si no, "A consumidor final" | Igual que B según receptor |
| Crédito fiscal                    | Sí (salvo leyenda)                                          | No                                                                               | No                         |
| Transparencia Fiscal (Ley 27.743) | No aplica (B2B)                                             | **Sí** ("IVA Contenido")                                                         | No (monotributo excluido)  |
| Código de tipo                    | 01                                                          | 06                                                                               | 11                         |

- **Identificación del consumidor final:** desde la **RG (ARCA) 5700/2025** —publicada en el Boletín Oficial el 27/5/2025 y vigente desde el 29/5/2025, que modifica las RG 1.415, 3.561 y 5.198—, solo es obligatorio identificar al consumidor final (CUIT/CUIL/CDI o DNI) cuando la operación es **igual o superior a $10.000.000**, sin importar el medio de pago, y se eliminó la actualización semestral por IPC del INDEC. Los topes anteriores eran $208.644 (efectivo/otros medios) y $417.288 (medios electrónicos). Por debajo de ese monto basta "A consumidor final". La misma RG 5700/2025 elevó a $500.000 por operación el tope para usar el "Facturador" web/móvil de ARCA ($250.000 para monotributistas sociales).

### 9. Monotributo emitiendo C (y también A y B)

- Como monotributista emitís **comprobantes C** por todas tus ventas, sin importar la condición del cliente (RI, monotributista, consumidor final, exento). **Nunca discriminás IVA** ni generás crédito fiscal.
- En la C no aplica la leyenda de Transparencia Fiscal (Ley 27.743) ni la discriminación de "IVA Contenido": los monotributistas están expresamente excluidos.
- Si tu **software se usa también por responsables inscriptos** que emiten A y B, el sistema debe:
  - Soportar discriminación de IVA por alícuota en la A.
  - Incluir la leyenda Ley 27.618 cuando la A se emite a un monotributista.
  - Incluir el bloque de Transparencia Fiscal ("IVA Contenido" + "Otros Impuestos Nacionales Indirectos") en la B.
  - Manejar la condición frente al IVA del receptor (RG 5616).
- El tipo de comprobante y la letra los determina la combinación emisor/receptor; tu lógica debe seleccionarlos antes de pedir el CAE (el web service valida la coherencia letra/tipo/condición del receptor).

### 10. Validaciones técnicas para sistemas que generan el PDF tras CAE por web service

- **WSAA + WSFEv1 (o WSMTXCA para detalle):** primero autenticás (ticket de acceso, service "wsfe"), luego `FECAESolicitar`. El WS devuelve CAE y `CAEFchVto`. Guardá el `Id`/secuencia de transacción para reproceso ante fallas (clave para recuperar un CAE sin duplicar numeración).
- **Numeración:** consultá `FECompUltimoAutorizado` y usá el inmediato siguiente; el WS valida consecutividad por tipo/punto de venta.
- **Condición frente al IVA del receptor (RG 5616/2024):** desde el **6/4/2025** el WS acepta el campo `CondicionIVAReceptorId` (p. ej. 6 = Responsable Monotributo); según el Manual del Desarrollador, "a contar del 6 de abril de 2025 el campo Condición Frente al IVA del Receptor pasará a ser Opcional" y su obligatoriedad —inicialmente prevista para el 15/4/2025 por la RG 5616— fue postergada. La **RG (ARCA) 5782 prorrogó la entrada en vigencia al 1 de agosto de 2026** ("razones de buena administración tributaria aconsejan posponer al 1 de agosto de 2026 la entrada en vigencia"); las versiones 4.4 y 4.5 del Manual del Desarrollador RG 4291 establecen la obligatoriedad del campo y dejan en desuso los códigos no excluyentes 10245 (CAE) y 825 (CAEA). Las validaciones excluyentes son los códigos 10242/10243. **Recomendación:** enviá el campo siempre para no recibir rechazos.
- **Moneda extranjera (RG 5616):** si facturás en moneda extranjera y se cancela en la misma moneda, debés informar `cancelaEnMismaMonedaExtranjera` (S/N) y la cotización del tipo de cambio vendedor divisa del BNA del día hábil anterior; el PDF debe consignar el tipo de cambio utilizado.
- **El QR lo generás vos** (el WS no lo provee). El código de barras es opcional.
- **Constatación:** al escanear el QR se accede a `serviciosweb.afip.gob.ar/genericos/comprobantes/cae.aspx` (CAE) o `.../caea.aspx` (CAEA), que valida el comprobante. No hay entorno de testing del QR: se prueba escaneándolo.
- **Diferencia con controlador fiscal / Comprobantes en Línea:** el controlador fiscal emite el documento fiscal directamente (otro régimen, RG 3561); "Comprobantes en Línea" arma el PDF y el QR automáticamente. Al generar tu propio PDF asumís la responsabilidad de incluir todos los datos del Anexo II + CAE + QR.

## Recommendations

1. **Construí una plantilla base que cumpla el "esqueleto" obligatorio** y dejá el resto del lienzo como zonas libres para diseño:
   - Recuadro emisor ≥ 7×3 cm (sup. izq.), bloque numeración/CUIT/IIBB/inicio actividades (sup. der.), letra + código de 3 dígitos en cuadro central superior, tamaño total ≥ 15×20 cm.
   - Pie con "CAE N°" + "Fecha de Vto. de CAE:" y la leyenda de no responsabilidad de ARCA.
   - QR en el frente.
2. **Generá el QR en cada comprobante** con el JSON v1 → Base64 → `https://www.afip.gob.ar/fe/qr/?p=...`. Validá escaneando con un celular que abra la página de constatación. No dependás del código de barras (ya no es obligatorio para electrónicos).
3. **Parametrizá las leyendas por tipo de comprobante y condición del receptor:** Ley 27.618 (A a monotributista), Transparencia Fiscal + "IVA Contenido" (B de RI a consumidor final), "A consumidor final" (B/C). Para tus propias C, no agregues Transparencia Fiscal.
4. **Implementá ya el campo `CondicionIVAReceptorId`** en la solicitud de CAE para todos los comprobantes; tratalo como obligatorio para no recibir rechazos. **Benchmark que cambia la decisión:** la obligatoriedad plena rige (según RG 5782) desde el 1/8/2026; si ARCA publica una nueva prórroga, ajustá el plan, pero enviarlo siempre es la opción segura.
5. **Para el umbral de identificación de consumidor final usá $10.000.000 (RG 5700/2025)** y recordá que ya no hay actualización automática por IPC; revisá si ARCA reintroduce un nuevo ajuste.
6. **Guardá el `Id` de transacción y construí un mecanismo de reproceso** para recuperar CAE ante caídas de red, evitando saltos o duplicación de numeración.

## Caveats

- **"ARCA" = ex AFIP:** la AFIP fue reemplazada por la Agencia de Recaudación y Control Aduanero (2024); las RG citadas conservan vigencia. Los dominios afip.gob.ar y arca.gob.ar conviven.
- **El rótulo "Comprobante Autorizado" no es una leyenda normada** por la RG 4291; es una etiqueta del formato de PDF de "Comprobantes en Línea". Lo exigido es consignar el CAE al pie (Art. 14 y 15 RG 4291). No es obligatorio reproducir esa etiqueta literal en tu PDF.
- **Las fechas de obligatoriedad de la RG 5616 sufrieron varias prórrogas** (15/4/2025 → sucesivas postergaciones → 1/8/2026 por la RG 5782). Confirmá la fecha vigente en el manual del desarrollador de ARCA antes de pasar a producción.
- **El tamaño mínimo del QR en cm no está fijado por norma**; solo se exige legibilidad. Usá un módulo y margen suficientes para lectura desde celular.
- Algunos datos de detalle (umbrales, versiones de manual del WS, códigos de validación) cambian con frecuencia; verificá contra los manuales oficiales de ARCA (afip.gob.ar/ws y afip.gob.ar/fe) en el momento de implementar.