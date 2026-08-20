"""Property tests for the splitter.

Chunk-size overruns and dropped sentences are the two failure modes that are
invisible in a fixed example but corrupt a corpus at scale, so they are checked
against randomized input rather than one hand-picked paragraph.
"""

import random

import pytest

from depth_explorer.core.chunking import ChunkingConfig, split_sentences, split_text

WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
         "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi"]


def _paragraph(rng, sentences=(1, 30), words=(1, 45)):
    return " ".join(
        " ".join(rng.choice(WORDS) for _ in range(rng.randint(*words))).capitalize() + "."
        for _ in range(rng.randint(*sentences))
    )


def _configs(rng):
    return ChunkingConfig(
        max_chars=rng.choice([40, 60, 100, 150, 300, 800, 1200]),
        overlap_chars=rng.choice([0, 10, 20, 80, 200, 500]),
        min_chars=rng.choice([0, 30, 100, 400]),
    )


@pytest.mark.parametrize("seed", range(25))
def test_no_window_exceeds_the_budget(seed):
    rng = random.Random(seed)
    for _ in range(40):
        config = _configs(rng)
        for window in split_text(_paragraph(rng), config):
            assert len(window) <= config.max_chars


@pytest.mark.parametrize("seed", range(25))
def test_no_window_is_empty(seed):
    rng = random.Random(seed)
    for _ in range(40):
        assert all(w.strip() for w in split_text(_paragraph(rng), _configs(rng)))


@pytest.mark.parametrize("seed", range(15))
def test_every_sentence_survives_somewhere(seed):
    """Overlap may duplicate content, but splitting must never drop it."""
    rng = random.Random(seed)
    for _ in range(20):
        text = _paragraph(rng, sentences=(1, 15), words=(1, 12))
        config = ChunkingConfig(max_chars=rng.choice([200, 400, 900]),
                                overlap_chars=rng.choice([0, 50]))
        joined = " ".join(split_text(text, config))
        for sentence in split_sentences(text):
            assert sentence in joined


def test_splitting_is_idempotent_for_already_small_text():
    text = "A single short sentence about depth."
    assert split_text(text) == [text]
    assert split_text(" ".join(split_text(text))) == [text]


@pytest.mark.parametrize("pathological", [
    "",
    "   ",
    "\n\n\n",
    ".",
    "word",
    "a" * 5000,
    "Ref. Ref. Ref.",
    "One.Two.Three.",
])
def test_pathological_inputs_do_not_hang_or_raise(pathological):
    config = ChunkingConfig(max_chars=50, overlap_chars=25, min_chars=10)
    windows = split_text(pathological, config)
    assert all(len(w) <= config.max_chars for w in windows)
