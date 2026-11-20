-- Company size as a band ("51-200"), which is how most enrichment sources give it. employee_count stays for an
-- exact number. Safe to run twice.
alter table companies add column if not exists employee_range text;
