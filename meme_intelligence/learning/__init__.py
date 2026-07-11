"""Self-learning "mind" layer for the meme coin scanner.

A reasoning layer that sits on top of the existing discovery + market +
security stack. For every live coin it answers, by analogy: *which coins
from the past does this one most resemble right now, and how did those
coins end up?* — combining three learners whose influence adapts to their
own measured accuracy:

* an append-only FAISS analog memory (instant learning),
* a warm-started LightGBM classifier (periodic learning), and
* a rug-pull engine (hard on-chain signals + rug-by-analogy).

Educational / paper-analysis only: it returns structured verdicts for the
dashboard and alert system. It never trades and never places orders
(Rule 21 — decision support, a human decides).

The layer is built in modular pieces (Rule 4):

* :mod:`meme_intelligence.learning.models`   — data model + outcome labels
* :mod:`meme_intelligence.learning.features`  — trajectory -> fingerprint vector
* :mod:`meme_intelligence.learning.store`     — SQLite persistence

Later increments add the FAISS analog engine, the LightGBM classifier, the
rug engine, the adaptive ensemble, and the ``evaluate_coin`` public API.
"""
