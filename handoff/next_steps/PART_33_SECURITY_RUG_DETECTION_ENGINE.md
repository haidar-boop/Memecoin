PART 33 — SECURITY & RUG DETECTION INTELLIGENCE ENGINE
Mission
Create a comprehensive security intelligence system designed to identify scam patterns, malicious behavior, manipulation attempts, and high-risk meme coin structures before capital is exposed.
The AI must operate under the assumption:
Every new token carries risk until proven otherwise.
The purpose of this system is not to guarantee safety.
The purpose is to reduce exposure to obvious and hidden threats.

Section 1 — Security Investigation Framework
Every token must pass a security investigation before receiving a positive ranking.
Analyze:
Smart contract security
Ownership structure
Liquidity security
Token permissions
Developer behavior
Wallet concentration
Trading restrictions
Social authenticity
Market manipulation risks

Section 2 — Smart Contract Risk Analysis
Analyze the contract code and permissions.
Check:

Ownership Status
Determine:
Is ownership renounced?
Who controls the contract?
Can important settings be changed?

Administrative Controls
Check for:
Ability to change fees
Ability to pause trading
Ability to blacklist wallets
Ability to modify balances
Ability to mint additional tokens

Upgradeability Risks
Analyze:
Proxy contracts
Upgrade permissions
Admin wallet control
Warning:
A contract that can change significantly after launch creates additional risk.

Section 3 — Honeypot Detection System
Determine whether users can freely buy and sell.
Analyze:
Buy ability
Sell ability
Transfer restrictions
Trading limits
Tax behavior

Honeypot Warning Signs
Examples:
Users can buy but cannot sell
Extreme sell taxes
Hidden restrictions
Selective wallet permissions

Section 4 — Liquidity Rug Detection
Analyze liquidity safety.
Check:
Liquidity amount
Liquidity ownership
Lock status
Lock duration
Liquidity provider behavior

Liquidity Risk Indicators
Warning signs:
Developer controls liquidity
Liquidity can be removed instantly
Extremely low liquidity
Sudden liquidity withdrawal

Section 5 — Developer Investigation
Analyze the creator.
Research:
Previous projects
Wallet history
Launch patterns
Communication behavior

Developer Red Flags
Examples:
Multiple failed launches
Anonymous behavior combined with suspicious activity
Early selling
Large token movements
Lack of communication

Developer Positive Signals
Examples:
Transparent updates
Consistent involvement
Community interaction
Long-term behavior

Section 6 — Token Distribution Risk
Analyze supply ownership.
Measure:
Top holders
Early buyers
Developer allocation
Insider concentration

Distribution Warning Signs
Examples:
Few wallets control supply
Hidden wallet clusters
Large early allocations
Coordinated selling ability

Section 7 — Fake Volume Detection
Analyze whether trading activity is real.
Look for:
Repeated wallet interactions
Artificial transaction patterns
Unusual buy/sell ratios
Same entities creating volume

Fake Volume Indicators
Examples:
High volume with low community growth
High activity from few wallets
No increase in real holders

Section 8 — Social Scam Detection
Analyze community authenticity.

Fake Community Indicators
Check:
Bot followers
Fake comments
Repeated messages
Artificial engagement
Paid hype campaigns

Real Community Indicators
Check:
Organic discussions
User-created content
Independent conversations
Long-term participation

Section 9 — Influencer Manipulation Detection
Analyze promotional activity.
Look for:
Sudden influencer campaigns
Coordinated promotions
Paid hype without fundamentals

Warning Pattern
Promotion increases rapidly.
Price increases.
Early wallets sell.
Community activity disappears.

Section 10 — Contract Change Monitoring
Continuously monitor:
Ownership changes
Permission changes
Contract upgrades
Fee changes

If changes occur:
Trigger:
HIGH PRIORITY SECURITY REVIEW

Section 11 — Rug Risk Scoring System
Create a risk score.
Score:
0–100
Higher score means safer.

Contract Safety
/25
Evaluate:
Permissions
Code risks
Restrictions

Liquidity Safety
/20
Evaluate:
Locking
Control
Stability

Developer Safety
/20
Evaluate:
History
Transparency
Behavior

Distribution Safety
/20
Evaluate:
Holder concentration
Wallet clusters

Social Authenticity
/15
Evaluate:
Community quality
Manipulation risk

Section 12 — Rug Detection Report Format
Every security report must include:

Security Overview
Token:
Contract:
Chain:
Launch Date:

Contract Analysis
Status:
Safe / Caution / Dangerous
Findings:

Liquidity Analysis
Status:
Safe / Caution / Dangerous
Findings:

Developer Analysis
Status:
Safe / Unknown / Risky
Findings:

Holder Risk Analysis
Status:
Safe / Caution / Dangerous
Findings:

Social Manipulation Analysis
Status:
Organic / Mixed / Artificial
Findings:

Final Security Score
/100

Final Security Classification
Choose:
Low Risk
Strong security profile.

Moderate Risk
Some concerns require monitoring.

High Risk
Multiple warning signs.

Avoid
Critical security failures.

Section 13 — Emergency Warning System
Immediately alert when:

Critical Alert
Trigger:
Liquidity removal
Honeypot detection
Developer dumping
Contract compromise

High Risk Alert
Trigger:
Large insider movement
Sudden ownership changes
Abnormal trading activity

Medium Risk Alert
Trigger:
Growing concentration
Community decline
Increasing selling pressure

Section 14 — Final Rug Prevention Rule
The AI must remember:
A good-looking meme does not protect investors.
A large community does not guarantee safety.
A rising chart does not prove legitimacy.
Security comes first.
Before evaluating upside, determine:
"Can this project safely exist?"
Only after passing security analysis should opportunity analysis begin.

---

NOTE (added for this handoff, not part of the original spec text above):
This part's Section 11 rug-risk sub-weighting (Contract 25 / Liquidity 20
/ Developer 20 / Distribution 20 / Social 15) is what
`config/settings.py::SecuritySubWeights` implements today — this is the
most detailed and latest-written version among the several security
sub-weight variants that appeared across the spec, so it was adopted as
canonical. Sections 1–8, 11–12, 14 are already fully built
(`analyzers/security_analyzer.py`). Section 10 (contract change
monitoring) and part of Section 9 (influencer manipulation) were built
during Part 18's session as `analyzers/security_monitor.py` and the
promotion-and-exit detector in `analyzers/wallet_intelligence.py`. See
STATUS.md Part 18 entry and DECISIONS_LOG.md item 2 for the full history.
