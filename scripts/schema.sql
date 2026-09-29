-- Esquema RAG para lexbancario-ai (Supabase / pgvector 768d)
-- Ejecutar en: SQL Editor del proyecto Supabase
-- Dashboard: https://supabase.com/dashboard/project/pwljuxllyvdriaezzxla/sql/new

create extension if not exists vector;

create table if not exists public.normativa_bancaria (
    id bigint generated always as identity primary key,
    documento_origen text not null,
    organismo text default 'ASFI / Banco Unión',
    tipo_norma text default 'Circular / Resolución',
    jerarquia text default 'RNSF',
    articulo_ref text,
    contenido text,
    embedding vector(768),
    created_at timestamptz default now()
);

create index if not exists normativa_bancaria_embedding_idx
    on public.normativa_bancaria using hnsw (embedding vector_cosine_ops);

create or replace function public.match_normativa(
    query_embedding vector(768),
    match_threshold float,
    match_count int
)
returns table (
    id bigint,
    documento_origen text,
    organismo text,
    tipo_norma text,
    jerarquia text,
    articulo_ref text,
    contenido text,
    similarity float
)
language sql stable
as $$
    select
        id,
        documento_origen,
        organismo,
        tipo_norma,
        jerarquia,
        articulo_ref,
        contenido,
        1 - (embedding <=> query_embedding) as similarity
    from normativa_bancaria
    where 1 - (embedding <=> query_embedding) > match_threshold
    order by embedding <=> query_embedding
    limit match_count;
$$;