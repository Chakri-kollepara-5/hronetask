# Engineering decisions

1. **Indexes.** `employees.emp_code` and `attendance_logs(emp_code, date)` are unique so concurrent inserts cannot violate identity or the one-record-per-day rule. Supporting indexes cover department/join-date headcount, date-sorted attendance listing, employee punch-in lookup, and date/status analytics. I avoided a separate index for every optional filter because extra indexes increase write and storage costs.

2. **Punch-in race.** The endpoint may check for an existing record to return a useful error, but correctness comes from the unique `(emp_code, date)` index. Concurrent inserts race at MongoDB; one succeeds with 201, and duplicate-key failures become 409.

3. **Ties.** The leaderboard sorts the aggregated totals, assigns standard competition ranks (`1, 2, 2, 4`), then applies `rank <= limit`. Therefore a tie at the cutoff is fully returned, ordered by total late minutes and then employee code.

4. **Headcount.** Department summary counts employees with `joined_on <= month-end` independently of attendance logs, so zero-log employees count and later joiners do not. Trend headcount applies the same rule for each day in its generated calendar series.

5. **At 100x scale.** I would measure the nested daily trend lookups and analytics explain plans first, then maintain a daily department attendance read model if those queries dominate. That reduces repeated joins while keeping the source attendance records authoritative.
