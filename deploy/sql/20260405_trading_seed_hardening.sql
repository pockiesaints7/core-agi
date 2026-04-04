create unique index if not exists idx_kb_articles_source_id_unique
on public.kb_articles (source_id)
where source_id is not null;

create index if not exists idx_kb_concepts_best_source_id
on public.kb_concepts (best_source_id)
where best_source_id is not null;

alter table if exists public.memory enable row level security;
alter table if exists public.playbook enable row level security;
alter table if exists public.backlog enable row level security;

drop policy if exists service_role_only on public.memory;
create policy service_role_only
on public.memory
for all
to public
using ((select auth.role()) = 'service_role')
with check ((select auth.role()) = 'service_role');

drop policy if exists service_role_only on public.playbook;
create policy service_role_only
on public.playbook
for all
to public
using ((select auth.role()) = 'service_role')
with check ((select auth.role()) = 'service_role');

drop policy if exists service_role_only on public.backlog;
create policy service_role_only
on public.backlog
for all
to public
using ((select auth.role()) = 'service_role')
with check ((select auth.role()) = 'service_role');
