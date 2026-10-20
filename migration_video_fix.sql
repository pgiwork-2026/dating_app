-- Safe to re-run. Fixes video invites failing when an older video_dates status CHECK rejects 'invited'.
do $$ declare c text; begin
  if to_regclass('public.video_dates') is not null then
    for c in select conname from pg_constraint where conrelid = 'public.video_dates'::regclass and contype = 'c' loop
      execute format('alter table video_dates drop constraint %I', c);
    end loop;
    alter table video_dates add constraint video_dates_status_check
      check (status in ('invited','scheduled','accepted','rejected','cancelled','completed'));
  end if;
end $$;