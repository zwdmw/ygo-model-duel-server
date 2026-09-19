"""Load unchanged learned parameters against a verified semantic-table extension."""
import hashlib
import json
from types import SimpleNamespace


def load_policy(args, assets):
    import worker
    if assets is None:
        agent, infer, schema = worker.load_policy(args)
        return agent, infer, schema, {}
    import flax
    from flax.traverse_util import flatten_dict
    import jax
    import jax.numpy as jnp
    import numpy as np
    from semantic_loader import inject_semantic_constants, load_structured_semantics
    from ygoai.rl.jax.agent import RNNAgent
    from policy_ipc import observation_schema

    worker.verify_snapshot()
    # The native engine only supplies schema/sample input; new cards run on the remote host.
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
    tables, shape, _ = load_structured_semantics(
        str(assets.path / "semantics.npz"), str(assets.path / "code_list.txt"))
    base_shape = load_structured_semantics(
        str(worker.PROJECT / "assets/structured/frozen_semantics_v1.npz"),
        str(worker.PROJECT / "scripts/code_list.txt"))[1]
    agent = RNNAgent(**{**worker.checkpoint_args(worker.CHECKPOINT, base_shape),
                        "structured": True, "semantic_shape": shape})
    template = flax.core.unfreeze(agent.init(jax.random.PRNGKey(0), sample, agent.init_rnn_state(1)))
    original = flax.serialization.msgpack_restore(worker.CHECKPOINT.read_bytes())
    extra = set(original) - set(template)
    if set(template) - set(original) or any(original[name] != {} for name in extra):
        raise ValueError("Checkpoint collections differ from the semantic extension")
    proof = hashlib.sha256()
    for collection in template:
        if collection == "constants":
            continue
        expected, actual = flatten_dict(template[collection]), flatten_dict(original[collection])
        if set(expected) != set(actual):
            sidecar = worker.CHECKPOINT.with_name(worker.CHECKPOINT.name + ".json")
            hint = ("" if sidecar.is_file() else
                    "; the architecture sidecar is missing, so a newer architecture"
                    f" cannot be identified ({sidecar.name})")
            raise ValueError("Checkpoint learned parameter names changed" + hint)
        for key, value in expected.items():
            if np.shape(value) != np.shape(actual[key]) or np.dtype(value.dtype) != actual[key].dtype:
                raise ValueError("Checkpoint learned parameter shape or dtype changed")
        template[collection] = flax.serialization.from_state_dict(template[collection], original[collection])
        loaded = flatten_dict(template[collection])
        for key in sorted(actual):
            if not np.array_equal(np.asarray(loaded[key]), actual[key]):
                raise ValueError("Learned checkpoint parameter was modified")
            proof.update((collection + "/" + "/".join(key)).encode())
            proof.update(actual[key].tobytes())
    inject_semantic_constants(template, tables)
    variables = jax.device_put(flax.core.freeze(template))

    @jax.jit
    def infer(state, observation):
        next_state, logits = agent.apply(variables, observation, state)[:2]
        return next_state, jnp.argmax(logits, axis=-1), logits

    warm = infer(agent.init_rnn_state(1), sample)
    jax.block_until_ready(warm[1])
    metadata = {**assets.public(), "learned_parameters_unchanged": True,
                "learned_parameters_sha256": proof.hexdigest()}
    print(json.dumps({"event": "external_pool_ready", **metadata}), flush=True)
    return agent, infer, observation_schema(sample), metadata
