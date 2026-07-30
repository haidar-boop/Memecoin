"""Guards on the pre-flight measurement probe itself.

Why this file exists: the probe printed "facts filled in 0" across 25 of the
operator's real coins, and the reason was not the data — it called
``Settings.from_env()``, which does not load ``.env``, so it never saw his Helius
key and every census refused before making a call. A measurement tool that
reports "this change would do nothing" when it simply never authenticated is the
same class of trap as ``security_cap_impact.py``, which reported the inverse of
the truth after its setting was deleted. Both directions read as clearance.
"""

import importlib.util
import pathlib

import pytest

PROBE = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "onchain_facts_probe.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("onchain_facts_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_reads_dotenv_rather_than_only_the_process_environment():
    """The field failure, pinned. `Settings.from_env()` skips load_dotenv(), so
    the probe MUST go through get_settings() or it silently runs unauthenticated
    against a droplet whose .env holds the key."""
    source = PROBE.read_text()
    assert "settings = get_settings()" in source
    assert "settings = Settings.from_env()" not in source, (
        "from_env() does not load .env — the probe would not see the operator's "
        "Helius key and would report 'facts filled in 0' regardless of the data")


def test_the_probe_opens_the_database_read_only():
    """It is run against the live monitor. It must not be able to write."""
    source = PROBE.read_text()
    assert "mode=ro" in source and "uri=True" in source
    upper = source.upper()
    for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "CREATE TABLE"):
        assert statement not in upper, f"probe must not contain {statement!r}"


def test_it_passes_a_pool_address_so_the_lp_half_is_actually_exercised():
    """It used to hardcode None, so 'LP status unknown' was structurally
    guaranteed and its own docstring pre-excused the result."""
    source = PROBE.read_text()
    assert "pair.pair_address" in source, "the LP read needs a real pool address"
    assert 'collect(row["address"], None)' not in source


def test_hostile_stored_json_is_skipped_rather_than_crashing_the_run():
    """Stored facts come from provider JSON that came from attacker-chosen token
    metadata. A nested object where a float belongs used to reach the analyzer
    and raise TypeError, ending the whole run partway through."""
    probe = load_probe()

    class Row(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

    row = Row(chain="solana", address="MintAddr1", symbol="X",
              facts='{"top_holder_percent": {"nested": true}, '
                    '"lp_locked_percent": "99", "is_mintable": false}')
    profile = probe.profile_from_row(row)
    assert profile is not None
    assert profile.top_holder_percent is None, "the dict must be dropped, not passed on"
    assert profile.is_mintable is False


@pytest.mark.parametrize("facts", ["not json at all", "[]", "null", "123"])
def test_unusable_rows_return_none_instead_of_raising(facts):
    probe = load_probe()
    row = dict(chain="solana", address="MintAddr1", symbol="X", facts=facts)
    assert probe.profile_from_row(row) is None
