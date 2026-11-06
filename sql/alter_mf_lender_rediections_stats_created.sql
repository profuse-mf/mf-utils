-- Add created (DATE) + created_date (DATETIME) to mf_lender_rediections_stats.
-- Run on MySQL before deploying sync_trackier_clicks / retention / CH sync updates.

ALTER TABLE mf_lender_rediections_stats
  ADD COLUMN created_date DATETIME NULL AFTER application_id,
  ADD COLUMN created DATE NULL AFTER created_date;

-- Backfill existing rows from linked application timestamp.
UPDATE mf_lender_rediections_stats AS r
INNER JOIN application_master AS a ON a.id = r.application_id
SET
  r.created_date = a.created,
  r.created = DATE(a.created)
WHERE r.created_date IS NULL;

-- Defaults for new inserts (scripts also set these explicitly).
-- Leave created nullable until any orphan rows (no application_master match) are fixed.
ALTER TABLE mf_lender_rediections_stats
  MODIFY COLUMN created_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ADD KEY idx_mf_lrs_created_date (created_date),
  ADD KEY idx_mf_lrs_created (created);

-- ClickHouse (run separately on CH):
-- ALTER TABLE mf_lender_rediections_stats
--   ADD COLUMN IF NOT EXISTS created_date Nullable(DateTime) AFTER application_id,
--   ADD COLUMN IF NOT EXISTS created Nullable(Date) AFTER created_date;
