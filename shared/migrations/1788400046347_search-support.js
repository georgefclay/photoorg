export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addColumns('photos', {
    search_tsv: { type: 'tsvector' },
  });
  pgm.sql(`create index photos_search_tsv_idx on photos using gin (search_tsv)`);

  // Rebuilds the search vector for one photo from:
  //   - description_ai
  //   - all non-hidden comment bodies
  //   - all back transcriptions
  pgm.sql(`
    create or replace function refresh_photo_tsv(p_photo_id bigint) returns void
      language plpgsql as $$
    declare
      v_desc text;
      v_comments text;
      v_backs text;
    begin
      select coalesce(description_ai, '') into v_desc from photos where id = p_photo_id;
      if not found then return; end if;

      select coalesce(string_agg(body, ' '), '') into v_comments
        from comments where photo_id = p_photo_id and not is_hidden;

      select coalesce(string_agg(transcribed_text, ' '), '') into v_backs
        from photo_backs where photo_id = p_photo_id and transcribed_text is not null;

      update photos
         set search_tsv = to_tsvector('english', v_desc || ' ' || v_comments || ' ' || v_backs)
       where id = p_photo_id;
    end
    $$;
  `);

  pgm.sql(`
    create or replace function photos_refresh_tsv_trg() returns trigger
      language plpgsql as $$
    begin
      if (tg_op = 'UPDATE'
          and new.description_ai is not distinct from old.description_ai) then
        return new;
      end if;
      perform refresh_photo_tsv(new.id);
      return new;
    end
    $$;
  `);
  pgm.sql(`
    create trigger photos_refresh_tsv
      after insert or update of description_ai on photos
      for each row execute function photos_refresh_tsv_trg();
  `);

  pgm.sql(`
    create or replace function comments_refresh_tsv_trg() returns trigger
      language plpgsql as $$
    begin
      if tg_op = 'DELETE' then
        perform refresh_photo_tsv(old.photo_id);
        return old;
      end if;
      perform refresh_photo_tsv(new.photo_id);
      if tg_op = 'UPDATE' and new.photo_id <> old.photo_id then
        perform refresh_photo_tsv(old.photo_id);
      end if;
      return new;
    end
    $$;
  `);
  pgm.sql(`
    create trigger comments_refresh_tsv
      after insert or update or delete on comments
      for each row execute function comments_refresh_tsv_trg();
  `);

  pgm.sql(`
    create or replace function photo_backs_refresh_tsv_trg() returns trigger
      language plpgsql as $$
    begin
      if tg_op = 'DELETE' then
        perform refresh_photo_tsv(old.photo_id);
        return old;
      end if;
      perform refresh_photo_tsv(new.photo_id);
      if tg_op = 'UPDATE' and new.photo_id <> old.photo_id then
        perform refresh_photo_tsv(old.photo_id);
      end if;
      return new;
    end
    $$;
  `);
  pgm.sql(`
    create trigger photo_backs_refresh_tsv
      after insert or update or delete on photo_backs
      for each row execute function photo_backs_refresh_tsv_trg();
  `);

  // people.search_key = space-joined metaphone of surname, maiden_name, given_name.
  pgm.addColumns('people', {
    search_key: { type: 'text' },
  });
  pgm.createIndex('people', 'search_key');

  pgm.sql(`
    create or replace function people_compute_search_key(p people) returns text
      language sql immutable as $$
      select trim(both ' ' from
        coalesce(metaphone(p.surname,     16), '') || ' ' ||
        coalesce(metaphone(p.maiden_name, 16), '') || ' ' ||
        coalesce(metaphone(p.given_name,  16), '')
      )
    $$;
  `);
  pgm.sql(`
    create or replace function people_set_search_key() returns trigger
      language plpgsql as $$
    begin
      new.search_key := people_compute_search_key(new);
      return new;
    end
    $$;
  `);
  pgm.sql(`
    create trigger people_search_key_trg before insert or update on people
      for each row execute function people_set_search_key();
  `);
};

export const down = (pgm) => {
  pgm.sql(`drop trigger if exists people_search_key_trg on people`);
  pgm.sql(`drop function if exists people_set_search_key()`);
  pgm.sql(`drop function if exists people_compute_search_key(people)`);
  pgm.dropColumns('people', ['search_key']);

  pgm.sql(`drop trigger if exists photo_backs_refresh_tsv on photo_backs`);
  pgm.sql(`drop function if exists photo_backs_refresh_tsv_trg()`);
  pgm.sql(`drop trigger if exists comments_refresh_tsv on comments`);
  pgm.sql(`drop function if exists comments_refresh_tsv_trg()`);
  pgm.sql(`drop trigger if exists photos_refresh_tsv on photos`);
  pgm.sql(`drop function if exists photos_refresh_tsv_trg()`);
  pgm.sql(`drop function if exists refresh_photo_tsv(bigint)`);
  pgm.dropColumns('photos', ['search_tsv']);
};
