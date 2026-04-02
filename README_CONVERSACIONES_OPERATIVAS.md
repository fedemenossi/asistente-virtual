# Bandeja Operativa de Conversaciones

## Objetivo
La bandeja de conversaciones funciona como herramienta operativa para doctora/secretaria sin automatizar todavia la gestion de turnos. El foco es clasificar, priorizar, seguir y cerrar pedidos entrantes por WhatsApp.

## Categorias operativas
- `turno_presencial`
- `turno_virtual`
- `receta_orden`
- `otra_consulta`
- `derivacion_humana`
- `sin_clasificar`

La categoria operativa puede venir del flujo conversacional o ajustarse manualmente desde el detalle.

## Vistas
- Tenant: `/t/conversation-states`
- Tenant detalle activo: `/t/conversation-states/{telefono}`
- Tenant detalle historico: `/t/conversation-states/history/{history_id}`
- Super admin global: `/admin/conversation-states`

## Filtros disponibles
- estado: pendientes, resueltas, todas
- categoria operativa
- subtipo tecnico
- solo con adjuntos
- solo revision humana
- hoy
- ultimas 24 horas
- rango `start_date` / `end_date`
- super admin: filtro adicional por tenant

## Acciones manuales
- marcar como resuelta
- volver a pendiente
- cambiar categoria operativa
- guardar nota interna breve

Las acciones dejan trazabilidad en audit log y preservan aislamiento multi-tenant.

## Aislamiento multi-tenant
- `TENANT_ADMIN` solo consulta y opera estados de su tenant.
- `SUPER_ADMIN` puede consultar la bandeja global desde `/admin/conversation-states`.
- La identificacion operativa sigue siendo por `tenant_id + telefono`, y el historial se conserva en `conversaciones_historial`.

## Datos relevantes
### `estados_conversacion`
- `status`
- `pending_reason`
- `pending_message`
- `conversation_category`
- `conversation_subtype`
- `operational_category`
- `manual_note`
- `requires_human_review`
- `has_media`
- `last_patient_message`
- `pending_at`
- `resolved_at`
- `resolved_by`
- `updated_at`

### `conversaciones_historial`
Snapshot de la conversacion al cerrarse, incluyendo categoria operativa y nota interna vigente al momento de resolver.

## Uso manual sugerido
1. Abrir `/t/conversation-states`.
2. Filtrar por pendientes o por categoria.
3. Entrar al detalle de la conversacion.
4. Ajustar categoria operativa si hace falta.
5. Guardar nota interna breve.
6. Marcar como resuelta o volver a pendiente segun corresponda.
