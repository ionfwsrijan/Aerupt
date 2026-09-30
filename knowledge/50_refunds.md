# Refunds & Cancellations

The refund outcome depends on the **fare rules attached to the ticket**, not on the booking platform.

## Refundable vs non-refundable fares
- **Refundable fares**: can be cancelled for a full cash refund (minus any fees), or changed freely. They cost more at purchase.
- **Non-refundable fares**: no cash refund after cancellation; value may live on as a **travel credit / voucher** (often minus a redeposit fee), valid typically 12 months.
- **Partially refundable**: refundable minus a cancellation fee; common in premium economy.
- **Basic/budget fares**: mostly non-refundable and non-changeable — only taxes can be refunded, and sometimes not even that.

## The US 24-hour rule
- Airlines reserve fare **for 24 hours** and allow cancellation *without penalty* within **24 hours of booking** when booked at least 7 days before departure, per U.S. DOT rules.
- So a same-day mistake can generally be reversed free of charge. After 24 hours, the fare rules apply.

## Fees
- **Cancellation fee**: a fixed amount (e.g. 0–$200 or EUR equivalent) deducted before any refund.
- **Redeposit fee** for frequent-flyer award tickets if the ticket uses miles.
- **Change fee**: some carriers removed change fees on most tickets (post-2020); fare difference still applies.

## What AERUPT's cancellation tool does
1. Locates the booking by **reference** (or flight id).
2. Reads the fare class on file.
3. Cancels the PNR on the platform.
4. Reports the outcome, and if the fare is non-refundable, states that value is held as credit and no cash is returned.

## Money-back expectations table
| Fare type | Cash refund? | Credit? | Fee? |
| --- | --- | --- | --- |
| Refundable | Yes, full minus fee | — | possible service fee |
| Non-refundable | No | Yes, minus redeposit fee | redeposit fee |
| Basic economy | No | Usually no | none (nothing to take) |
| Award (miles) | No cash (miles returned) | — | redeposit fee |

## Practical guidance to give travelers
- Read the fare rules **at booking time** — that is when the class is locked in.
- Book a refundable fare when plans are uncertain; it is the only path to cash back.
- Cancel before the 24-hour window expires to reverse a mistake free of charge (7-day-ahead bookings).
- Cancellation requests are entries in a session ledger: they are idempotent — cancelling an already-cancelled booking is reported as a no-op.