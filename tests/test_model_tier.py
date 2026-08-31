"""Tests for anthrouter.model_tier — canonical tier vocabulary and ranking.

Covers:
- classify_model_tier: short names, suffixed IDs, full provider IDs, unknown, case insensitivity
- model_tier_rank: haiku/sonnet/opus/fable ranks, unknown → None, highest-rank wins
- _cap_cached_tier (model_router): no-upgrade cap, fail-open on unknown
"""

from __future__ import annotations

import pytest

from anthrouter.model_tier import TIER_RANK, classify_model_tier, model_tier_rank


# ---------------------------------------------------------------------------
# classify_model_tier
# ---------------------------------------------------------------------------

class TestClassifyModelTier:
    """classify_model_tier returns one of haiku/sonnet/opus/fable or 'other'."""

    @pytest.mark.parametrize("model,expected", [
        # Short canonical names
        ('haiku',  'haiku'),
        ('sonnet', 'sonnet'),
        ('opus',   'opus'),
        ('fable',  'fable'),
    ])
    def test_short_names(self, model, expected):
        """Short canonical tier names resolve directly."""
        assert classify_model_tier(model) == expected

    @pytest.mark.parametrize("model,expected", [
        # Suffixed IDs (bracket and colon context suffixes)
        ('opus[1m]',   'opus'),
        ('sonnet:1m',  'sonnet'),
        ('haiku[1m]',  'haiku'),
        ('fable[1m]',  'fable'),
    ])
    def test_suffixed_ids(self, model, expected):
        """Context-suffix variants resolve to their coarse tier."""
        assert classify_model_tier(model) == expected

    @pytest.mark.parametrize("model,expected", [
        # Full provider IDs
        ('claude-3-5-haiku-20241022',  'haiku'),
        ('anthropic/claude-opus-4',    'opus'),
        ('claude-sonnet-4-6',          'sonnet'),
        ('anthropic/claude-fable-3',   'fable'),
    ])
    def test_full_provider_ids(self, model, expected):
        """Full provider ID strings (with date stamps, namespaces) resolve to coarse tier."""
        assert classify_model_tier(model) == expected

    @pytest.mark.parametrize("model,expected", [
        ('gpt-4o',  'other'),
        ('',        'other'),
        ('plugin-model-1', 'other'),
        ('codex',   'other'),
    ])
    def test_unknown_models(self, model, expected):
        """Non-tier model IDs resolve to 'other'."""
        assert classify_model_tier(model) == expected

    @pytest.mark.parametrize("model,expected", [
        ('Claude-FABLE',  'fable'),
        ('OPUS',          'opus'),
        ('HAIKU',         'haiku'),
        ('SONNET',        'sonnet'),
        ('CLAUDE-OPUS-4-8', 'opus'),
    ])
    def test_case_insensitive(self, model, expected):
        """Matching is case-insensitive."""
        assert classify_model_tier(model) == expected

    def test_highest_rank_wins_when_multiple_tiers_in_name(self):
        """When multiple tier names appear in one string, the highest rank wins.

        Iteration order is fable → opus → sonnet → haiku, so the first match
        in that descending sequence is returned.  A model string containing both
        'opus' and 'haiku' resolves to 'opus', not 'haiku'.
        """
        # 'fable-opus-crossover' contains both 'fable' and 'opus'; fable wins.
        assert classify_model_tier('fable-opus-crossover') == 'fable'
        # 'opus-haiku-bridge' contains 'opus' and 'haiku'; opus wins.
        assert classify_model_tier('opus-haiku-bridge') == 'opus'
        # 'sonnet-haiku-variant' contains 'sonnet' and 'haiku'; sonnet wins.
        assert classify_model_tier('sonnet-haiku-variant') == 'sonnet'


# ---------------------------------------------------------------------------
# model_tier_rank
# ---------------------------------------------------------------------------

class TestModelTierRank:
    """model_tier_rank returns an integer rank or None for unknown tiers."""

    @pytest.mark.parametrize("model,expected", [
        ('haiku',  0),
        ('sonnet', 1),
        ('opus',   2),
        ('fable',  3),
    ])
    def test_known_tiers(self, model, expected):
        """Known tier names map to their documented rank."""
        assert model_tier_rank(model) == expected

    def test_tier_rank_dict_matches_function(self):
        """TIER_RANK constant is consistent with model_tier_rank for all known tiers."""
        for tier, rank in TIER_RANK.items():
            assert model_tier_rank(tier) == rank

    @pytest.mark.parametrize("model", ['gpt-4o', '', 'plugin-model-1', 'codex'])
    def test_unknown_models_return_none(self, model):
        """Unknown / non-tier models return None (fail-open, not -1)."""
        assert model_tier_rank(model) is None

    def test_fable_rank_exceeds_opus(self):
        """fable rank (3) is strictly greater than opus (2)."""
        assert model_tier_rank('fable') > model_tier_rank('opus')

    def test_rank_ordering(self):
        """haiku < sonnet < opus < fable."""
        ranks = [model_tier_rank(t) for t in ('haiku', 'sonnet', 'opus', 'fable')]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == 4  # all distinct

    def test_order_precedence_highest_rank_wins(self):
        """When two tier names are in a string, the higher-ranked tier is returned.

        classify_model_tier resolves to the first match in (fable, opus, sonnet, haiku)
        order — so model_tier_rank of that string equals the higher tier's rank.
        """
        # 'fable' substring found before 'opus'; rank should equal fable rank (3)
        assert model_tier_rank('fable-opus-crossover') == model_tier_rank('fable')
        # 'opus' found before 'haiku'; rank should equal opus rank (2)
        assert model_tier_rank('opus-haiku-bridge') == model_tier_rank('opus')



# ---------------------------------------------------------------------------
# _cap_cached_tier (model_router)
# ---------------------------------------------------------------------------

class TestCapCachedTier:
    """_cap_cached_tier clamps a replayed cached tier so it never upgrades the request.

    Upgrade-only cap, asymmetric: a cached LOWER tier can still downgrade a
    higher-requested continuation.  Fails open when either rank is unknown.
    """

    @pytest.fixture(autouse=True)
    def _import(self):
        from anthrouter.model_router import _cap_cached_tier
        self._cap = _cap_cached_tier

    def test_cap_applies_when_cached_above_requested(self):
        """cached='fable'(3), requested='haiku'(0) → 'haiku' (cap applied)."""
        result = self._cap('fable', 'haiku')
        assert result == 'haiku'

    def test_cap_does_not_apply_when_cached_below_requested(self):
        """cached='opus'(2), requested='fable'(3) → 'opus' (no cap: cached < requested).

        The cap only prevents upgrades.  A cached lower tier may still downgrade a
        higher-requested continuation — asymmetric by design.
        """
        result = self._cap('opus', 'fable')
        assert result == 'opus'

    def test_cap_applies_when_cached_just_above_requested(self):
        """cached='fable'(3), requested='opus'(2) → 'opus' (cap applied)."""
        result = self._cap('fable', 'opus')
        assert result == 'opus'

    def test_no_cap_when_cached_equals_requested(self):
        """cached='sonnet'(1), requested='sonnet'(1) → 'sonnet' (equal, no cap)."""
        result = self._cap('sonnet', 'sonnet')
        assert result == 'sonnet'

    def test_fail_open_when_cached_unknown(self):
        """cached=unknown model, requested='sonnet' → cached unchanged (fail-open).

        When the cached tier's rank is None, the cap cannot fire; the cached
        value is returned unchanged to preserve the original routing decision.
        """
        unknown_cached = 'gpt-4o-custom'
        result = self._cap(unknown_cached, 'sonnet')
        assert result == unknown_cached

    def test_fail_open_when_requested_unknown(self):
        """cached='fable', requested=unknown → cached unchanged (fail-open)."""
        result = self._cap('fable', 'gpt-4o')
        assert result == 'fable'

    def test_cap_with_label_map_configured_fable_as_deep(self):
        """label_map={deep: fable} ranks 'fable' as deep (rank 2) via exact reverse lookup.

        When the operator maps 'deep'→'fable', the reverse map gives fable a rank of 2
        (the _LABEL_ORDER rank of 'deep'), so cached='fable' vs requested='sonnet'(1)
        resolves: cached_rank(2) > requested_rank(1) → cap to 'sonnet'.
        """
        label_map = {'trivial': 'haiku', 'standard': 'sonnet', 'deep': 'fable'}
        result = self._cap('fable', 'sonnet', label_map=label_map)
        assert result == 'sonnet'

    def test_cap_with_label_map_no_cap_when_cached_lower(self):
        """With label_map, cached='haiku'(0) vs requested='sonnet'(1) → 'haiku' (no cap)."""
        label_map = {'trivial': 'haiku', 'standard': 'sonnet', 'deep': 'fable'}
        result = self._cap('haiku', 'sonnet', label_map=label_map)
        assert result == 'haiku'

