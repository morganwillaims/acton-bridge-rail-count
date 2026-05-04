-- Run this after the TD collector has been running for a while.
-- It shows berth/headcode activity in the CE area so we can identify the real Acton Bridge approach berths.

select
  area_id,
  berth,
  description,
  count(*) as times_seen,
  max(event_ts) as last_seen
from public.td_berth_events
where area_id = 'CE'
  and description is not null
  and description ~ '^[0-9][A-Z][0-9]{2}$'
group by area_id, berth, description
order by last_seen desc
limit 200;

-- Once we identify real approach berths, add them here, for example:
-- insert into public.td_berth_map (area_id, berth, side, label)
-- values
--   ('CE', 'XXXX', 'hartford', 'Approaching from Hartford Junction'),
--   ('CE', 'YYYY', 'weaver', 'Approaching from Weaver Junction')
-- on conflict (area_id, berth) do update
-- set side = excluded.side, label = excluded.label;
