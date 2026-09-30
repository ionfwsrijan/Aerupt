# Routes, Fares & Classes (booking support KB)

How the platform picks fares and what class choices mean — grounding for "book the cheapest", "business class", and re-picking after a correction.

## Fare features by class (typical)
- **Economy**: basic seat, single checked bag on long haul (varies), cheapest fares, most restriction on changes/refunds.
- **Premium Economy**: wider seat, better meals, 2 checked bags, more flexible fares, dedicated cabin.
- **Business**: lie-flat or cradle seats on long haul, lounge access, priority boarding/security, 2–3 checked bags, most flexible.
- **First**: the top cabin, highest allowance, most flexibility, premium check-in.

## Price arbitration rules the platform applies
- When the user asks for the **cheapest** or "budget", the platform sorts results by price and picks the lowest fare that satisfies origin/destination/date/class.
- When the user asks for **business/first**, only fares in that cabin are considered.
- When the user asks for a specific **flight id** (e.g. SU454), that exact flight is booked regardless of price.
- When a second turn re-targets a route ("actually, to Tokyo"), the platform re-runs a **fresh search for the new route** instead of reusing a stale flight from the previous route.

## Defaults
- If only a city pair is given, the platform uses each city's default airport (see airport codes).
- If no class is given, the cheapest matching availability is chosen unless overridden.
- If no date is given, the current travel date is used.
- Multi-passenger bookings: the same flight is booked for the stated count of passengers; price shown is per person.