-- D2: the API streams legacy PDFs using SQL slices. No document writes.
DO $migration$
BEGIN
    IF to_regclass('public.documents') IS NOT NULL THEN
        GRANT SELECT (id, data) ON public.documents TO arh_api;
    END IF;
END;
$migration$;
