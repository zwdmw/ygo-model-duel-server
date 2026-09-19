"""Run the frozen upstream (official) checkpoint inside the external-duel path.

The official model is an upstream ``RNNAgent(structured=False)``: its card
embedding table only covers the 864-card official code list, while this runtime
builds observations against a much larger pool code list. Every forward pass
therefore needs the observation remapped into the official id space -- the same
step ``scripts/evaluate_structured_vs_official.py`` performs when it plays the
structured and official models against each other.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

ARCHITECTURE = "official"
# Observed fields that carry card ids as two bytes, and the byte offset of the id.
ID_FIELDS = (("cards_", 0), ("actions_", 1), ("h_actions_", 1))
# The public checkpoints predate the read-back of the real table; 999 covers the
# 864-card official list and is what the reference implementation uses.
PROVISIONAL_MAX_ID = 999


def read_codes(path):
    """Card codes in table order.

    The runtime pool list is ``<code> <flag>`` per line with the 1-based line number
    as the network id (``vendor/netduel/cards.py::_load_code_list``); the official
    list is one code per line. Both are read by their first token, line order kept.
    """
    codes = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if parts and parts[0].lstrip("-").isdigit():
            codes.append(int(parts[0]))
    if len(codes) != len(set(codes)):
        raise ValueError("code list contains duplicate card codes: %s" % path)
    return codes


def build_lookup(current_codes, official_codes, official_max_id):
    """``lookup[current_id] = official_id``, 0 when the official model lacks the card."""
    import numpy as np
    if len(official_codes) > official_max_id:
        raise ValueError("official code list has %d rows but the checkpoint accepts at most %d"
                         % (len(official_codes), official_max_id))
    official_code_to_id = {code: index for index, code in enumerate(official_codes, start=1)}
    lookup = np.zeros(len(current_codes) + 1, dtype=np.int32)
    for current_id, code in enumerate(current_codes, start=1):
        lookup[current_id] = official_code_to_id.get(code, 0)
    mapped = int(np.count_nonzero(lookup))
    stats = {"current_rows": len(current_codes), "official_rows": len(official_codes),
             "mapped_current_rows": mapped, "unmapped_current_rows": len(current_codes) - mapped,
             "official_max_id": int(official_max_id)}
    return lookup, stats


def _card_ids(values, start):
    return values[..., start].astype("int32") * 256 + values[..., start + 1].astype("int32")


def remap_observation(observation, lookup):
    """Rewrite card ids into the official id space (unmapped ids become 0)."""
    import jax.numpy as jnp
    remapped = dict(observation)
    for name, start in ID_FIELDS:
        values = observation.get(name)
        if values is None:
            continue
        card_ids = _card_ids(values, start)
        in_range = card_ids < lookup.shape[0]
        mapped = lookup[jnp.clip(card_ids, 0, lookup.shape[0] - 1)]
        mapped = jnp.where(in_range, mapped, 0)
        values = values.at[..., start].set((mapped >> 8).astype(values.dtype))
        values = values.at[..., start + 1].set((mapped & 0xFF).astype(values.dtype))
        remapped[name] = values
    return remapped


def count_unmapped(observation, lookup):
    """How many observed cards the official model does not know."""
    import jax.numpy as jnp
    total = jnp.asarray(0, dtype=jnp.int32)
    for name, start in ID_FIELDS:
        values = observation.get(name)
        if values is None:
            continue
        card_ids = _card_ids(values, start)
        in_range = card_ids < lookup.shape[0]
        mapped = lookup[jnp.clip(card_ids, 0, lookup.shape[0] - 1)]
        total = total + jnp.count_nonzero((card_ids != 0) & (~in_range | (mapped == 0)))
    return total


_LOOKUPS = {}


def lookup_for(stats):
    """Rebuild (and cache) the id lookup described by ``stats``."""
    key = (stats["code_list"], stats["official_code_list"], stats["official_max_id"])
    if key not in _LOOKUPS:
        import jax.numpy as jnp
        lookup, _ = build_lookup(read_codes(stats["code_list"]),
                                 read_codes(stats["official_code_list"]),
                                 stats["official_max_id"])
        _LOOKUPS[key] = jnp.asarray(lookup)
    return _LOOKUPS[key]


def official_code_list_path(app):
    path = Path(app) / "project/scripts/official_code_list_864.txt"
    if not path.is_file():
        raise ValueError("official code list is missing: %s" % path)
    return path


def resolve_code_list(app, assets):
    """The code list the observation encoder uses; the lookup must match it."""
    if assets is not None:
        return Path(assets.path) / "code_list.txt"
    return Path(app) / "project/scripts/code_list.txt"


def load_policy(args, app, checkpoint, assets=None):
    """Return ``(agent, infer, schema, metadata)`` for the frozen official checkpoint."""
    import flax
    from flax.traverse_util import flatten_dict
    import jax
    import jax.numpy as jnp
    from policy_ipc import observation_schema
    from ygoai.rl.jax.agent import RNNAgent

    import worker

    checkpoint = Path(checkpoint)
    if not checkpoint.is_file():
        raise ValueError("official checkpoint is missing: %s" % checkpoint)
    code_list = resolve_code_list(app, assets)
    official_list = official_code_list_path(app)

    # The native engine supplies the observation schema and one sample input.
    template_args = SimpleNamespace(seed=args.seed, output=args.output,
                                    model_deck=worker.MODEL_DECK, human_deck=worker.MODEL_DECK)
    worker.init_decks(template_args)
    env = worker.make_env(template_args, "self", 0)
    try:
        sample, _ = env.reset()
    finally:
        env.close()
    sample = jax.tree.map(lambda x: None if x is None else jnp.asarray(x), sample,
                          is_leaf=lambda x: x is None)

    current_codes = read_codes(code_list)
    official_codes = read_codes(official_list)
    provisional, _ = build_lookup(current_codes, official_codes, PROVISIONAL_MAX_ID)
    provisional = jnp.asarray(provisional)

    agent = RNNAgent(structured=False)
    template = agent.init(jax.random.PRNGKey(0), remap_observation(sample, provisional),
                          agent.init_rnn_state(1))
    raw = checkpoint.read_bytes()
    original = flax.serialization.msgpack_restore(raw)
    template = flax.serialization.from_bytes(template, raw)
    variables = jax.device_put(template)
    embedding_rows = int(template["params"]["Encoder_0"]["Embed_0"]["embedding"].shape[0])

    lookup, stats = build_lookup(current_codes, official_codes, embedding_rows - 1)
    lookup = jnp.asarray(lookup)
    proof = hashlib.sha256()
    for key, value in sorted(flatten_dict(original["params"]).items()):
        proof.update(("/".join(key)).encode())
        proof.update(jnp.asarray(value).tobytes())
    stats.update(architecture=ARCHITECTURE, checkpoint=str(checkpoint),
                 checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
                 learned_parameters_sha256=proof.hexdigest(), embedding_rows=embedding_rows,
                 code_list=str(code_list), official_code_list=str(official_list))

    @jax.jit
    def infer(state, observation):
        next_state, logits = agent.apply(variables, remap_observation(observation, lookup), state)[:2]
        return next_state, jnp.argmax(logits, axis=-1), logits

    warm = infer(agent.init_rnn_state(1), sample)
    jax.block_until_ready(warm[1])
    stats["sample_unmapped_ids"] = int(jnp.asarray(count_unmapped(sample, lookup)))
    return agent, infer, observation_schema(sample), stats


def describe(stats):
    return ("official model %s (id 1..%d, %d cards): %d/%d runtime codes mapped, sample unmapped %s"
            % (Path(stats["checkpoint"]).name, stats["official_max_id"], stats["official_rows"],
               stats["mapped_current_rows"], stats["current_rows"], stats.get("sample_unmapped_ids")))


def as_json(stats):
    return json.dumps(stats, ensure_ascii=False, sort_keys=True)
