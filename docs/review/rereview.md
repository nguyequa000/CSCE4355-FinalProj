# Publication re-review — `e611072`

**Verdict: NO-GO for publishing this exact export until public team attribution is cleared.** The four excluded files are gone, the technical privacy scan found no confirmed leak in the requested classes, and all 10 requested database tests passed. The remaining release decision is narrow: the README and report still publish teammates’ full names alongside pending contribution assessments. Removing the proposal does not remove that information. I found no permission decision in the handoff; this is not a finding that anyone refused permission.

## Status of the seven prior findings

| # | Status | Evidence at the reviewed revision |
|---|---|---|
| 1. Paths, addresses, history | Resolved for the prescribed export | Fresh `git archive HEAD`, four exclusions, no `.git` or `.env`; residual scan below. This does not clear a push of the development repository. |
| 2. SQL retention | Resolved for the collector | `scripts/collect.py:45–55` inserts `NULL::text`, not truncated SQL. `tests/test_capture.py:198–214` checks the base table, not just the reader view; passed live. `README.md:460–463` and `docs/report/report.md:319–324` distinguish new captures from historical rows. This is not a database constraint: `dashboard/dev_fixtures.sql:40–44,58–62` deliberately supplies synthetic SQL strings. |
| 3. Reproducing the lock run | Resolved | `README.md:238–251` identifies customer 42, labels the archived single-rule run, and predicts the additional idle-blocker alert with current rules. Compare `workload/lock_demo.py:37,113–117,134–138`, `docs/report/report.md:132,208–209`, and `docs/results/t5_extra_rules_live.txt:161–165`. |
| 4. Notification guarantee and timing | Partially resolved | `README.md:412–415` and `docs/report/report.md:425–431,545–546,557–560` correctly distinguish stamp delay from visibility and disclose retry duplicates. But `notifier/notify.py:11–13` still says the stamp commits “with the print”; lines 55–81 emit before the stamp commits. `README.md:47` cites the console log for the stamp, but that log contains only event times; the stamp is in `docs/results/live/alert_sql.txt:17–18`. |
| 5. Wait duration | Resolved by qualification, not by changing the clock | `docs/report/report.md:334–340,642–646` explains why `state_change` may predate a wait and trigger early, and distinguishes `pg_locks.waitstart`. `README.md:55–58` describes the immediate-update case. The 52 ms difference is between the state transition and the client's confirmation, **not** a measurement of the error against `waitstart`; see `workload/lock_demo.py:166–172`. |
| 6. Evidence labels | Resolved | `README.md:304`, `docs/report/report.md:501`: first four of seven samples. `README.md:361–363` agrees with inclusive comparisons at `sql/21_extra_rules.sql:183,189`. `README.md:275–279` and `docs/report/report.md:555` split five denials into three reader and two app; all six privilege tests passed. |
| 7. Course files and team metadata | Partially resolved — release gate | Both course handouts, the plan, and proposal PDF were absent. `README.md:7–11` still pairs named teammates with pending work; `docs/report/report.md:9,689–710` retains attribution and contribution status. The report is also shipped as DOCX and PDF. Permission for **public** distribution of this remaining metadata was not verified. |

## Export scan

Reviewed HEAD `e611072548d4d77125c92745b13a7021d07ad68f`. In empty reviewer scratch: `git archive HEAD | tar -x -C <scratch>/export`, then removed `CSCE4355_Final_Project_Submission_Guidelines.docx`, `CSCE4355_Project_Proposal_Guidelines.docx`, `Tonight_Build_Plan.md`, and `DB_Monitoring_Dashboard_Project_Proposal.pdf`. Checked all four absences. Scanned **67 files**: 59 plain-text files, one DOCX (17 XML/relationship parts, raw markup and extracted text), one PDF (`pdftotext`), and six PNGs. Both report formats contained the corrected phrases for SQL retention, delivery semantics, wait timing, customer 42, sample counts and privilege labels.

| Scan | Observed result |
|---|---|
| Local account name and Windows/MSYS/Unix home paths | 0 matches. |
| Addresses except the two allowed IPv4 values | 0 confirmed. Three IPv6-shaped matches in OCR text were partial `HH:MM::` clock readings following dates in the synthetic wait-start column, not network endpoints. |
| Current `.env` passwords | All four nonempty password values compared locally against raw export bytes and extracted content; 0 exact matches. Values were not printed. |
| Test SQL and literals | Compared 42 distinct normalized SQL strings and 18 quoted literal values from test source, plus the privacy marker. Marker: one occurrence, at its definition in `tests/test_capture.py:200`; none in captured results or reports. Expected SQL overlaps in implementation/fixtures. The result-file overlap was a quoted PostgreSQL state in explanatory prose (`docs/results/t5_extra_rules_live.txt:162`), not a statement dump. |
| PNGs | Decoded all six and ran offline Windows OCR; all returned text. The two DOCX embedded images byte-matched two of those PNGs. This was **not** a manual visual inspection or proof that OCR recognized every character. |

## Fresh test evidence

Confirmed `review2_t2` absent on the loopback project cluster (port 55432). `bash scripts/setup_db.sh review2_t2` succeeded: PostgreSQL 17.11, `pg_stat_statements` 1.11, 1,000 customers and 10,000 orders. Ran `TEST_DB=review2_t2 .venv/Scripts/python.exe -B -m pytest -p no:cacheprovider --basetemp=<reviewer-scratch>/pytest tests/test_capture.py tests/test_privileges.py`: **4 capture + 6 privilege = 10 passed in 0.64 s**, exit 0. `dropdb review2_t2` succeeded; catalog readback returned 0 databases with that name. `docs/results/final_pytest.txt:41` records 30 passed in 22.21 s; that is an archived full-suite result, not this rerun.

## Required action and notes

1. **Before making the export public:** confirm permission for the attribution and contribution status as actually published, or omit identifying teammate details from the public README and all report formats while preserving the private submission. If files change, scan the replacement export. If permission is confirmed for these unchanged files, there is no remaining technical publication blocker from this review.
2. **Documentation follow-up (non-blocking):** correct the notifier docstring and README evidence pointer in finding 4. An operator-visible print time cannot be recovered from these artifacts.

The decision applies to the scratch export only. No commit, push, publication, signed-out access check or submission was performed; the nested clone was not used.
