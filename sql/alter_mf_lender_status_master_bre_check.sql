-- Flag lender statuses eligible for BRE upgrades, with optional % threshold.
-- Used by CRM Rejection Analysis ("Include for BRE upgrades").

ALTER TABLE mf_lender_status_master
  ADD COLUMN bre_check TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN bre_threshold TINYINT UNSIGNED NULL DEFAULT NULL;
