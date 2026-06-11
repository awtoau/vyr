## What

<!-- one paragraph; link the issue (F-track or sub-issue) -->

## Invariant checklist

- [ ] No `std` leakage into `vyr-core` (I7)
- [ ] No full-frame assumptions — works for any `area` band (I1)
- [ ] New primitive/widget ⇒ bench + golden included in this PR (I3)
- [ ] Goldens and perf baseline untouched (or this PR is *only* a reviewed
      baseline change)
- [ ] No default chrome introduced (I5); failures are hard errors, not blanks (I6)
- [ ] Commits signed off (`git commit -s`)
