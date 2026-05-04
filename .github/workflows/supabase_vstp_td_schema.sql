-- Acton Bridge Rail Count — VSTP + TD live-status layer schema
-- Run this in Supabase SQL Editor before uploading the new collectors.

create table if not exists public.vstp_services (
  id uuid primary key default gen_random_uuid(),
  train_uid text,
  signalling_id text,
  origin_tiploc text,
  origin_name text,
  destination_tiploc text,
  destination_name text,
  atoc_code text,
  schedule_start_date date,
  schedule_end_date date,
  days_runs text,
  stp_indicator text,
  transaction_type text,
  raw jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create unique index if not exists vstp_services_unique_idx
on public.vstp_services (train_uid, signalling_id, schedule_start_date, schedule_end_date);

create table if not exists public.vstp_locations (
  id uuid primary key default gen_random_uuid(),
  service_id uuid references public.vstp_services(id) on delete cascade,
  location_order integer,
  tiploc text,
  location_name text,
  signalling_id text,
  arrival text,
  departure text,
  pass_time text,
  platform text,
  line text,
  path text,
  created_at timestamptz default now()
);

create index if not exists vstp_locations_tiploc_idx on public.vstp_locations (tiploc);
create index if not exists vstp_locations_service_idx on public.vstp_locations (service_id);

create table if not exists public.td_berth_events (
  id uuid primary key default gen_random_uuid(),
  event_ts timestamptz default now(),
  area_id text,
  msg_type text,
  from_berth text,
  to_berth text,
  berth text,
  description text,
  raw jsonb
);

create index if not exists td_berth_events_area_ts_idx on public.td_berth_events (area_id, event_ts desc);
create index if not exists td_berth_events_desc_idx on public.td_berth_events (description);

create table if not exists public.td_current_berths (
  area_id text not null,
  berth text not null,
  description text,
  updated_at timestamptz default now(),
  primary key (area_id, berth)
);

-- Map TD berths to approach sides once discovered.
-- Leave this empty at first; the TD discovery collector will show the real berths seen around Crewe/Acton.
create table if not exists public.td_berth_map (
  area_id text not null,
  berth text not null,
  side text not null check (side in ('hartford','weaver','acton','ignore')),
  label text,
  primary key (area_id, berth)
);

create table if not exists public.acton_live_status (
  id text primary key default 'ACB',
  updated_at timestamptz default now(),
  td_last_seen timestamptz,
  hartford_headcode text,
  hartford_berth text,
  weaver_headcode text,
  weaver_berth text,
  confidence text,
  note text
);

insert into public.acton_live_status (id, note)
values ('ACB', 'TD layer installed; waiting for berth mapping')
on conflict (id) do nothing;
