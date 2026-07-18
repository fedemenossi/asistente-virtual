# Serializar emisiones por serie fiscal

Las solicitudes de CAE se serializarán por tenant, emisor, ambiente, punto de venta y tipo de comprobante. Antes de solicitar el siguiente número se verificará el último autorizado y se aplicará una restricción de unicidad local, para evitar duplicados durante emisiones simultáneas.
