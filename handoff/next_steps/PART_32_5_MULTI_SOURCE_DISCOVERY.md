PART 32.5 — MULTI-SOURCE DISCOVERY & ANTI-THROTTLING ARCHITECTURE
Mission
Improve the data collection system by creating a scalable, 24/7 discovery architecture that finds early opportunities while avoiding unnecessary API pressure and provider throttling.
This section expands the existing data architecture.
It does not replace:

* The scoring system
* The security system
* The community system
* The AI reasoning system
* The risk philosophy
Section 1 — Core Discovery Philosophy
The system must not depend on a single website or data provider.
The system should combine:

* Early discovery sources
* Blockchain data
* Market confirmation sources
* Social intelligence sources
* Security analysis sources
The goal is:
Find opportunities early while maintaining reliable data collection.
Section 2 — Discovery Source Separation
The system must separate:
Discovery Sources
Purpose:
Find potential opportunities as early as possible.
Examples:

* New token launches
* New liquidity events
* New trading activity
* Early community growth
Confirmation Sources
Purpose:
Validate whether an opportunity deserves deeper analysis.
Examples:

* Market activity
* Liquidity conditions
* Trading history
* Holder behavior
The AI must never treat discovery as confirmation.
A newly discovered token is only a candidate until it passes analysis.
Section 3 — Pump.fun Early Discovery Integration
The system should support early launch monitoring from platforms such as Pump.fun.
Purpose:
Identify tokens during early stages.
Analyze:

* Launch timing
* Creator activity
* Early wallet behavior
* Holder growth
* Trading activity
* Community signals
The system must not alert on every new launch.
Most launches should be filtered out.
Section 4 — Multi-Source Data Strategy
The system should combine multiple information sources.
Potential categories:
Launch Data
Used for:

* New token detection
* Early activity
Blockchain Data
Used for:

* Transactions
* Wallet behavior
* Holder changes
* Token movements
Market Data
Used for:

* Liquidity
* Volume
* Price activity
* Market confirmation
Social Data
Used for:

* Community strength
* Engagement
* Sentiment
Security Data
Used for:

* Contract analysis
* Rug detection
* Risk evaluation
Section 5 — Anti-Throttling System
The system must be designed to avoid unnecessary API pressure.
Required features:
Caching System
Store recently collected information.
Avoid requesting identical information repeatedly.
Request Prioritization
Not all tokens require equal processing.
Priority levels:
High:

* Rapid growth
* Unusual activity
* Strong early signals
Medium:

* Potential candidates
Low:

* Weak signals
Data Refresh Control
The system must adjust refresh frequency.
Example:
High-interest tokens:
More frequent updates.
Low-interest tokens:
Less frequent updates.
Multiple Provider Support
The system should avoid depending on one provider.
If one source becomes unavailable:

* Use backup sources
* Lower confidence temporarily
* Continue operating
Section 6 — Event-Driven Monitoring
Where possible, use event-based monitoring instead of constant polling.
Examples:
Monitor:

* New liquidity events
* New transactions
* Wallet activity
* Contract events
The system should react to meaningful changes rather than repeatedly requesting unchanged information.
Section 7 — Candidate Filtering Pipeline
The system workflow:

```
New Token Detected

↓

Basic Filtering

↓

Security Quick Check

↓

Early Activity Review

↓

Community Review

↓

Deep AI Analysis

↓

Final Score

↓

Alert Decision

```

Section 8 — Deep Analysis Threshold
The AI should not perform expensive analysis on every token.
A token should receive deeper analysis only after meeting initial requirements.
Possible requirements:

* Minimum activity level
* No obvious security failures
* Evidence of organic interest
* Increasing attention
Section 9 — Data Reliability Handling
Every data source should include:

* Source name
* Timestamp
* Reliability score
* Data confidence
If sources disagree:
The AI should reduce confidence rather than assume one source is correct.
Section 10 — 24/7 Operation Requirements
The system must be designed for continuous operation.
Required:

* Automatic restart after failure
* Error logging
* API failure recovery
* Database backups
* Monitoring alerts
Section 11 — Final Rule
The system must optimize for:
Early discovery.
Reliable verification.
Efficient data usage.
High-quality alerts.
The system should never win by collecting the most data.
It should win by collecting the right data, analyzing it intelligently, and avoiding unnecessary failures caused by poor architecture.

---

NOTE (added for this handoff, not part of the original spec text above):
This part was delivered as the final addendum in the original prompt
sequence, explicitly framed as an *extension* of Part 32, not a
replacement. Section 3 (Pump.fun integration) is the one genuinely new,
unbuilt requirement here — everything else in this part is substantially
covered by the existing `MarketDataService` (Part 15) and
`ContinuousScanner` (Part 13) implementations. See STATUS.md.
