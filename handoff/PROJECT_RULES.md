PROJECT RULES — MEME COIN INTELLIGENCE SYSTEM
Purpose
These rules define how the AI should build, modify, and maintain this project.
The accompanying project specification is the source of truth for features and functionality.
These rules explain how the software should be engineered.
Rule 1 — Follow the Specification
Always use the attached project specification as the primary source of requirements.
Do not invent major features that conflict with the specification.
If a requirement is unclear, make the most reasonable implementation and explain your assumptions.
Rule 2 — Build in Small Steps
Do not attempt to build the entire application in one response.
Implement one logical section at a time.
After completing a section:
● Verify functionality
● Explain what was built
● List remaining work
● Wait for the next implementation request
Rule 3 — Never Break Working Code
Before changing existing code:
● Understand what it currently does.
● Preserve existing functionality whenever possible.
● Improve code without removing working features unless necessary.
If a breaking change is required, explain why.
Rule 4 — Modular Design
Separate responsibilities.
Examples:
Scanner
Security
Community
On-chain
Scoring
Dashboard
Alerts
Database
Configuration
Logging
Testing
Avoid putting unrelated functionality into one large file.
Rule 5 — Prioritize Readability
Write code that another developer can easily understand.
Use:
● Clear variable names
● Comments where helpful
● Logical folder structure
● Consistent formatting
Avoid unnecessary complexity.
Rule 6 — Production Quality
Assume the project is intended for real-world use.
Avoid placeholder implementations whenever practical.
Handle:
● Errors
● Missing data
● API failures
● Invalid responses
● Timeouts
Rule 7 — Build for Reliability
The scanner should run continuously.
Design for:
● Automatic recovery after crashes
● Error logging
● Retry logic
● Graceful shutdown
● Stable long-running execution
Rule 8 — Data Before Assumptions
Base conclusions on available evidence.
Do not create scores using unsupported assumptions.
If important information is missing:
● Reduce confidence
● Explain uncertainty
Do not fabricate data.
Rule 9 — Multi-Source Intelligence
Never rely on one source whenever practical.
Combine information from:
● Blockchain
● Market data
● Community
● Security
● Social signals
If one provider becomes unavailable:
Continue using the remaining available data.
Rule 10 — Efficient Data Collection
Avoid unnecessary requests.
Prefer:
● WebSockets
● Event-driven monitoring
● Cached data
● Intelligent refresh schedules
Only perform expensive analysis when a token passes initial filtering.
Rule 11 — Avoid API Abuse
The system must respect provider limits.
Use:
● Request queues
● Rate limiting
● Retry delays
● Exponential backoff
● Caching
The objective is stable operation, not maximum request frequency.
Rule 12 — Keep Performance High
Optimize for:
● Fast scanning
● Efficient memory usage
● Efficient database queries
● Low latency
Avoid unnecessary processing.
Rule 13 — Logging
Every important action should be logged.
Examples:
Scanner started
API unavailable
Security analysis completed
Alert generated
Database updated
Errors
Warnings
Rule 14 — Testing
When implementing a new feature:
Verify that:
● It works correctly.
● It does not break existing functionality.
● Obvious edge cases are handled.
When practical, include automated tests.
Rule 15 — Documentation
Document:
● New modules
● APIs
● Configuration
● Database changes
● Major architectural decisions
Keep documentation updated.
Rule 16 — Security
Do not expose:
● API keys
● Private keys
● Secrets
● Passwords
Store secrets securely using environment variables or a secure secrets manager.
Rule 17 — Configuration
Avoid hardcoding values.
Place configurable settings in configuration files or environment variables.
Examples:
Refresh intervals
Thresholds
API keys
Database settings
Alert settings
Rule 18 — Backward Compatibility
When improving an existing module:
Prefer extending functionality rather than rewriting everything.
Only replace large sections when there is a clear benefit.
Rule 19 — Explain Major Decisions
When making important architectural decisions:
Briefly explain:
● Why the approach was chosen
● Benefits
● Trade-offs
Rule 20 — Ask Before Major Changes
If the specification is unclear or two valid implementations exist:
Do not guess silently.
Present the options and explain the advantages and disadvantages before proceeding.
Rule 21 — Development Mindset
The objective is not simply to write code.
The objective is to build a reliable, maintainable, and extensible meme coin intelligence
platform.
Prioritize:
● Accuracy
● Stability
● Scalability
● Clear architecture
● Practical performance
Avoid unnecessary complexity.
Simple, reliable solutions are preferred over clever but fragile ones.
