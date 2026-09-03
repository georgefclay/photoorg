export const shorthands = undefined;

export const up = (pgm) => {
  // 40 points: capture_date_confirmed = true
  // 40 points: at least one non-disputed, non-deleted face with a person_id
  //            OR photos.has_no_people = true
  // 20 points: at least one place linked via photo_places
  pgm.sql(`
    create or replace function compute_completeness(p_photo_id bigint) returns int
      language plpgsql stable as $$
    declare
      s int := 0;
      v_date_ok bool;
      v_face_ok bool;
      v_no_people bool;
      v_place_ok bool;
    begin
      select capture_date_confirmed, has_no_people
        into v_date_ok, v_no_people
        from photos where id = p_photo_id;
      if not found then return 0; end if;

      if v_date_ok then s := s + 40; end if;

      if v_no_people then
        s := s + 40;
      else
        select exists (
          select 1 from faces
           where photo_id = p_photo_id
             and person_id is not null
             and not is_disputed
             and not is_deleted
        ) into v_face_ok;
        if v_face_ok then s := s + 40; end if;
      end if;

      select exists (
        select 1 from photo_places where photo_id = p_photo_id
      ) into v_place_ok;
      if v_place_ok then s := s + 20; end if;

      return s;
    end
    $$;
  `);

  pgm.sql(`
    create or replace function refresh_completeness(p_photo_id bigint) returns int
      language plpgsql as $$
    declare v_score int;
    begin
      v_score := compute_completeness(p_photo_id);
      update photos set completeness_score = v_score where id = p_photo_id;
      return v_score;
    end
    $$;
  `);

  pgm.sql(`
    create or replace function refresh_all_completeness() returns bigint
      language plpgsql as $$
    declare v_count bigint := 0;
    begin
      update photos
         set completeness_score = compute_completeness(id)
       where not is_deleted;
      get diagnostics v_count = row_count;
      return v_count;
    end
    $$;
  `);
};

export const down = (pgm) => {
  pgm.sql(`drop function if exists refresh_all_completeness()`);
  pgm.sql(`drop function if exists refresh_completeness(bigint)`);
  pgm.sql(`drop function if exists compute_completeness(bigint)`);
};
