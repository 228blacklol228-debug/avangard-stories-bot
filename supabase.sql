-- Run once in the SQL Editor of your own FREE Supabase project.
-- Tables are in a private schema, inaccessible through the public data API.
create schema if not exists stories_private;
revoke all on schema stories_private from public, anon, authenticated;

create table if not exists stories_private.state (
  bot text primary key,
  snapshot text not null default '',
  lease_owner uuid,
  lease_until timestamptz,
  updated_at timestamptz not null default now()
);
create table if not exists stories_private.inbox (
  bot text not null references stories_private.state(bot),
  update_id bigint not null,
  payload jsonb,
  done boolean not null default false,
  created_at timestamptz not null default now(),
  primary key(bot,update_id)
);

create or replace function public.stories_enqueue(p_bot text,p_update jsonb)
returns boolean language plpgsql security definer set search_path=pg_catalog,stories_private as $$
begin
  insert into stories_private.state(bot) values(p_bot) on conflict do nothing;
  insert into stories_private.inbox(bot,update_id,payload)
    values(p_bot,(p_update->>'update_id')::bigint,p_update) on conflict do nothing;
  return true;
end $$;

create or replace function public.stories_claim(p_bot text,p_owner uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,stories_private as $$
declare st stories_private.state%rowtype; item stories_private.inbox%rowtype;
begin
  select * into st from stories_private.state where bot=p_bot for update;
  if not found then return null; end if;
  if st.lease_owner is not null and st.lease_owner<>p_owner and st.lease_until>clock_timestamp() then
    return null;
  end if;
  select * into item from stories_private.inbox where bot=p_bot and not done order by update_id limit 1;
  if not found then return null; end if;
  update stories_private.state set lease_owner=p_owner,lease_until=clock_timestamp()+interval '90 seconds' where bot=p_bot;
  return jsonb_build_object('update',item.payload,'snapshot',st.snapshot);
end $$;

create or replace function public.stories_renew(p_bot text,p_owner uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,stories_private as $$
begin
  update stories_private.state set lease_until=clock_timestamp()+interval '90 seconds'
   where bot=p_bot and lease_owner=p_owner and lease_until>clock_timestamp();
  return found;
end $$;

create or replace function public.stories_complete(p_bot text,p_owner uuid,p_update_id bigint,p_snapshot text)
returns boolean language plpgsql security definer set search_path=pg_catalog,stories_private as $$
begin
  update stories_private.state set snapshot=p_snapshot,updated_at=clock_timestamp(),lease_owner=null,lease_until=null
   where bot=p_bot and lease_owner=p_owner and lease_until>clock_timestamp();
  if not found then return false; end if;
  update stories_private.inbox set done=true,payload=null where bot=p_bot and update_id=p_update_id;
  -- Deduplication needs only recent IDs; Telegram does not retain updates for more than 24 hours.
  delete from stories_private.inbox where bot=p_bot and done and created_at<now()-interval '7 days';
  return true;
end $$;

revoke all on function public.stories_enqueue(text,jsonb) from public,anon,authenticated;
revoke all on function public.stories_claim(text,uuid) from public,anon,authenticated;
revoke all on function public.stories_renew(text,uuid) from public,anon,authenticated;
revoke all on function public.stories_complete(text,uuid,bigint,text) from public,anon,authenticated;
grant execute on function public.stories_enqueue(text,jsonb) to service_role;
grant execute on function public.stories_claim(text,uuid) to service_role;
grant execute on function public.stories_renew(text,uuid) to service_role;
grant execute on function public.stories_complete(text,uuid,bigint,text) to service_role;

-- Private original files, never a public bucket. Uploads use the server's service_role key.
insert into storage.buckets(id,name,public,file_size_limit)
values('stories-originals','stories-originals',false,12000000)
on conflict(id) do nothing;
