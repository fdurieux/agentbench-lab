# Early repayment scheduling policy

Apply these rules in order. A failure returns status REJECTED, the indicated reason, and a null settlement date.

1. The loan status must be ACTIVE. Otherwise return LOAN_NOT_ACTIVE.
2. The requested principal in cents must be a positive integer no greater than the outstanding principal. Otherwise return INVALID_AMOUNT.
3. Both dates must be valid calendar dates in the exact YYYY-MM-DD form. Otherwise return INVALID_DATE.
4. The earliest date is fourteen calendar days after the request date, inclusive. An earlier requested settlement date returns NOTICE_TOO_SHORT.
5. When the requested settlement date satisfies the notice period, move it forward to the next Monday if it falls on Saturday or Sunday. The date adjustment does not consider public holidays.
6. Return status SCHEDULED, a null reason, and the adjusted settlement date in YYYY-MM-DD format.
