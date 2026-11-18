-- Flag lender statuses eligible for retargeting / remarketing campaigns.
-- Used by CRM Rejection Analysis ("Include in retargeting").

ALTER TABLE mf_lender_status_master
  ADD COLUMN remarketing TINYINT(1) NOT NULL DEFAULT 0
  AFTER first_seen;
