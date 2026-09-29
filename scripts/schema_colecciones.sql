-- ============================================================================
-- MIGRACIÓN MULTI-TENANT / AISLAMIENTO VECTORIAL POR COLECCIÓN
-- Proyecto: pwljuxllyvdriaezzxla
-- Ejecutar en: SQL Editor de Supabase (Dashboard -> SQL -> New query)
-- ============================================================================

-- 1) Columna de colección (aislamiento lógico multi-tenant)
alter table public.normativa_bancaria
    add column if not exists coleccion_id varchar(50) not null default 'asfi_bancaria_2026';

-- 2) Índice B-Tree exigido por el enunciado (filtro rápido por dominio)
create index if not exists idx_normativa_coleccion_btree
    on public.normativa_bancaria using btree (coleccion_id);

-- Índice compuesto: acelera el check de "ya ingerido" por (coleccion, documento)
create index if not exists idx_normativa_coleccion_documento
    on public.normativa_bancaria using btree (coleccion_id, documento_origen);

-- 3) Backfill explícito de los registros preexistentes (22 filas de la Fase 3)
update public.normativa_bancaria
   set coleccion_id = 'asfi_bancaria_2026'
 where coleccion_id is null
    or coleccion_id = '';

-- 4) RPC con aislamiento determinista por colección (PL/pgSQL)
--    El filtro por coleccion_id se evalúa DENTRO de la consulta: ningún vector
--    de otra colección puede ser retornado, ni siquiera por error de similitud.
create or replace function public.match_normativa_coleccion(
    query_embedding   vector(768),
    p_coleccion_id    varchar,
    match_threshold   float,
    match_count       int
)
returns table (
    id               bigint,
    documento_origen text,
    coleccion_id     varchar,
    organismo        text,
    tipo_norma       text,
    jerarquia        text,
    articulo_ref     text,
    contenido        text,
    similarity       float
)
language plpgsql
stable
as $$
declare
    v_faltantes int;
begin
    -- Guarda de integridad: colección inexistente -> conjunto vacío explícito
    -- (nunca degradar a búsqueda global, que causaría contaminación cruzada).
    -- La tabla se califica porque 'coleccion_id' es ambigua: existe como columna
    -- y como variable de salida de RETURNS TABLE.
    select count(*) into v_faltantes
      from public.normativa_bancaria n
     where n.coleccion_id = p_coleccion_id;

    if v_faltantes = 0 then
        return;
    end if;

    return query
    select
        n.id,
        n.documento_origen,
        n.coleccion_id,
        n.organismo,
        n.tipo_norma,
        n.jerarquia,
        n.articulo_ref,
        n.contenido,
        (1 - (n.embedding <=> query_embedding))::float as similarity
    from public.normativa_bancaria n
    where n.coleccion_id = p_coleccion_id                 -- AISLAMIENTO DETERMINISTA
      and n.embedding is not null
      and (1 - (n.embedding <=> query_embedding)) > match_threshold
    order by n.embedding <=> query_embedding                -- coseno ascendente
    limit greatest(1, least(match_count, 200));
end;
$$;

-- 5) Inventario de colecciones (para validar aislamiento y poblar el frontend)
create or replace view public.colecciones_registradas as
select
    coleccion_id,
    count(*)                                   as documentos,
    min(created_at)                            as primera_carga,
    max(created_at)                            as ultima_carga
from public.normativa_bancaria
group by coleccion_id
order by coleccion_id;
