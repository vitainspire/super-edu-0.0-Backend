-- Read-only. Lists every foreign key in the public schema that points AT
-- 'schools', plus every FK on textbook_books/chapters/topics/images, so we
-- know exactly what would cascade before truncating anything.
select
    tc.table_name as referencing_table,
    kcu.column_name as referencing_column,
    ccu.table_name as referenced_table,
    ccu.column_name as referenced_column,
    rc.delete_rule
from information_schema.table_constraints tc
join information_schema.key_column_usage kcu
    on tc.constraint_name = kcu.constraint_name and tc.table_schema = kcu.table_schema
join information_schema.constraint_column_usage ccu
    on tc.constraint_name = ccu.constraint_name and tc.table_schema = ccu.table_schema
join information_schema.referential_constraints rc
    on tc.constraint_name = rc.constraint_name and tc.table_schema = rc.constraint_schema
where tc.constraint_type = 'FOREIGN KEY'
  and tc.table_schema = 'public'
  and (
    ccu.table_name = 'schools'
    or tc.table_name in ('textbook_books', 'textbook_chapters', 'textbook_topics', 'textbook_images')
  )
order by referencing_table, referencing_column;
