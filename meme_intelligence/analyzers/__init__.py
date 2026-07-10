"""Analysis engines: one module per intelligence category (Spec Part 22 structure).

* ``security_analyzer``   -- rug/scam risk (Parts 4, 18, 33)
* ``security_monitor``    -- continuous contract-change monitoring (Part 18)
* ``community_analyzer``  -- social strength + fake detection (Part 5)
* ``foundation_analyzer`` -- foundation score combiner (Part 5, Section 12)
* ``onchain_analyzer``    -- wallet/holder behavior (Part 6)
* ``token_analyzer``      -- token structure & valuation (Part 7)
* ``momentum_analyzer``   -- momentum & market timing (Parts 14, 26)
* ``narrative_analyzer``  -- narrative intelligence & viral potential (Part 19)
* ``wallet_intelligence`` -- smart money & whale intelligence (Part 17)
* ``risk_analyzer``       -- risk scoring & portfolio discipline (Part 9)
* ``scoring_engine``      -- combined final score & decision tree (Parts 10, 31)
"""
